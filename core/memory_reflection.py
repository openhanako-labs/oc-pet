# -*- coding: utf-8 -*-
"""ReflectionEngine — 反思/摘要引擎（P1-3，N.E.K.O. memory/reflection/ + refine.py 参考重写）

把事件流（``<agent_id>_events.jsonl``，EventStream）周期性压缩成"反思条目"
（insight：观察 observation / 结论 conclusion / 置信度 confidence），存
``~/.oc-pet/memory/<agent_id>_reflections.json``。

设计（对齐迁移文档 P1-3 + 验收思路）：
- **触发**：``maybe_reflect()`` 按 ``config memory.reflection.interval_hours``（默认 24h）
  周期判断是否到期；``trigger_reflection()`` 手动强制触发。事件不足
  （``min_events`` 默认 5）或 LLM 失败时跳过并记日志，**不推进 last_reflect_at**
  （下次周期再试）；失败后退避 ``retry_minutes``（默认 60）避免每小时空打 LLM。
- **摘要压缩**：取最近 ``max_events``（默认 200）条事件渲染成文本 → Hanako LLM
  （source="memory_reflect"）→ JSON insight 列表 → 入库。
- **隐私**：``source="vision"`` 事件在事件流里已不落文本（EventStream 双保险），
  反思渲染时再排除一次；prompt 只带 topic/分类/情绪，不注入原始视觉内容。
- **LLM 不可用/失败 → 跳过 + 记日志**，不阻塞调用方。
- **线程约束（0x8001010D 教训）**：``schedule_reflect()`` 在后台线程执行 LLM，
  结果经 Qt Signal 回主线程通知 UI；无 Qt 环境（headless 单测）用同步
  ``maybe_reflect()``。
- **持久化**：``{"version": 2, "meta": {"last_reflect_at", ...}, "reflections": [...]}``；
  旧文件缺字段读取时自动归一，不崩。

用法：
    engine = ReflectionEngine("miku", event_source=event_stream)
    engine.maybe_reflect()          # 周期判断（由 pet tick 调用）
    engine.trigger_reflection()     # 手动强制
    insights = engine.get_reflections(limit=10)
"""
from __future__ import annotations

import json
import logging
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

DEFAULT_MEMORY_DIR = Path.home() / ".oc-pet" / "memory"

REFLECTIONS_FILE_VERSION = 2
SOURCE_MEMORY_REFLECT = "memory_reflect"

# 反思条目字段上限
REFLECTION_MAX_TEXT_CHARS = 300
REFLECTION_EVIDENCE_LIMIT = 3

# 触发参数（可由 config 覆盖）
DEFAULT_INTERVAL_HOURS = 24.0
DEFAULT_RETRY_MINUTES = 60
DEFAULT_MAX_EVENTS = 200
DEFAULT_MIN_EVENTS = 5

# 终态：不再参与后续反思/展示过滤
REFLECTION_TERMINAL_STATUSES = frozenset({"promoted", "denied", "archived", "merged"})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_reflection_id() -> str:
    return f"r_{uuid.uuid4().hex[:10]}"


def _iso_to_ts(iso: str | None, fallback: float = 0.0) -> float:
    if not iso:
        return fallback
    try:
        parsed = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        return parsed.timestamp()
    except (ValueError, TypeError, OverflowError):
        return fallback


def _fmt_ts(ts: float) -> str:
    """epoch 秒 → "MM-DD HH:MM"；0/非法 → 空串。"""
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return ""


# ── LLM 反思 prompt / 解析 ───────────────────────────────────────────


def build_reflect_prompt(events_rendered: str) -> str:
    """构造反思 LLM 指令（事件流 → JSON insight 数组）。"""
    lines = [
        "你是记忆反思引擎。根据以下事件流，提炼出 1~4 条值得记住的洞察（insight）。",
        "只输出 JSON 数组，不要输出解释或多余文字。每个元素格式：",
        '{"observation": "观察到的现象（一句话）", "conclusion": "得出的结论/推断（一句话）", "confidence": 0到1的小数, "category": "工作/生活/偏好/关系/健康/其他 之一"}。',
        "要求：结论必须能从事件推出，不要编造；confidence 反映证据充分程度（证据越足越高）。",
        "事件流：",
        events_rendered,
    ]
    return "\n".join(lines)


def parse_reflection_insights(raw: str) -> list[dict]:
    """解析 LLM 输出的 JSON 数组 → insight dict 列表（防御式）。

    Returns:
        [{"observation", "conclusion", "confidence", "category"}]；无有效内容 → []。
    """
    if not raw or not str(raw).strip():
        return []
    raw = str(raw).strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    data: Any = None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("[")
        end = raw.rfind("]")
        if start != -1 and end > start:
            try:
                data = json.loads(raw[start:end + 1])
            except json.JSONDecodeError:
                return []
        else:
            return []
    if not isinstance(data, list):
        return []
    insights: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        observation = str(item.get("observation") or "").strip()
        conclusion = str(item.get("conclusion") or "").strip()
        if not conclusion and not observation:
            continue
        if not observation:
            observation = conclusion
        if len(conclusion) > REFLECTION_MAX_TEXT_CHARS:
            conclusion = conclusion[:REFLECTION_MAX_TEXT_CHARS - 1] + "…"
        try:
            confidence = float(item.get("confidence", 0.6) or 0.6)
        except (TypeError, ValueError):
            confidence = 0.6
        confidence = max(0.0, min(1.0, confidence))
        category = str(item.get("category") or "其他").strip() or "其他"
        insights.append({
            "observation": observation,
            "conclusion": conclusion,
            "confidence": confidence,
            "category": category,
        })
    return insights


# ── ReflectionEngine ─────────────────────────────────────────────────


class ReflectionEngine:
    """反思/摘要引擎：事件流 → LLM 摘要压缩 → insight 条目持久化。

    Args:
        agent_id: 桌宠/角色 id（反思文件按 agent 分实例）
        memory_dir: 记忆目录；缺省 ~/.oc-pet/memory
        filename: 覆盖文件名（默认 ``<agent_id>_reflections.json``）
        llm_fn: 可选注入的同步反思函数 ``fn(prompt: str) -> str | None``
            （单测/无适配器环境）
        adapter: HanakoPetAdapter（或实现 ``chat(message, inject_memory,
            extra_context, source) -> (text, emotion)`` 的对象）
        event_source: EventStream（有 ``read_all()``）或 ``Callable[[], list[dict]]``；
            None 时反思永远跳过（事件不足）
        interval_hours: 反思周期（小时；默认 24）
        use_qt_bridge: 后台线程结果是否经 Qt Signal 回主线程（headless 关闭）
    """

    def __init__(
        self,
        agent_id: str = "default",
        memory_dir: str | Path | None = None,
        filename: str | None = None,
        llm_fn: Callable[[str], str | None] | None = None,
        adapter: Any = None,
        event_source: Any = None,
        interval_hours: float | None = None,
        min_events: int | None = None,
        max_events: int | None = None,
        retry_minutes: float | None = None,
        use_qt_bridge: bool = True,
    ):
        self._agent_id = agent_id or "default"
        self._dir = Path(memory_dir) if memory_dir else DEFAULT_MEMORY_DIR
        self._path = self._dir / (filename or f"{agent_id}_reflections.json")
        self._llm_fn = llm_fn
        self._adapter = adapter
        self._event_source = event_source
        # 参数缺省时从配置读取（memory.reflection.*）；再缺省用模块常量
        if (interval_hours is None or min_events is None
                or max_events is None or retry_minutes is None):
            try:
                from config import load_config
                refl_cfg = load_config().get("memory", {}).get("reflection", {})
                if interval_hours is None:
                    interval_hours = refl_cfg.get("interval_hours") or DEFAULT_INTERVAL_HOURS
                if min_events is None:
                    min_events = refl_cfg.get("min_events") or DEFAULT_MIN_EVENTS
                if max_events is None:
                    max_events = refl_cfg.get("max_events") or DEFAULT_MAX_EVENTS
                if retry_minutes is None:
                    retry_minutes = refl_cfg.get("retry_minutes") or DEFAULT_RETRY_MINUTES
            except Exception:
                logger.debug("memory_reflection: 非致命异常(已静默吞掉)", exc_info=True)
        self._interval_hours = float(interval_hours) if interval_hours else DEFAULT_INTERVAL_HOURS
        self._min_events = int(min_events) if min_events else DEFAULT_MIN_EVENTS
        self._max_events = int(max_events) if max_events else DEFAULT_MAX_EVENTS
        self._retry_minutes = float(retry_minutes) if retry_minutes else DEFAULT_RETRY_MINUTES
        self._lock = threading.Lock()
        self._reflections: list[dict] = []
        self._meta: dict = {}
        self._changed_callbacks: list[Callable[[dict], None]] = []
        self._bridge = None
        if use_qt_bridge:
            self._setup_bridge()
        self.load()

    # ── Qt 桥（后台 LLM → 主线程通知；0x8001010D 约束）──────────────

    def _setup_bridge(self) -> None:
        try:
            from PySide6.QtCore import QObject, Signal

            class _Bridge(QObject):
                reflected = Signal(str)  # JSON payload：maybe_reflect 结果

            self._bridge = _Bridge()
            self._bridge.reflected.connect(self._on_reflected)
        except Exception as exc:  # 无 Qt / 无 PySide6 → 同步路径
            logger.debug("ReflectionEngine Qt bridge unavailable, use sync path: %s", exc)
            self._bridge = None

    def _on_reflected(self, payload: str) -> None:
        """主线程槽：解析后台反思结果并通知监听者。"""
        try:
            result = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            result = {"triggered": False, "added": 0, "reason": "unknown"}
        self._notify_changed(result)

    def set_changed_callback(self, callback: Callable[[dict], None] | None) -> None:
        """注册反思结果通知（主线程回调；result: {"triggered","added","reason"}）。"""
        if callback is None:
            self._changed_callbacks = []
            return
        if callback not in self._changed_callbacks:
            self._changed_callbacks.append(callback)

    def _notify_changed(self, result: dict) -> None:
        for cb in list(self._changed_callbacks):
            try:
                cb(result)
            except Exception as exc:  # 通知失败不影响主链路
                logger.error("ReflectionEngine changed callback error: %s", exc)

    def shutdown(self) -> None:
        """清理回调引用（进程退出时调用）。"""
        self._changed_callbacks = []

    # ── 路径/属性 ──

    @property
    def path(self) -> Path:
        return self._path

    @property
    def agent_id(self) -> str:
        return self._agent_id

    @property
    def reflections(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._reflections]

    @property
    def meta(self) -> dict:
        return dict(self._meta)

    @property
    def last_reflect_at(self) -> str:
        return str(self._meta.get("last_reflect_at") or "")

    # ── 持久化 ──

    def load(self) -> None:
        """从磁盘加载；文件不存在/损坏 → 空档（不崩）。"""
        try:
            if not self._path.exists():
                self._reflections = []
                self._meta = {}
                return
            data = json.loads(self._path.read_text(encoding="utf-8"))
            self._load_data(data)
        except Exception as exc:
            logger.warning("ReflectionEngine 加载失败（用空档）: %s", exc)
            self._reflections = []
            self._meta = {}

    def _load_data(self, data: Any) -> None:
        """兼容 v1（裸数组）与 v2（{"reflections": [...]}）两种格式。"""
        if isinstance(data, list):
            raw_reflections = data
            self._meta = {}
        elif isinstance(data, dict):
            self._meta = dict(data.get("meta") or {})
            raw_reflections = data.get("reflections") or []
        else:
            raw_reflections = []
        entries: list[dict] = []
        for r in raw_reflections:
            if not isinstance(r, dict):
                continue
            entry = self._normalize_entry(r)
            if not entry.get("conclusion") and not entry.get("observation"):
                continue
            entries.append(entry)
        self._reflections = entries

    @staticmethod
    def _normalize_entry(r: dict) -> dict:
        """旧文件兼容：缺字段补默认值。"""
        entry = dict(r)
        entry.setdefault("id", _new_reflection_id())
        entry.setdefault("type", "reflection")
        entry.setdefault("observation", "")
        entry.setdefault("conclusion", "")
        entry.setdefault("confidence", 0.6)
        entry.setdefault("category", "其他")
        entry.setdefault("status", "pending")
        entry.setdefault("created_at", entry.get("created_at") or _now_iso())
        entry.setdefault("created_ts", _iso_to_ts(entry.get("created_at"), 0.0))
        entry.setdefault("period_start_ts", 0.0)
        entry.setdefault("period_end_ts", 0.0)
        entry.setdefault("source_event_count", 0)
        entry.setdefault("evidence", [])
        return entry

    def save(self) -> bool:
        """显式写盘（幂等）。"""
        with self._lock:
            return self._save_locked()

    def _save_locked(self) -> bool:
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            data = {
                "version": REFLECTIONS_FILE_VERSION,
                "agent_id": self._agent_id,
                "updated_at": _now_iso(),
                "meta": self._meta,
                "reflections": self._reflections,
            }
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self._path)
            return True
        except Exception as exc:
            logger.warning("ReflectionEngine 保存失败: %s", exc)
            return False

    # ── 触发 ──

    def maybe_reflect(self, now: datetime | None = None, force: bool = False) -> dict:
        """按配置周期判断并执行反思（同步路径；headless 单测 / 手动调用）。

        Args:
            now: 当前时间（测试注入）；缺省取真实时间
            force: True 时忽略周期与最小事件数限制（手动触发）

        Returns:
            {"triggered": 是否执行了反思流程, "added": 新增 insight 数,
             "reason": not_due/retry_backoff/not_enough_events/llm_empty_or_failed/ok}
        """
        now = now or datetime.now(timezone.utc)
        if not force:
            last_at = self._meta.get("last_reflect_at")
            if last_at:
                try:
                    last = datetime.fromisoformat(str(last_at).replace("Z", "+00:00"))
                    if last.tzinfo is None:
                        last = last.replace(tzinfo=timezone.utc)
                    if (now - last).total_seconds() < self._interval_hours * 3600:
                        return {"triggered": False, "added": 0, "reason": "not_due"}
                except (ValueError, TypeError):
                    logger.debug("memory_reflection: 非致命异常(已静默吞掉)", exc_info=True)
            attempt_at = self._meta.get("last_attempt_at")
            if attempt_at:
                try:
                    attempt = datetime.fromisoformat(str(attempt_at).replace("Z", "+00:00"))
                    if attempt.tzinfo is None:
                        attempt = attempt.replace(tzinfo=timezone.utc)
                    if (now - attempt).total_seconds() < self._retry_minutes * 60:
                        return {"triggered": False, "added": 0, "reason": "retry_backoff"}
                except (ValueError, TypeError):
                    logger.debug("memory_reflection: 非致命异常(已静默吞掉)", exc_info=True)
        return self._run_reflection(now, force)

    def trigger_reflection(self) -> dict:
        """手动强制触发一次反思（忽略周期与最小事件数限制）。"""
        return self.maybe_reflect(force=True)

    def schedule_reflect(self, force: bool = False) -> bool:
        """异步：后台线程执行反思，结果经 Qt 信号回主线程通知。

        Returns:
            True=已调度后台线程；False=退化为同步执行（headless）。
        """
        if self._bridge is None:
            self.maybe_reflect(force=force)
            return False
        threading.Thread(
            target=self._reflect_worker,
            args=(force,),
            daemon=True,
            name="memory-reflect",
        ).start()
        return True

    def _reflect_worker(self, force: bool) -> None:
        """后台线程：执行反思（文件 I/O + LLM，安全），结果经信号回主线程。"""
        try:
            result = self.maybe_reflect(force=force)
        except Exception as exc:  # 防御：后台绝不抛到线程外
            logger.warning("[Reflection] 后台反思异常: %s", exc)
            result = {"triggered": False, "added": 0, "reason": "error"}
        self._bridge.reflected.emit(json.dumps(result, ensure_ascii=False))

    # ── 内部执行 ──

    def _run_reflection(self, now: datetime, force: bool) -> dict:
        """执行一次反思：取事件 → 渲染 → LLM 摘要 → 入库。"""
        events = self._gather_events()
        # 隐私：vision 事件在事件流已不落文本，这里再排除一次（双保险）
        events = [e for e in events if str(e.get("source") or "").strip() != "vision"]
        if not force and len(events) < self._min_events:
            logger.info("[Reflection] 事件不足（%d<%d），跳过反思", len(events), self._min_events)
            with self._lock:
                self._meta["last_attempt_at"] = _now_iso()
                self._save_locked()
            return {"triggered": True, "added": 0, "reason": "not_enough_events"}

        recent = events[-self._max_events:]
        period_start = float(recent[0].get("ts") or 0.0) if recent else 0.0
        period_end = float(recent[-1].get("ts") or 0.0) if recent else 0.0
        rendered = self._render_events(recent)

        insights = self._synthesize(rendered, period_start, period_end)
        if not insights:
            # LLM 失败/空 → 记尝试时间，不推进 last_reflect_at（下次周期再试）
            logger.info("[Reflection] LLM 反思为空/失败，跳过（不推进 last_reflect_at）")
            with self._lock:
                self._meta["last_attempt_at"] = _now_iso()
                self._save_locked()
            return {"triggered": True, "added": 0, "reason": "llm_empty_or_failed"}

        added = 0
        with self._lock:
            for ins in insights:
                self._reflections.append(
                    self._new_reflection(ins, recent, period_start, period_end, now)
                )
                added += 1
            self._meta["last_reflect_at"] = _now_iso()
            self._meta["last_attempt_at"] = _now_iso()
            self._meta["last_reflect_event_count"] = len(recent)
            self._save_locked()
        logger.info("[Reflection] 反思完成：新增 %d 条 insight（agent=%s）", added, self._agent_id)
        return {"triggered": True, "added": added, "reason": "ok"}

    def _gather_events(self) -> list[dict]:
        """从 event_source 读取事件（EventStream.read_all 或 callable）。"""
        if self._event_source is None:
            return []
        try:
            if callable(self._event_source):
                events = self._event_source()
            else:
                events = self._event_source.read_all()
            return [dict(e) for e in (events or []) if isinstance(e, dict)]
        except Exception as exc:
            logger.warning("[Reflection] 读取事件流失败: %s", exc)
            return []

    @staticmethod
    def _render_events(events: list[dict]) -> str:
        """把事件渲染成 prompt 文本行（分类/时间/话题/情绪；无原始视觉内容）。"""
        lines = []
        for e in events:
            time_str = _fmt_ts(e.get("ts"))
            category = str(e.get("category") or "")
            scenario = str(e.get("scenario") or "")
            where = " / ".join(x for x in (category, scenario) if x)
            topic = str(e.get("topic") or "")
            text = topic or "(无文本记录)"
            line = f"[{time_str}] {where} | {text}"
            emotion = str(e.get("emotion") or "")
            if emotion and emotion != "neutral":
                intensity = e.get("intensity") or 0.0
                line += f"（情绪 {emotion}:{intensity}）"
            lines.append(line)
        return "\n".join(lines)

    def _synthesize(self, rendered: str, period_start: float, period_end: float) -> list[dict]:
        """LLM 摘要压缩 → insight 列表；失败/无通道 → []（跳过 + 记日志）。"""
        if self._llm_fn is not None:
            try:
                raw = self._llm_fn(build_reflect_prompt(rendered))
                return parse_reflection_insights(raw or "")
            except Exception as exc:
                logger.warning("[Reflection] LLM 反思失败（跳过）: %s", exc)
                return []
        if self._adapter is None:
            logger.info("[Reflection] 无 LLM 通道，跳过反思")
            return []
        prompt = build_reflect_prompt(rendered)
        extra = f"事件区间：{_fmt_ts(period_start)} ~ {_fmt_ts(period_end)}"
        try:
            reply, _emotion = self._adapter.chat(
                prompt,
                inject_memory=False,
                extra_context=extra,
                source=SOURCE_MEMORY_REFLECT,
            )
        except Exception as exc:
            logger.warning("[Reflection] LLM 反思异常（跳过）: %s", exc)
            return []
        return parse_reflection_insights(reply or "")

    def _new_reflection(self, ins: dict, events: list[dict],
                        period_start: float, period_end: float, now: datetime) -> dict:
        """由 insight 构造反思条目（含证据引用）。"""
        evidence = []
        for e in events[-REFLECTION_EVIDENCE_LIMIT:]:
            evidence.append({
                "event_id": str(e.get("event_id") or ""),
                "ts": float(e.get("ts") or 0.0),
                "source": str(e.get("source") or ""),
                "quote": str(e.get("topic") or "")[:80],
            })
        return {
            "id": _new_reflection_id(),
            "type": "reflection",
            "observation": ins.get("observation", ""),
            "conclusion": ins.get("conclusion", ""),
            "confidence": float(ins.get("confidence", 0.6) or 0.6),
            "category": ins.get("category", "其他"),
            "status": "pending",
            "created_at": _now_iso(),
            "created_ts": now.timestamp(),
            "period_start_ts": float(period_start or 0.0),
            "period_end_ts": float(period_end or 0.0),
            "source_event_count": len(events),
            "evidence": evidence,
        }

    # ── 查询接口 ──

    def get_reflections(self, status: str | None = None, limit: int = 50) -> list[dict]:
        """取反思条目（可按状态过滤，最新在前）。"""
        hits = list(self._reflections)
        if status:
            hits = [r for r in hits if r.get("status") == status]
        hits.sort(key=lambda r: -float(r.get("created_ts", 0.0)))
        return hits[:limit]

    def search(self, query: str, limit: int = 10) -> list[dict]:
        """按关键词检索反思（观察/结论/分类 子串匹配，最新在前）。"""
        if not query:
            return []
        q = str(query).strip().lower()
        hits = []
        for r in self._reflections:
            hay = " ".join(str(x) for x in (
                r.get("observation"), r.get("conclusion"), r.get("category")))
            if q and q in hay.lower():
                hits.append(r)
        hits.sort(key=lambda r: -float(r.get("created_ts", 0.0)))
        return hits[:limit]

    def by_category(self, category: str, limit: int = 10) -> list[dict]:
        """按分类过滤反思条目。"""
        if not category:
            return []
        cat_l = str(category).strip().lower()
        hits = [r for r in self._reflections if cat_l in str(r.get("category") or "").lower()]
        hits.sort(key=lambda r: -float(r.get("created_ts", 0.0)))
        return hits[:limit]

    def by_confidence(self, min_confidence: float = 0.0, limit: int = 50) -> list[dict]:
        """按置信度过滤（≥min_confidence，倒序）。"""
        try:
            min_conf = float(min_confidence or 0.0)
        except (TypeError, ValueError):
            min_conf = 0.0
        hits = [r for r in self._reflections
                if float(r.get("confidence", 0.0) or 0.0) >= min_conf]
        hits.sort(key=lambda r: -float(r.get("confidence", 0.0)))
        return hits[:limit]

    def stats(self) -> dict:
        """统计：总数 + 按状态分布。"""
        counts: dict[str, int] = {}
        for r in self._reflections:
            s = r.get("status") or "pending"
            counts[s] = counts.get(s, 0) + 1
        return {
            "total": len(self._reflections),
            "by_status": counts,
            "last_reflect_at": self._meta.get("last_reflect_at", ""),
        }


__all__ = [
    "ReflectionEngine",
    "DEFAULT_MEMORY_DIR",
    "SOURCE_MEMORY_REFLECT",
    "DEFAULT_INTERVAL_HOURS",
    "DEFAULT_MAX_EVENTS",
    "DEFAULT_MIN_EVENTS",
    "REFLECTION_TERMINAL_STATUSES",
    "build_reflect_prompt",
    "parse_reflection_insights",
]
