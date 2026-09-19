"""A2A：把活派给 Hana 的 agent（报告 G2 / A4）。

## 先破一个我自己的错判

2026-09-19 上午我写「**建议不做**：Hana 已有 7 个 agent 与完整委派体系，
oc-pet 再自研一层是重复建设」。**这个判断是错的**——错的不是方向，是取证：
我说"重复建设"时，**没去看 oc-pet 里到底已经有什么**。实际是：

    core/hanako_session_manager.py
        create_session(agent_id=...)   → POST /api/sessions/new（可指定 agent）
        send_and_wait(session, text)   → WS 发话 + 流式聚合 + 超时/可恢复
        list_sessions(agent_id=...)    → 读 manifest DB（带 agentId）
    core/hana_catalog.py               → 读得到那 7 个 agent

所以 A2A **不是"造一层运行时"，而是"把已有的三块拼起来 + 加边界"**——一层胶水。

## 边界比功能重要

派活 = 花你的 token + 在 Hana 里留下会话。所以默认**关**，并叠五道护栏：

1. ``enabled``（默认 **false**）——不开口就不动。
2. ``allowed_agents``（默认**空 = 一个都不许**）——**绝不自动挑 agent**。
   宁可派不出去，也不能替用户决定"这事该找谁"。
3. ``max_per_hour`` / ``max_per_day``（默认 6 / 30）——滑动窗口硬上限。
4. **一律新建会话**，绝不往用户已有会话里插话（不污染他的对话历史）。
5. 结果**不自动念出来**：先落结果队列，由上层决定要不要说。
   （避免"它突然讲了一段别人的结论"。）

## 本模块的边界

只做**策略 + 编排**，不直接碰 Hana——``create_session`` / ``send`` 由外部注入。
这样既能单测，也方便以后换通道（换成 MCP 工具或新接口都不用改这里）。
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

MAX_TASK_CHARS = 2000          # 派活文本上限（防有人把整篇文档塞进去）
HOUR = 3600.0
DAY = 86400.0

DEFAULT_MAX_PER_HOUR = 6
DEFAULT_MAX_PER_DAY = 30


@dataclass
class DelegateResult:
    """一次派活的结果。失败**绝不伪装成成功**（与 mc_bridge / hanako_bridge 同纪律）。"""

    ok: bool
    agent_id: str = ""
    session_id: str = ""
    session_path: str = ""
    reply: str = ""
    error: str = ""
    elapsed: float = 0.0

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "agent_id": self.agent_id,
            "session_id": self.session_id,
            "session_path": self.session_path,
            "reply": self.reply,
            "error": self.error,
            "elapsed": round(self.elapsed, 2),
        }


class Delegator:
    """把任务派给指定 agent，拿回结果。

    Args:
        create_session: ``(agent_id: str) -> object``，返回带
            ``session_id`` / ``session_path`` 的对象（或 dict）。
        send: ``(session, text: str, timeout: float) -> str``，返回回复文本。
        cfg: ``config.json`` 的 ``a2a`` 块。
        now: 取时间函数（测试注入）。
        on_result: 可选回调 ``(DelegateResult) -> None``（在调用线程执行）。
    """

    def __init__(
        self,
        create_session: Callable[[str], Any],
        send: Callable[..., str],
        cfg: Optional[dict] = None,
        now: Callable[[], float] = time.time,
        on_result: Optional[Callable[[DelegateResult], None]] = None,
    ):
        c = cfg or {}
        self._create_session = create_session
        self._send = send
        self._now = now
        self._on_result = on_result

        self._enabled = bool(c.get("enabled", False))
        raw = c.get("allowed_agents") or []
        if isinstance(raw, str):
            raw = [raw]
        self._allowed = [str(a).strip() for a in raw if str(a).strip()]
        self._max_hour = self._as_pos_int(c.get("max_per_hour"), DEFAULT_MAX_PER_HOUR)
        self._max_day = self._as_pos_int(c.get("max_per_day"), DEFAULT_MAX_PER_DAY)
        try:
            self._timeout = float(c.get("timeout_seconds", 180) or 180)
        except (TypeError, ValueError):
            self._timeout = 180.0

        self._lock = threading.Lock()
        self._spent: list[float] = []      # 派活时间戳（滑窗）
        self.results: list[DelegateResult] = []   # 结果队列（不自动念）

    # ── 配置视图 ──────────────────────────────────────────

    @staticmethod
    def _as_pos_int(value, default: int) -> int:
        try:
            v = int(value)
        except (TypeError, ValueError):
            return default
        return v if v > 0 else default

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def allowed_agents(self) -> list[str]:
        return list(self._allowed)

    def check(self, agent_id: str) -> tuple[bool, str]:
        """只判不做：``(是否允许, 拒因)``。拒因用于日志与气泡文案。"""
        if not self._enabled:
            return False, "a2a 未启用"
        agent = str(agent_id or "").strip()
        if not agent:
            return False, "没指定 agent（本模块绝不自动挑）"
        if agent not in self._allowed:
            return False, f"agent {agent} 不在白名单里"
        now = self._now()
        with self._lock:
            self._spent = [t for t in self._spent if now - t <= DAY]
            if sum(1 for t in self._spent if now - t <= HOUR) >= self._max_hour:
                return False, f"已达每小时上限（{self._max_hour}）"
            if len(self._spent) >= self._max_day:
                return False, f"已达每日上限（{self._max_day}）"
        return True, ""

    def stats(self) -> dict:
        now = self._now()
        with self._lock:
            last_hour = sum(1 for t in self._spent if now - t <= HOUR)
            last_day = sum(1 for t in self._spent if now - t <= DAY)
        return {
            "enabled": self._enabled,
            "allowed_agents": list(self._allowed),
            "used_last_hour": last_hour,
            "used_last_day": last_day,
            "max_per_hour": self._max_hour,
            "max_per_day": self._max_day,
        }

    # ── 派活 ──────────────────────────────────────────────

    def delegate(self, task: str, agent_id: str) -> DelegateResult:
        """同步派活（调用方负责放到线程里，别堵住主线程）。

        Returns:
            :class:`DelegateResult`；任何失败都返回 ``ok=False`` + ``error``，
            **不抛异常**（上层是对话主路径，不能被派活拖垮）。
        """
        text = (task or "").strip()
        if not text:
            return self._finish(DelegateResult(False, error="任务为空"))
        if len(text) > MAX_TASK_CHARS:
            return self._finish(
                DelegateResult(False, agent_id=agent_id,
                               error=f"任务过长（{len(text)} > {MAX_TASK_CHARS} 字）")
            )
        ok, why = self.check(agent_id)
        if not ok:
            return self._finish(DelegateResult(False, agent_id=agent_id, error=why))

        t0 = self._now()
        agent = str(agent_id).strip()
        session = None
        try:
            # 一律**新建**会话：绝不往用户已有会话里插话（护栏 4）
            session = self._create_session(agent)
            sid, spath = self._ids_of(session)
            if not sid:
                return self._finish(DelegateResult(
                    False, agent_id=agent, error="新建会话没拿到稳定标识"))
            reply = self._send(session, text, self._timeout) or ""
            with self._lock:
                self._spent.append(t0)
            return self._finish(DelegateResult(
                True, agent_id=agent, session_id=sid, session_path=spath,
                reply=str(reply), elapsed=self._now() - t0,
            ))
        except Exception as e:  # noqa: BLE001 — 派活失败绝不拖垮主路径
            logger.warning("A2A 派活失败（agent=%s）: %s", agent, e)
            sid, spath = self._ids_of(session)
            return self._finish(DelegateResult(
                False, agent_id=agent, session_id=sid, session_path=spath,
                error=f"{type(e).__name__}: {e}", elapsed=self._now() - t0,
            ))

    @staticmethod
    def _ids_of(session) -> tuple[str, str]:
        if session is None:
            return "", ""
        if isinstance(session, dict):
            return (str(session.get("session_id") or ""),
                    str(session.get("session_path") or ""))
        return (str(getattr(session, "session_id", "") or ""),
                str(getattr(session, "session_path", "") or ""))

    def _finish(self, result: DelegateResult) -> DelegateResult:
        with self._lock:
            self.results.append(result)
            # 结果队列有界，避免长跑堆积
            if len(self.results) > 50:
                del self.results[:-50]
        if self._on_result is not None and result.ok:
            try:
                self._on_result(result)
            except Exception:  # noqa: BLE001
                logger.debug("A2A on_result 回调失败（忽略）", exc_info=True)
        return result


def build_from_config(
    config: Optional[dict],
    create_session: Callable[[str], Any],
    send: Callable[..., str],
    on_result: Optional[Callable[[DelegateResult], None]] = None,
) -> Delegator:
    """按 ``config.json`` 的 ``a2a`` 块构建（未启用也返回实例，便于查询状态）。"""
    cfg = ((config or {}).get("a2a") or {}) if isinstance(config, dict) else {}
    d = Delegator(create_session=create_session, send=send, cfg=cfg, on_result=on_result)
    logger.info("A2A 就绪：%s", d.stats())
    return d
