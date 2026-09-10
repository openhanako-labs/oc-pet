"""日程感知 — 读取 Hanako 自动化任务

数据源（BugFix #5-C：原实现读 ~/.hanako/.ephemeral/automation*.json，该文件
不存在 → 感知永远空转；BugFix #8：UI 任务计划真实存储在 studio 空间，
agents/<id>/desk/cron-jobs.json 已弃用/为空）：
  1. ~/.hanako/studios/<space_id>/desk/cron-jobs.json   — UI「任务计划」真实来源
     按 actorAgentId 过滤当前 agent 的任务；空间 ID 从 spaces.json 的
     defaultSpaceId 读取。
  2. ~/.hanako/agents/<agent_id>/desk/cron-jobs.json   — 兜底（legacy）
  3. ~/.hanako/.ephemeral/deferred-tasks.json           — 延迟任务 dict
  4. ~/.hanako/.ephemeral/plugin-tasks.json             — 插件任务 {tasks, schedules}

refresh() 把四处数据归一化为单列表（每项带 kind 字段），
format_for_prompt() 只输出 enabled 的 cron + pending 的 deferred + 非空
plugin schedules。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

HANAKO_HOME = Path.home() / ".hanako"


def _get_default_studio_id(hanako_home: Path | None = None) -> str | None:
    """从 ~/.hanako/spaces.json 读取 defaultSpaceId（工作室空间 ID）。"""
    if hanako_home is None:
        hanako_home = HANAKO_HOME
    path = hanako_home / "spaces.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text("utf-8"))
        sid = str(data.get("defaultSpaceId") or "").strip()
        return sid or None
    except Exception as e:
        logger.debug("读取 spaces.json 失败: %s", e)
        return None


def _parse_iso(ts: str) -> float | None:
    """把 ISO8601 时间字符串转 epoch 秒（供本地时区格式化/比较；失败返回 None）。"""
    if not ts:
        return None
    try:
        s = str(ts).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is not None:
            return dt.timestamp()
        return dt.timestamp()
    except Exception:
        return None


class SchedulePerception:
    """日程感知 - 读取 Hanako 自动化任务（cron / deferred / plugin-schedule）"""

    def __init__(self, agent_id: str = ""):
        self._agent_id = (agent_id or "").strip()
        self._automations: list[dict] = []

    # ── 刷新 ──────────────────────────────────────────────

    def refresh(self) -> None:
        """重读三处数据源，归一化为 _automations 列表。"""
        self._automations = []
        # 2026-09-10：三处失败原本均 logger.debug（INFO 不可见）。
        # 后果 = 日程提醒静默丢失，用户以为没设成功。
        for name, reader in (
            ("cron", self._read_cron_jobs),
            ("deferred", self._read_deferred_tasks),
            ("plugin", self._read_plugin_tasks),
        ):
            try:
                self._automations.extend(reader())
            except Exception as e:
                logger.warning(
                    "Schedule %s 数据源刷新失败，该类提醒本次不会触发: %s", name, e,
                )

    # ── 数据源 ────────────────────────────────────────────

    def _read_cron_jobs(self) -> list[dict]:
        """读 cron-jobs.json：优先 studio 空间（UI 真实来源），兜底 agent 目录。"""
        if not self._agent_id:
            return []

        # 主数据源：studio 空间的 cron-jobs.json
        studio_id = _get_default_studio_id()
        if studio_id:
            studio_path = HANAKO_HOME / "studios" / studio_id / "desk" / "cron-jobs.json"
            studio_items = self._read_cron_jobs_from_path(
                studio_path, filter_agent_id=self._agent_id
            )
            if studio_items:
                return studio_items

        # 兜底：agent 自身目录（legacy / 未迁移到 studio 的任务）
        agent_path = HANAKO_HOME / "agents" / self._agent_id / "desk" / "cron-jobs.json"
        return self._read_cron_jobs_from_path(agent_path)

    def _read_cron_jobs_from_path(
        self, path: Path, filter_agent_id: str | None = None
    ) -> list[dict]:
        """读取指定路径的 cron-jobs.json；studio 数据按 actorAgentId 过滤。"""
        data = self._read_json(path)
        jobs = data.get("jobs") if isinstance(data, dict) else None
        if not isinstance(jobs, list):
            return []
        items: list[dict] = []
        for job in jobs:
            if not isinstance(job, dict):
                continue
            if filter_agent_id:
                actor = str(job.get("actorAgentId") or "").strip()
                # actor 为空时保留（兼容性），否则必须匹配当前 agent
                if actor and actor != filter_agent_id:
                    continue
            label = str(job.get("label") or job.get("prompt") or "未知任务").strip()
            items.append({
                "kind": "cron",
                "id": str(job.get("id") or ""),
                "label": label[:40],
                "schedule": str(job.get("schedule") or ""),
                "enabled": bool(job.get("enabled", True)),
                "last_run_at": str(job.get("lastRunAt") or ""),
                "next_run_at": str(job.get("nextRunAt") or ""),
                "consecutive_errors": int(job.get("consecutiveErrors") or 0),
            })
        return items

    def _read_deferred_tasks(self) -> list[dict]:
        """读 ~/.hanako/.ephemeral/deferred-tasks.json（dict key=subagent id）。"""
        path = HANAKO_HOME / ".ephemeral" / "deferred-tasks.json"
        data = self._read_json(path)
        if not isinstance(data, dict):
            return []
        items: list[dict] = []
        for key, task in data.items():
            if not isinstance(task, dict):
                continue
            items.append({
                "kind": "deferred",
                "key": str(key),
                "status": str(task.get("status") or ""),
                "delivered": bool(task.get("delivered", False)),
                "session_id": str(task.get("sessionId") or ""),
            })
        return items

    def _read_plugin_tasks(self) -> list[dict]:
        """读 ~/.hanako/.ephemeral/plugin-tasks.json 的 schedules[]。"""
        path = HANAKO_HOME / ".ephemeral" / "plugin-tasks.json"
        data = self._read_json(path)
        if not isinstance(data, dict):
            return []
        schedules = data.get("schedules")
        if not isinstance(schedules, list):
            return []
        items: list[dict] = []
        for sched in schedules:
            if not isinstance(sched, dict):
                continue
            items.append({
                "kind": "plugin_schedule",
                "id": str(sched.get("id") or sched.get("taskId") or ""),
                "label": str(sched.get("label") or sched.get("name") or "插件任务"),
                "schedule": str(sched.get("schedule") or sched.get("cron") or ""),
            })
        return items

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

    # ── 查询 ──────────────────────────────────────────────

    def get_cron_jobs(self) -> list[dict]:
        """全部 cron 任务（含 disabled；巡检用）。"""
        return [i for i in self._automations if i.get("kind") == "cron"]

    def get_upcoming(self, max_items: int = 3) -> list[dict]:
        """按 next_run_at 升序取前 N 条 enabled cron 任务（兼容旧调用方）。"""
        crons = [i for i in self._automations
                 if i.get("kind") == "cron" and i.get("enabled")]
        crons.sort(key=lambda i: _parse_iso(i.get("next_run_at")) or 0)
        return crons[:max_items]

    def get_pending_deferred(self) -> list[dict]:
        """status == pending 的延迟任务。"""
        return [i for i in self._automations
                if i.get("kind") == "deferred" and i.get("status") == "pending"]

    def get_plugin_schedules(self) -> list[dict]:
        """插件 schedules（非空时列入 prompt）。"""
        return [i for i in self._automations if i.get("kind") == "plugin_schedule"]

    # ── prompt 注入 ───────────────────────────────────────

    def format_for_prompt(self) -> str:
        """输出注入 LLM prompt 的日程上下文（空则返回空串）。"""
        parts: list[str] = []

        crons = self.get_upcoming(max_items=10)
        if crons:
            lines = ["[即将到来的定时任务]"]
            for item in crons:
                label = item.get("label", "未知")
                schedule = item.get("schedule", "")
                next_ts = _parse_iso(item.get("next_run_at"))
                hhmm = datetime.fromtimestamp(next_ts).strftime("%H:%M") if next_ts else "待定"
                lines.append(f"- {label}（{schedule}，下次 {hhmm}）")
            parts.append("\n".join(lines))

        pending = self.get_pending_deferred()
        if pending:
            lines = ["[延迟任务]"]
            for item in pending:
                sid = item.get("session_id", "")
                tail = sid[-8:] if sid else item.get("key", "")[-8:]
                lines.append(f"- [延迟任务] {tail}（pending）")
            parts.append("\n".join(lines))

        plugin_scheds = self.get_plugin_schedules()
        if plugin_scheds:
            lines = ["[插件定时]"]
            for item in plugin_scheds:
                lines.append(f"- {item.get('label')}（{item.get('schedule')}）")
            parts.append("\n".join(lines))

        return "\n".join(parts)
