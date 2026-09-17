"""Hanako 任务巡检 — 订阅 Hanako 已产出结果并主动汇报（观察者每 5 分钟轮询）。

BugFix #6-D 需求：把巡检从"自判 cron nextRunAt 临近/过期"改为"订阅 Hanako
已经产出的结果"。重复的自判逻辑（用户明确不要）已删除，改为监控三处 Hanako
产出：

  1. cron 运行结果：~/.hanako/agents/<agent_id>/desk/cron-runs/*.jsonl
     每追加一行（一个 JSON 对象，含 status: success/failed）→
     通知「【任务】<label>跑完了 ✅ / 跑失败了 ❌」（label 取自同目录 cron-jobs.json）。
  2. 延迟任务：~/.hanako/.ephemeral/deferred-tasks.json
     新增 status=pending 条目 → 通知「有 N 个延迟任务待处理」。
  3. （可选）通知约定：~/.hanako/.ephemeral/notifications.json
     存在则读取新条目播报；文件不存在则安全跳过。

不做 near/overdue 自判；只播有用信息，气泡文案精简。
复用现有主动汇报链路（controller._tick_inspection → callback → _on_proactive_trigger）。
节流：同一 key 在 REPORT_REPEAT_SECONDS（默认 30 分钟）内只通知一次。
所有文件缺失/损坏均容错（不抛异常）。
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from . import schedule as _schedule_mod
from .schedule import SchedulePerception

logger = logging.getLogger(__name__)

# 巡检节流：每 5 分钟检查一次（对齐 Hanako cron 设计的观察者轮询粒度）
INSPECTION_INTERVAL_SECONDS = 300.0

# 同一触发 key 的重复通知冷却（30 分钟；期间不再重复提醒）
REPORT_REPEAT_SECONDS = 1800.0


class InspectionPerception:
    """Hanako 任务巡检 — 订阅 cron 运行结果 / 延迟任务 / 通知约定并产出触发文案。"""

    def __init__(self, schedule: SchedulePerception | None = None, agent_id: str = ""):
        self._schedule = schedule or SchedulePerception()
        # agent_id 优先显式传入，否则从 schedule 继承（SchedulePerception._agent_id）
        self._agent_id = (agent_id or getattr(self._schedule, "_agent_id", "") or "").strip()
        self._last_tick_at: float = 0.0
        # 是否已过首个 tick：仅首个 tick 跳过当时已有历史，其后出现的文件从头处理
        self._primed: bool = False
        self._reported: dict[str, float] = {}       # 节流：key -> 上次通知时间
        self._last_findings: list[str] = []          # 最近一次命中（供 prompt 注入）
        # cron-runs 文件游标：每文件已读取行数（只处理新增行，避免重复播报）
        self._run_cursors: dict[str, int] = {}
        # 已播报的 deferred pending 任务 key（只在「新增」时播报）
        self._seen_deferred: set[str] = set()
        # 已播报的 notifications 条目 id（只在「新增」时播报）
        self._seen_notify: set[str] = set()

    # ── 对外入口 ──────────────────────────────────────────

    def tick(self, now: float | None = None) -> list[str]:
        """执行一次巡检（带 5 分钟节流）。

        Args:
            now: 注入的当前时间（测试用；缺省 time.time()）。

        Returns:
            命中的触发文案列表；节流内或未命中返回空列表。
        """
        now = time.time() if now is None else float(now)
        if now - self._last_tick_at < INSPECTION_INTERVAL_SECONDS:
            return []
        self._last_tick_at = now
        priming = not self._primed

        # 刷新 cron-jobs.json（仅供 label 查表；缺失/损坏容错）
        try:
            self._schedule.refresh()
        except Exception as e:
            logger.debug("Inspection schedule refresh failed: %s", e)

        findings: list[tuple[str, str]] = []
        try:
            findings.extend(self._check_cron_runs(now, priming))
        except Exception as e:
            logger.debug("Inspection cron-runs check failed: %s", e)
        try:
            findings.extend(self._check_deferred(now))
        except Exception as e:
            logger.debug("Inspection deferred check failed: %s", e)
        try:
            findings.extend(self._check_notifications(now))
        except Exception as e:
            logger.debug("Inspection notifications check failed: %s", e)

        # 只保留通过去重的命中
        triggers = [t for t in findings if self._allow_report(t[0], now)]
        self._last_findings = [t[1] for t in triggers]
        self._primed = True
        if triggers:
            logger.info("Inspection triggers: %d 条（%s）", len(triggers),
                        "；".join(t[1] for t in triggers[:3]))
        return [t[1] for t in triggers]

    def format_for_prompt(self) -> str:
        """最近一次巡检命中注入 LLM prompt（无命中返回空串）。"""
        if not self._last_findings:
            return ""
        lines = ["[任务巡检]"]
        for text in self._last_findings:
            lines.append(f"- {text}")
        return "\n".join(lines)

    def reset(self) -> None:
        """清空节流与去重状态（测试/手动触发用）。"""
        self._last_tick_at = 0.0
        self._reported.clear()
        self._last_findings = []
        self._run_cursors.clear()
        self._seen_deferred.clear()
        self._seen_notify.clear()

    # ── 命中检测 ──────────────────────────────────────────

    def _check_cron_runs(self, now: float, priming: bool = False) -> list[tuple[str, str]]:
        """监控 cron-runs/*.jsonl 新增行；status=success → 完成，failed/error → 失败。

        studio 运行记录文件名即 job id（JSONL 行内不一定含 jobId），
        且只播报属于当前 agent 的任务（过滤不在 job_map 中的记录）。

        Args:
            now: 当前时间（注入用）。
            priming: 是否为本实例首个 tick——仅此时跳过当时已存在的所有历史行，
                避免重启后回放旧 run；其后才出现的文件（cursor 仍为 None）从头处理新增行。

        Returns:
            [(dedup_key, 触发文案), ...]
        """
        if not self._agent_id:
            return []
        runs_dir = self._get_runs_dir()
        if not runs_dir or not runs_dir.is_dir():
            return []
        # 构建 job id -> label 查表（来自 cron-jobs.json）
        job_map = self._build_job_label_map()
        hits: list[tuple[str, str]] = []
        for jsonl in sorted(runs_dir.glob("*.jsonl")):
            key = str(jsonl)
            cursor = self._run_cursors.get(key)
            try:
                lines = jsonl.read_text("utf-8").splitlines()
            except Exception as e:
                logger.debug("读取 %s 失败: %s", jsonl, e)
                continue

            # 首个 tick：跳过当时已有历史（游标置文件末尾）；其后出现的文件从头处理
            if cursor is None:
                if priming:
                    self._run_cursors[key] = len(lines)
                    continue
                cursor = 0
            new_lines = lines[cursor:]
            self._run_cursors[key] = len(lines)
            if not new_lines:
                continue
            for raw in new_lines:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except Exception:
                    continue
                if not isinstance(obj, dict):
                    continue
                status = str(obj.get("status") or "").lower()
                jid = self._job_id_of(obj, jsonl.name)
                # 只播报当前 agent 的任务（防止读到其他 agent 的运行记录）
                if jid not in job_map:
                    continue
                label = job_map[jid]
                if status == "success":
                    text = f"【任务】{label}跑完了 ✅"
                    dedup = f"cron:{jid}:success"
                elif status in ("failed", "error"):
                    text = f"【任务】{label}跑失败了 ❌"
                    dedup = f"cron:{jid}:failed"
                else:
                    continue
                hits.append((dedup, text))
        return hits

    def _check_deferred(self, now: float) -> list[tuple[str, str]]:
        """deferred-tasks.json 新增 status=pending → 通知「有 N 个延迟任务待处理」。"""
        path = _schedule_mod.HANAKO_HOME / ".ephemeral" / "deferred-tasks.json"
        data = self._read_json(path)
        if not isinstance(data, dict) or not data:
            return []
        pending_keys: list[str] = []
        for key, task in data.items():
            if not isinstance(task, dict):
                continue
            if str(task.get("status") or "") == "pending":
                pending_keys.append(str(key))
        if not pending_keys:
            return []
        new_pending = [k for k in pending_keys if k not in self._seen_deferred]
        if not new_pending:
            return []
        # 只标记「新增」的为已见，已解决又再次出现者仍会被再次播报
        self._seen_deferred.update(new_pending)
        # 2026-09-17：报**增量**，不报总数。
        #
        # 原实现：检测的是 new_pending（新增），却报 len(pending_keys)（总数）——
        # 检测与文案不自洽；且与 SchedulePerception.format_for_prompt() 的
        # 「有 N 个任务待处理」撞车（同一个 context 块里两条说同一件事）。
        # 现在分工明确：
        #   schedule  → 当前状态（每轮都注入）
        #   inspection → 事件（新增才报，30 分钟节流）
        count = len(new_pending)
        # 节流 key 用 pending（同一批 pending 30 分钟内不重复）
        return [("pending", f"新增 {count} 个延迟任务")]

    def _check_notifications(self, now: float) -> list[tuple[str, str]]:
        """（可选）notifications.json 新增条目 → 播报其文案；文件不存在则跳过。"""
        path = _schedule_mod.HANAKO_HOME / ".ephemeral" / "notifications.json"
        if not path.exists():
            return []
        data = self._read_json(path)
        if not isinstance(data, list) or not data:
            return []
        hits: list[tuple[str, str]] = []
        for idx, item in enumerate(data):
            if isinstance(item, dict):
                nid = str(item.get("id") or item.get("key") or idx)
                msg = (item.get("message") or item.get("text") or item.get("content")
                       or item.get("title") or "")
            elif isinstance(item, str):
                nid = str(idx)
                msg = item
            else:
                continue
            msg = str(msg).strip()
            if not msg:
                continue
            if nid in self._seen_notify:
                continue
            self._seen_notify.add(nid)
            hits.append((f"notify:{nid}", msg))
        return hits

    # ── 辅助 ──────────────────────────────────────────────

    def _get_runs_dir(self) -> Path | None:
        """cron-runs 目录：优先 studio 空间，兜底 agent 目录。"""
        studio_id = _schedule_mod._get_default_studio_id()
        if studio_id:
            studio_dir = (
                _schedule_mod.HANAKO_HOME / "studios" / studio_id / "desk" / "cron-runs"
            )
            if studio_dir.is_dir():
                return studio_dir
        agent_dir = (
            _schedule_mod.HANAKO_HOME / "agents" / self._agent_id / "desk" / "cron-runs"
        )
        return agent_dir if agent_dir.is_dir() else None

    def _build_job_label_map(self) -> dict[str, str]:
        """job id -> label（来自 cron-jobs.json）。"""
        mapping: dict[str, str] = {}
        try:
            for job in self._schedule.get_cron_jobs():
                jid = str(job.get("id") or "")
                label = str(job.get("label") or "").strip()
                if jid and label:
                    mapping[jid] = label
        except Exception:
            logger.debug("inspection: 非致命异常(已静默吞掉)", exc_info=True)
        return mapping

    @staticmethod
    def _job_id_of(obj: dict, filename: str | None = None) -> str:
        """从 cron-runs 行提取 job id（兼容多种字段名）； studio 文件名即 job id。"""
        for f in ("jobId", "job_id", "id", "name"):
            v = obj.get(f)
            if v:
                return str(v)
        if filename:
            # studio 运行记录命名：studio_job_22.jsonl -> studio_job_22
            stem = Path(filename).stem
            if stem:
                return stem
        return "unknown"

    @staticmethod
    def _resolve_label(obj: dict, job_map: dict[str, str]) -> str:
        """解析 label：优先行内 label，否则用 job id 查 cron-jobs 表。"""
        inline = obj.get("label")
        if inline:
            return str(inline).strip() or "未知任务"
        jid = InspectionPerception._job_id_of(obj)
        if jid in job_map:
            return job_map[jid]
        return "未知任务"

    @staticmethod
    def _read_json(path: Path):
        """安全读 JSON；文件缺失/损坏返回 None。"""
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text("utf-8"))
        except Exception as e:
            logger.debug("读取 %s 失败: %s", path, e)
            return None

    def _allow_report(self, key: str, now: float) -> bool:
        """同一 key 在 REPORT_REPEAT_SECONDS 内只通知一次。"""
        last = self._reported.get(key)
        if last is not None and now - last < REPORT_REPEAT_SECONDS:
            return False
        self._reported[key] = now
        return True
