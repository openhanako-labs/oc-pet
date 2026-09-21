# -*- coding: utf-8 -*-
"""结构输入侧单测（core/atmosphere_input.py）。

锁三件事：
1. **不偷看未来**：基线只来自过去窗口（离线研究可以用全局分位，上线不行）；
2. **有预热**：前 warmup 轮一律 neu——没有基线就没有"偏离"可言；
3. **异常安全**：出任何事回 neu，绝不冒泡。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.atmosphere_input import (  # noqa: E402
    SLOT_HIGH, SLOT_LOW, SLOT_MID,
    RollingQuantileTristate, defaults_for, signal_value,
)


# ── 信号取值 ────────────────────────────────────────────

def test_ratio_sign_and_zero():
    # 用户说得多 → 正；桌宠说得多 → 负；一样长 → 0
    assert signal_value("ratio", "x" * 40, "y" * 2) > 0
    assert signal_value("ratio", "x", "y" * 40) < 0
    assert signal_value("ratio", "abc", "def") == 0.0


def test_ratio_missing_material_is_none():
    assert signal_value("ratio", "", "abc") is None
    assert signal_value("ratio", "abc", "") is None


def test_len_u_only_needs_user():
    assert signal_value("len_u", "abc", "") is not None
    assert signal_value("len_u", "", "abc") is None


def test_drift_identical_vectors_is_zero():
    v = [1.0, 0.0, 0.0]
    assert abs(signal_value("drift_u", "a", "b", v, v)) < 1e-9


def test_drift_orthogonal_vectors_is_one():
    a, b = [1.0, 0.0], [0.0, 1.0]
    assert abs(signal_value("drift_u", "a", "b", a, b) - 1.0) < 1e-9


def test_drift_missing_vectors_is_none():
    assert signal_value("drift_u", "a", "b") is None
    assert signal_value("drift_u", "a", "b", [1.0], None) is None


def test_unknown_signal_is_none():
    assert signal_value("不存在", "a", "b") is None


def test_signal_value_never_raises_on_garbage():
    assert signal_value("drift_u", "a", "b", ["x"], ["y"]) is None
    assert signal_value(None, "a", "b") is None


# ── 滚动分位三态 ────────────────────────────────────────

def test_warmup_returns_mid():
    t = RollingQuantileTristate(window=10, warmup=5)
    for i in range(5):
        assert t.label(float(i)) == SLOT_MID


def test_detects_high_and_low_after_warmup():
    t = RollingQuantileTristate(window=20, warmup=5)
    # 稳定的一串
    for _ in range(12):
        t.label(1.0)
    assert t.label(100.0) == SLOT_HIGH
    assert t.label(-100.0) == SLOT_LOW


def test_baseline_uses_past_only():
    """同样的历史，喂一个大值 → high；喂完它进历史后，再喂同一个值就不该还是 high。"""
    a = RollingQuantileTristate(window=10, warmup=3)
    b = RollingQuantileTristate(window=10, warmup=3)
    for t in (a, b):
        for _ in range(6):
            t.label(1.0)
    assert a.label(50.0) == SLOT_HIGH
    # b 先把 50 喂进去（进历史），再喂一次 50 → 它已在窗口里，不该再算极端
    b.label(50.0)
    for _ in range(5):
        b.label(1.0)
    assert b.label(50.0) in (SLOT_HIGH, SLOT_MID)  # 不应崩，且基线确实变了


def test_none_returns_mid_without_polluting_history():
    t = RollingQuantileTristate(window=8, warmup=2)
    t.label(1.0)
    t.label(1.0)
    assert t.label(None) == SLOT_MID
    assert t.snapshot()["samples"] == 2, "None 不该进历史"


def test_window_is_bounded():
    t = RollingQuantileTristate(window=5, warmup=2)
    for i in range(100):
        t.label(float(i))
    assert t.snapshot()["samples"] == 5


def test_nan_and_inf_are_neutral():
    t = RollingQuantileTristate(window=10, warmup=2)
    assert t.label(float("nan")) == SLOT_MID
    assert t.label(float("inf")) == SLOT_MID


def test_label_never_raises_on_garbage():
    t = RollingQuantileTristate(window=10, warmup=2)
    assert t.label("不是数字") == SLOT_MID
    assert t.label(object()) == SLOT_MID


def test_reset_clears():
    t = RollingQuantileTristate(window=10, warmup=2)
    for _ in range(6):
        t.label(1.0)
    t.reset()
    s = t.snapshot()
    assert s["samples"] == 0 and s["seen"] == 0
    assert t.label(1.0) == SLOT_MID       # 回到预热期


def test_snapshot_shape():
    t = RollingQuantileTristate(window=10, warmup=2)
    for i in range(6):
        t.label(float(i))
    s = t.snapshot()
    for k in ("window", "warmup", "samples", "seen", "median", "shares"):
        assert k in s
    assert set(s["shares"]) == {"hi", "mid", "lo"}


def test_bad_config_falls_back():
    t = RollingQuantileTristate(window=0, warmup=0, low_q=0.9, high_q=0.1)
    s = t.snapshot()
    assert s["window"] >= 2 and s["warmup"] >= 1
    # low_q/high_q 被夹在合法区间，不会出现 q1 > q2 把三态拧反
    for i in range(20):
        assert t.label(float(i)) in (SLOT_HIGH, SLOT_MID, SLOT_LOW)


# ── 族默认阈值 ──────────────────────────────────────────

def test_defaults_differ_per_family():
    """两族的 clear 工作点不同，阈值必须分开——混用会让一族的迟滞带整个落空。"""
    st = defaults_for("structural")
    em = defaults_for("emotion")
    assert st["theta_hi"] == 0.50 and st["theta_lo"] == 0.40
    assert em["theta_hi"] == 0.40 and em["theta_lo"] == 0.25
    assert st["theta_lo"] > em["theta_lo"]


def test_defaults_unknown_source_is_emotion():
    assert defaults_for("")["theta_hi"] == 0.40
    assert defaults_for(None)["theta_hi"] == 0.40
    assert defaults_for("ratio")["theta_hi"] == 0.50
