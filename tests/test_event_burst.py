# -*- coding: utf-8 -*-
"""事件爆发合并（去抖）验收。

只测单个函数没用——真正要证明的是调用方的行为：
**一串密集事件只打一次 API，而稀疏事件各打各的。**
所以这里按 screen.py 的方式模拟"每次 add 就重排定时器"再来数冲刷次数。
"""
from __future__ import annotations

from pathlib import Path

from core.perception.event_burst import (
    DEFAULT_MAX_AGE_S,
    DEFAULT_MAX_SIZE,
    DEFAULT_MIN_AGE_S,
    DEFAULT_QUIET_GAP_S,
    EventBurstCoalescer,
)


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def _simulate(clock, timeline, **kw):
    """按调用方语法模拟：每次 add 说"立刻冲"就冲，否则重排定时器。

    timeline: [(app, 距上一事件过了多少秒)]
    返回冲刷出来的爆发列表。
    """
    c = EventBurstCoalescer(clock=clock, **kw)
    flushes = []
    deadline = None

    def _fire_if_due():
        nonlocal deadline
        if deadline is not None and clock() >= deadline:
            b = c.take()
            if b:
                flushes.append(b)
            deadline = None

    for app, advance in timeline:
        clock.advance(advance)
        _fire_if_due()
        flush_now, delay = c.add(app)
        if flush_now:
            b = c.take()
            if b:
                flushes.append(b)
            deadline = None
        else:
            deadline = clock() + delay
    if deadline is not None:
        clock.t = max(clock.t, deadline)
        b = c.take()
        if b:
            flushes.append(b)
    return flushes


# ── 密度决定次数（核心） ──────────────────────────────────


def test_dense_burst_merges_into_one_call():
    """五个窗口连切 → **只打一次**，但那一次知道全部五个。"""
    clock = _Clock()
    t = [("Edge", 0.0), ("VSCode", 0.2), ("Edge", 0.2), ("Terminal", 0.2), ("Chrome", 0.2)]
    flushes = _simulate(clock, t, quiet_gap_s=1.0, min_age_s=0.5, max_age_s=8.0)
    assert len(flushes) == 1, f"应该只合并成一次，实际 {len(flushes)} 次"
    assert flushes[0].size == 5
    assert flushes[0].window_chain() == "Edge → VSCode → Edge → Terminal → Chrome"


def test_sparse_events_each_get_their_own_call():
    """隔很久的窗口切换不是一个爆发，各算各的。"""
    clock = _Clock()
    t = [("Edge", 0.0), ("VSCode", 100.0), ("Chrome", 100.0)]
    flushes = _simulate(clock, t, quiet_gap_s=1.0, min_age_s=0.5, max_age_s=8.0)
    assert len(flushes) == 3


def test_more_dense_never_means_more_calls():
    """反直觉但关键：事件越密，调用次数**不会更多**（只会被合并）。"""
    coarse = _simulate(_Clock(), [("A", 0.0), ("B", 2.0)], quiet_gap_s=1.0)
    assert len(coarse) == 2, "隔很久的两次各算一次"
    dense = _simulate(_Clock(), [("A", 0.0)] + [("B", 0.2)] * 4, quiet_gap_s=1.0)
    assert len(dense) == 1, "5 个事件不超容量 → 合并成一次"
    # 超过容量时按容量分批，但**绝不会一事件一次**（11 个事件最多 2 次）
    many = _simulate(_Clock(), [("A", 0.0)] + [("B", 0.2)] * 10, quiet_gap_s=1.0)
    assert len(many) <= 2
    assert sum(f.size for f in many) == 11, "合并不能丢事件"


def test_max_size_forces_flush():
    """到量就冲，不无限攒。"""
    clock = _Clock()
    t = [("A", 0.0)] + [("B", 0.05)] * 5
    flushes = _simulate(clock, t, quiet_gap_s=10.0, min_age_s=0.1,
                        max_age_s=60.0, max_size=3)
    assert len(flushes) >= 2
    assert all(f.size <= 3 for f in flushes)


def test_max_age_caps_the_wait():
    """一直有事件（永不安静）时，也不能无限等。"""
    clock = _Clock()
    t = [("A", 0.0)] + [("B", 0.5)] * 40        # 20 秒的持续活动
    flushes = _simulate(clock, t, quiet_gap_s=1.0, min_age_s=0.1, max_age_s=3.0)
    assert len(flushes) >= 3, "max_age 到了就该冲"


def test_timer_flush_happens_after_quiet_gap():
    clock = _Clock()
    c = EventBurstCoalescer(quiet_gap_s=2.0, clock=clock)
    flush_now, delay = c.add("Edge")
    assert flush_now is False
    assert 1.9 < delay <= 2.0
    clock.advance(delay - 0.1)
    assert c.pending_count == 1, "还没到点，不能提前冲"


def test_min_age_holds_back_a_lone_event():
    """单个事件也要等到 min_age——避免"一到就走"跟定时器撞车。"""
    clock = _Clock()
    c = EventBurstCoalescer(quiet_gap_s=0.1, min_age_s=1.0, clock=clock)
    _flush, delay = c.add("Edge")
    assert delay >= 0.9


# ── 窗口链 ────────────────────────────────────────────────


def test_chain_collapses_consecutive_duplicates():
    clock = _Clock()
    c = EventBurstCoalescer(clock=clock)
    for app in ("Edge", "Edge", "VSCode", "VSCode", "VSCode", "Edge"):
        c.add(app)
    assert c.take().window_chain() == "Edge → VSCode → Edge"


def test_chain_is_capped():
    clock = _Clock()
    c = EventBurstCoalescer(max_size=20, clock=clock)
    for i in range(12):
        c.add(f"W{i}")
    chain = c.take().window_chain(limit=4)
    assert chain.endswith("…") and chain.count("→") == 4


def test_chain_ignores_blank_labels():
    clock = _Clock()
    c = EventBurstCoalescer(clock=clock)
    for app in ("Edge", "", "   ", "VSCode"):
        c.add(app)
    assert c.take().window_chain() == "Edge → VSCode"


def test_hint_falls_back_when_last_window_unknown():
    clock = _Clock()
    c = EventBurstCoalescer(clock=clock)
    for app in ("Edge", "VSCode", "  "):
        clock.advance(0.5)
        c.add(app)
    h = c.take().hint()
    assert "某窗口" in h, "最后一个窗口未知时不能让提示词断在半句"
    assert "Edge → VSCode" in h


# ── hint（给模型看的一句话） ──────────────────────────────


def test_hint_single_event():
    clock = _Clock()
    c = EventBurstCoalescer(clock=clock)
    c.add("Edge")
    assert "刚刚切到 Edge" in c.take().hint()


def test_hint_multi_event_mentions_last_window():
    clock = _Clock()
    c = EventBurstCoalescer(clock=clock)
    for app in ("Edge", "VSCode", "Chrome"):
        clock.advance(1.0)
        c.add(app)
    h = c.take().hint()
    assert "切过 3 次窗口" in h and "最后停在 Chrome" in h


def test_hint_empty_when_no_events():
    burst = EventBurstCoalescer().pending()
    assert burst is None


# ── 取值边界 ──────────────────────────────────────────────


def test_min_age_clamped_to_max_age():
    c = EventBurstCoalescer(min_age_s=99.0, max_age_s=5.0)
    assert c.min_age_s == 5.0, "min_age 超过 max_age 会永远等不到"


def test_negative_and_zero_inputs_are_safe():
    c = EventBurstCoalescer(quiet_gap_s=-1, min_age_s=-1, max_age_s=0, max_size=0)
    assert c.quiet_gap_s == 0.0
    assert c.max_age_s >= 0.1
    assert c.max_size >= 1


def test_take_clears_and_reset_works():
    clock = _Clock()
    c = EventBurstCoalescer(clock=clock)
    c.add("A")
    assert c.pending_count == 1
    assert c.take() is not None
    assert c.pending_count == 0 and c.take() is None
    c.add("B")
    c.reset()
    assert c.pending_count == 0


def test_defaults_are_conservative():
    """默认安静窗口要**短**（不牺牲响应性），上限也别太贪。"""
    assert DEFAULT_QUIET_GAP_S <= 3.0
    assert DEFAULT_MIN_AGE_S <= DEFAULT_QUIET_GAP_S + 1.0
    assert DEFAULT_MAX_AGE_S <= 15.0
    assert DEFAULT_MAX_SIZE <= 10


# ── 接线护栏（screen.py） ─────────────────────────────────


def _screen_src() -> str:
    return Path(__file__).resolve().parents[1].joinpath(
        "core", "perception", "screen.py").read_text(encoding="utf-8")


def test_screen_wires_the_coalescer():
    s = _screen_src()
    assert "EventBurstCoalescer" in s
    assert "self._burst_enabled" in s
    body = s[s.index("def on_foreground_change"):]
    body = body[:body.index("def start(")]
    assert "_flush_burst" in body and "_schedule_burst_flush" in body


def test_old_behaviour_is_still_reachable():
    """合并不可用/关掉时必须回退旧行为，不能让事件路径直接哑掉。"""
    s = _screen_src()
    body = s[s.index("def on_foreground_change"):]
    body = body[:body.index("def start(")]
    assert "if not (self._burst_enabled" in body
    assert "_capture_and_analyze(mode=\"event\"" in body


def test_flush_does_nothing_after_stop():
    """停止感知后不能又冒出一条 API。"""
    s = _screen_src()
    body = s[s.index("def _flush_burst"):]
    body = body[:body.index("def start(")]
    assert "if not self._running" in body
    assert "cancel_pending_burst" in s, "停止时要能丢弃未冲的爆发"
