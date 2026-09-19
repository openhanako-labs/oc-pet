# -*- coding: utf-8 -*-
"""后台 LLM 全局闸门（O1-P2，429 治理）单元测试。

时间注入，不依赖真实等待。
"""
from __future__ import annotations

from core.llm_gate import BUDGET_WINDOW_SECONDS, DEFAULT_BUDGETS, LlmGate


class _Clock:
    def __init__(self, t: float = 10_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _gate(**kw) -> tuple[LlmGate, _Clock]:
    clock = kw.pop("clock", None) or _Clock()
    g = LlmGate(now=clock, **kw)
    return g, clock


# ── 基础 ──────────────────────────────────────────────────


def test_clean_gate_allows():
    g, _ = _gate()
    assert g.check("enrich") == (True, "")
    assert g.cooldown_remaining() == 0.0


def test_disabled_gate_allows_everything():
    g, c = _gate(enabled=False)
    g.notify_429()
    assert g.cooldown_remaining() == 0.0        # 关掉闸门就不记冷却
    assert g.check("enrich")[0] is True


# ── 并发上限 ──────────────────────────────────────────────


def test_concurrency_limit_default_one():
    g, _ = _gate(max_concurrent=1)
    assert g.acquire("vision") is True
    assert g.acquire("enrich") is False          # 槽被占，抢不到就放弃
    g.release()
    assert g.acquire("enrich") is True


def test_only_one_denial_does_not_leak_slots():
    g, _ = _gate(max_concurrent=1)
    g.acquire("vision")
    for _ in range(5):
        assert g.acquire("vision") is False
    g.release()
    assert g.acquire("vision") is True


def test_guard_yields_true_then_releases():
    g, _ = _gate(max_concurrent=1)
    with g.guard("enrich") as ok:
        assert ok is True
        assert g.check("enrich")[0] is True      # check 不看槽
    with g.guard("vision") as ok2:               # 上一个已归还
        assert ok2 is True


def test_guard_yields_false_when_busy():
    g, _ = _gate(max_concurrent=1)
    g.acquire("vision")
    with g.guard("enrich") as ok:
        assert ok is False
    g.release()


def test_max_concurrent_two_allows_two():
    g, _ = _gate(max_concurrent=2)
    assert g.acquire("a") and g.acquire("b")
    assert g.acquire("c") is False


def test_extra_release_does_not_inflate_slots():
    """多还一次不能把信号量撑大（否则并发上限会静默失效）。"""
    g, _ = _gate(max_concurrent=1)
    g.release()
    g.release()
    assert g.acquire("a") is True
    assert g.acquire("b") is False


# ── 每源预算 ──────────────────────────────────────────────


def test_budget_blocks_after_limit():
    g, _ = _gate(max_concurrent=8, budgets={"enrich": 2})
    assert g.acquire("enrich") and g.acquire("enrich")
    ok, why = g.check("enrich")
    assert ok is False and why == "budget"
    g.release()


def test_budget_is_per_source():
    g, _ = _gate(max_concurrent=8, budgets={"enrich": 1, "vision": 5})
    g.acquire("enrich")
    assert g.check("enrich")[0] is False
    assert g.check("vision")[0] is True           # 不受别的源牵连
    g.release()


def test_budget_zero_means_source_off():
    g, _ = _gate(budgets={"enrich": 0})
    ok, why = g.check("enrich")
    assert ok is False and why == "budget=0"


def test_unknown_source_is_unlimited():
    g, _ = _gate(max_concurrent=200, budgets={"enrich": 1})
    for _ in range(50):
        assert g.acquire("something_else") is True
        g.release()


def test_budget_window_slides():
    g, c = _gate(max_concurrent=8, budgets={"enrich": 1})
    g.acquire("enrich")
    assert g.check("enrich")[0] is False
    c.advance(BUDGET_WINDOW_SECONDS + 1)          # 滑出窗口
    assert g.check("enrich")[0] is True


def test_budget_does_not_count_denied_attempts():
    g, _ = _gate(max_concurrent=8, budgets={"enrich": 2})
    g.acquire("enrich")
    for _ in range(10):
        g.acquire("enrich")                       # 第二次成功，其余被拒
    assert g.stats()["used_last_hour"]["enrich"] == 2
    g.release()


def test_defaults_are_generous_enough_for_normal_cadence():
    """默认预算不能被正常节奏撞到（否则一开就降级）。"""
    assert DEFAULT_BUDGETS["vision"] >= 90     # 正常约 30/h
    assert DEFAULT_BUDGETS["enrich"] >= 30     # 冷却 300s 时约 12/h
    assert DEFAULT_BUDGETS["proactive"] >= 20


# ── 全局 429 冷却 ─────────────────────────────────────────


def test_429_cools_down_every_source():
    g, _ = _gate()
    assert g.notify_429("vision") == 60.0
    for src in ("vision", "enrich", "proactive"):
        ok, why = g.check(src)
        assert ok is False and why == "cooldown"
    assert g.cooldown_remaining() == 60.0


def test_cooldown_expires():
    g, c = _gate()
    g.notify_429("vision")
    c.advance(61)
    assert g.check("enrich")[0] is True
    assert g.cooldown_remaining() == 0.0


def test_repeated_429_escalates_and_caps():
    g, c = _gate(cooldown_seconds=60, max_cooldown_seconds=240)
    assert g.notify_429() == 60.0
    c.advance(30)                                  # 冷却期内又撞
    assert g.notify_429() == 120.0
    c.advance(30)
    assert g.notify_429() == 240.0
    c.advance(30)
    assert g.notify_429() == 240.0                 # 封顶


def test_second_round_after_expiry_starts_from_base():
    g, c = _gate(cooldown_seconds=60)
    g.notify_429()
    c.advance(61)
    assert g.notify_429() == 60.0                  # 不是累加到 120


def test_notify_ok_resets_counters():
    g, c = _gate()
    g.notify_429()
    c.advance(61)
    g.notify_ok()
    assert g.stats()["hits_429"] == 0
    c.advance(1)
    g.notify_429()
    assert g.cooldown_remaining() == 60.0          # 没有带着旧计数翻倍


# ── stats ─────────────────────────────────────────────────


def test_stats_shape():
    g, _ = _gate(budgets={"enrich": 2})
    g.acquire("enrich")
    s = g.stats()
    assert s["enabled"] is True
    assert s["max_concurrent"] == 1
    assert s["budgets"]["enrich"] == 2
    assert s["used_last_hour"]["enrich"] == 1
    assert "vision" in s["budgets"]
    g.release()


def test_bad_budget_value_is_ignored():
    g, _ = _gate(budgets={"enrich": "not-a-number", "vision": 7})
    assert g.stats()["budgets"]["enrich"] == DEFAULT_BUDGETS["enrich"]
    assert g.stats()["budgets"]["vision"] == 7


# ── 单例配置 ──────────────────────────────────────────────


def test_configure_gate_reads_config():
    from core.llm_gate import configure_gate, get_gate

    configure_gate({"llm_gate": {"max_concurrent": 3, "cooldown_seconds": 5,
                                 "budgets": {"enrich": 9}, "enabled": True}})
    g = get_gate()
    assert g.stats()["max_concurrent"] == 3
    assert g.stats()["budgets"]["enrich"] == 9
    assert g.stats()["budgets"]["vision"] == DEFAULT_BUDGETS["vision"]
    configure_gate(None)                           # 还原，别影响别的测试
    assert get_gate().enabled is True
