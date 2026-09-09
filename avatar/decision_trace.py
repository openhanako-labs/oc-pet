"""表情 / 动作 / 情绪仲裁的决策可追溯。

没有可观测性，调表情动作参数就是抓瞎。本项目 live2d_renderer.py 30 天改 79 次，
每次改动都像盲人摸象——换模型后某个表情静默失效，只能靠用户偶然发现。
decision_trace 记录每一次"选了什么、谁被否决、为什么"，并提供最近 N 条查询
与人类可读报告，让"桌宠现在为什么是这个表情/动作"可查。

特性：
- 环形缓冲（默认 200 条），线程安全。
- 连续相同的决策自动合并为一条并计数，避免每帧刷屏（表情匹配每帧都在跑）。
- 暴露全局单例 ``trace``，任意模块 ``from avatar.decision_trace import trace`` 即可记录。
- 产生"新决策"（非合并）时自动打 ``logger.info("[DECISION] ...")``，
  运行日志可直接 ``grep DECISION`` 看变化时刻。

用法：
    trace.record("emotion", chosen="happy", source="screen",
                 rejected={"thinking": "dwell<8s"}, note="switch")
    trace.recent(10)     # 最近 10 条 Decision 对象
    trace.report(10)     # 人类可读文本
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

logger = logging.getLogger("oc.decision_trace")

TRACE_MAX = 200


@dataclass
class Decision:
    ts: float
    scope: str            # "emotion" | "expression" | "motion"
    chosen: str           # 最终选择（无匹配记为 "(none)"）
    source: str = ""      # 触发方 / 来源（如 "screen" / "dialog" / "exact:happy"）
    rejected: Dict[str, str] = field(default_factory=dict)  # {候选: 被否决原因}
    note: str = ""
    count: int = 1

    def as_line(self) -> str:
        t = time.strftime("%H:%M:%S", time.localtime(self.ts))
        rj = ", ".join(f"{k}={v}" for k, v in self.rejected.items()) if self.rejected else "-"
        c = f" x{self.count}" if self.count > 1 else ""
        extra = f" | {self.note}" if self.note else ""
        return f"[{t}] {self.scope}: {self.chosen}{c} (src={self.source or '-'}) rejected={rj}{extra}"


class DecisionTrace:
    def __init__(self, maxlen: int = TRACE_MAX):
        self._buf: list[Decision] = []
        self._max = maxlen
        self._lock = threading.Lock()

    def record(self, scope: str, chosen, source: str = "",
               rejected: Optional[Dict[str, str]] = None, note: str = "",
               merge: bool = True) -> Decision:
        d = Decision(ts=time.time(), scope=scope, chosen=str(chosen),
                     source=source or "", rejected=rejected or {}, note=note or "")
        with self._lock:
            if merge and self._buf:
                last = self._buf[-1]
                if (last.scope == scope and last.chosen == d.chosen
                        and last.source == d.source and last.note == d.note):
                    last.count += 1
                    last.ts = d.ts
                    return last
            self._buf.append(d)
            if len(self._buf) > self._max:
                self._buf = self._buf[-self._max:]
        logger.info("[DECISION] %s", d.as_line())
        return d

    def recent(self, n: int = 20) -> list[Decision]:
        with self._lock:
            return list(self._buf[-n:])

    def report(self, n: int = 20) -> str:
        with self._lock:
            lines = [d.as_line() for d in self._buf[-n:]]
        return "\n".join(lines) if lines else "(no decisions recorded)"

    def clear(self) -> None:
        with self._lock:
            self._buf.clear()


# 全局单例：pet 层与 avatar 层共享同一份缓冲，任意一处 report 都能看到全部决策。
trace = DecisionTrace()
