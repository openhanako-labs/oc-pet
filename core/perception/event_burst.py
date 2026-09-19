"""事件爆发合并（去抖）——借鉴 Cortico 的 batching，但**只借它真对的那部分**。

## 它解决什么

屏幕感知的"事件路径"（前台窗口切换）会和"定时路径"一起打视觉 API。
全日志实测：**event 91 次 / timer 96 次**——事件路径承担了一半流量。

而现在的处理是**丢弃**而不是**合并**：一次切窗口爆发里，只有第一个窗口被分析，
其余的连"你看过"这件事都没留下。Cortico 的做法是反过来——

    距末次事件等 quietGap → 再动手；
    距首次至少 minBatch；
    最多 maxBatch；
    一批最多 maxSize。

于是 A→B→C 三连切只打**一次** API，但那一次**知道你看过 A、B、C**。

## 为什么不用那个"两行就能改"的便宜修法

`screen.py` 里 timer 会避让 event，但 event 不看 timer 的时间戳——所以会出现
（实测 2026-09-19 20:54:26 timer → 20:54:29 event）**相隔 3 秒的两条视觉 API**。

最省事的改法是给 event 也套上共用的 `interval`（默认 ~120s）冷却。
**不采用**：那会让"你切了个窗口"最多要等两分钟才被注意——省钱但把响应性卖了。
Cortico 对的地方恰恰是它等得**很短**（quietGap 级），不是等两分钟。

## 本模块的边界

**纯判定，不含线程。** 什么时候真的动手由调用方排（screen.py 用 threading.Timer）。
clock 注入 → 能单测，能证明"越密只动手一次"。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Optional

# 默认值：安静窗口短（不牺牲响应性）、最多等 8 秒（不无限拖）
DEFAULT_QUIET_GAP_S = 1.5
DEFAULT_MIN_AGE_S = 1.0
DEFAULT_MAX_AGE_S = 8.0
DEFAULT_MAX_SIZE = 6
MAX_CHAIN_LEN = 6          # 窗口链最多显示几个，避免提示词被刷长


@dataclass(frozen=True)
class BurstEvent:
    app: str
    title: str = ""
    category: str = ""
    ts: float = 0.0

    def label(self) -> str:
        """窗口名（空白一律返回空串，由调用方决定怎么显示）。"""
        return (self.app or "").strip()


@dataclass
class Burst:
    """一次爆发（已合并的事件序列）。"""

    events: list[BurstEvent] = field(default_factory=list)

    @property
    def size(self) -> int:
        return len(self.events)

    @property
    def first_ts(self) -> float:
        return self.events[0].ts if self.events else 0.0

    @property
    def last_ts(self) -> float:
        return self.events[-1].ts if self.events else 0.0

    @property
    def last(self) -> Optional[BurstEvent]:
        return self.events[-1] if self.events else None

    @property
    def duration(self) -> float:
        return max(0.0, self.last_ts - self.first_ts)

    def window_chain(self, limit: int = MAX_CHAIN_LEN) -> str:
        """把窗口序列压成 ``A → B → C``（连续重复的只留一个）。

        这是合并**真正的收益**：一次调用里能告诉模型"你刚切过哪几个窗口"，
        而不像以前只看得见第一个。
        """
        chain: list[str] = []
        for e in self.events:
            label = e.label()
            if not label or (chain and chain[-1] == label):
                continue
            chain.append(label)
        if len(chain) > limit:
            chain = chain[:limit] + ["…"]
        return " → ".join(chain)

    def hint(self) -> str:
        """给视觉提示词用的一句人话（没有内容时返回空串）。"""
        if not self.events:
            return ""
        chain = self.window_chain()
        last_label = self.events[-1].label() or "某窗口"
        if self.size <= 1:
            return f"刚刚切到 {chain or last_label}（{self.duration:.1f}s 内）"
        return (f"这 {self.duration:.1f}s 里切过 {self.size} 次窗口：{chain}"
                f"（最后停在 {last_label}）")


class EventBurstCoalescer:
    """把一串挤在一起的事件合并成一次。（纯判定，不含线程。）

    用法::

        flush_now, delay = c.add(app, title, category)
        if flush_now:
            do_flush()
        else:
            schedule(delay, do_flush)     # 每次 add 都重排，即去抖
        ...
        burst = c.take()                  # 冲刷时取走
    """

    def __init__(self,
                 quiet_gap_s: float = DEFAULT_QUIET_GAP_S,
                 min_age_s: float = DEFAULT_MIN_AGE_S,
                 max_age_s: float = DEFAULT_MAX_AGE_S,
                 max_size: int = DEFAULT_MAX_SIZE,
                 clock: Callable[[], float] = time.monotonic):
        self.quiet_gap_s = max(0.0, float(quiet_gap_s))
        # 下限不能超过上限，否则永远等不到
        self.max_age_s = max(0.1, float(max_age_s))
        self.min_age_s = min(max(0.0, float(min_age_s)), self.max_age_s)
        self.max_size = max(1, int(max_size))
        self._clock = clock
        self._events: list[BurstEvent] = []

    # ── 状态 ──────────────────────────────────────────────

    @property
    def pending_count(self) -> int:
        return len(self._events)

    def pending(self) -> Optional[Burst]:
        if not self._events:
            return None
        return Burst(list(self._events))

    # ── 主入口 ────────────────────────────────────────────

    def add(self, app: str, title: str = "", category: str = "") -> tuple:
        """记一个事件，返回 ``(是否该立刻冲刷, 还要等多少秒)``。

        去抖的核心：每次 add 都把"再等一会"往后推——所以事件越密，
        动手越晚，**最后只动一次**。
        """
        now = self._clock()
        self._events.append(BurstEvent(app=app, title=title,
                                       category=category, ts=now))

        # 到量了立刻冲刷，不必再等
        if len(self._events) >= self.max_size:
            return True, 0.0

        first = self._events[0].ts
        deadline = first + self.max_age_s
        if now >= deadline:
            return True, 0.0

        # 等到 max(末次 + quietGap, 首次 + minAge)，但不超过 maxAge
        target = max(now + self.quiet_gap_s, first + self.min_age_s)
        target = min(target, deadline)
        delay = max(0.0, target - now)
        return (delay <= 0.0), delay

    def take(self) -> Optional[Burst]:
        """取走并清空（冲刷时调）。"""
        if not self._events:
            return None
        burst = Burst(list(self._events))
        self._events.clear()
        return burst

    def reset(self) -> None:
        self._events.clear()
