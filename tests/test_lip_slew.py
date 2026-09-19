# -*- coding: utf-8 -*-
"""口型起音/收音（O3 残余）单元测试。

对齐 AgentAtelierR 的 40ms 起音 / 90ms 收音；纯数学，不依赖渲染器。
"""
from __future__ import annotations

import math
from pathlib import Path

from core.lip_sync import (
    MOUTH_ATTACK_S,
    MOUTH_RELEASE_S,
    get_mouth_peak,
    set_mouth_peak,
    slew,
)

ROOT = Path(__file__).resolve().parent.parent


# ── 到达时间 ──────────────────────────────────────────────


def _steps(cur, target, total_s, dt, **kw):
    """从 cur 出发，按 dt 步进 total_s，返回最终值。"""
    v = cur
    n = int(round(total_s / dt))
    for _ in range(n):
        v = slew(v, target, dt, **kw)
    return v


def test_attack_reaches_most_of_target_within_attack_window():
    """40ms 内应到约 95% 目标（τ = 秒/3）。"""
    v = _steps(0.0, 1.0, MOUTH_ATTACK_S, 0.005)
    assert 0.90 <= v <= 0.99, f"40ms 后到了 {v:.3f}，偏离 95% 太远"


def test_release_is_slower_than_attack():
    """同样的 40ms，降得必须比升得少——收比起慢是人说话的样子。"""
    up = _steps(0.0, 1.0, 0.04, 0.005)
    down = _steps(1.0, 0.0, 0.04, 0.005)
    assert (1.0 - down) < up, "收音不比起音慢"


def test_release_reaches_about_95_percent_within_release_window():
    """合约：``release_s`` 秒内到达**约 95%** 路程。

    τ = 秒/3 ⇒ 剩 e⁻³ ≈ 5%。断言 2% 会误报——那是把“到 95%”写成了“几乎到位”。
    """
    v = _steps(0.95, 0.30, MOUTH_RELEASE_S, 0.005)
    reached = (0.95 - v) / (0.95 - 0.30)
    assert reached >= 0.90, f"90ms 只走了 {reached:.1%}"
    assert reached < 0.99, "不该在 T 秒内就宣称完全到位"


def test_tau_ratio_contract():
    """显式把“秒 → 时间常数”的换算比写下来：一倍 T 后应剩约 e⁻³。"""
    v = _steps(0.0, 1.0, MOUTH_ATTACK_S, 0.002)
    assert abs((1.0 - v) - math.exp(-3.0)) < 0.02


def test_mouth_never_undershoots_or_overshoots():
    """指数逼近不得越界（嘴不能比目标张更大/更小）。"""
    for _ in range(50):
        v = slew(0.2, 0.8, 0.016)
        assert 0.2 <= v <= 0.8
    for _ in range(50):
        v = slew(0.8, 0.2, 0.016)
        assert 0.2 <= v <= 0.8


# ── 帧率无关 ──────────────────────────────────────────────


def test_frame_rate_independent_over_100ms():
    """大帧与小帧在同一段时间内应几乎同结果，否则换显示器就变形。"""
    coarse = _steps(0.0, 1.0, 0.1, 0.05)      # 2 帧
    fine = _steps(0.0, 1.0, 0.1, 0.005)       # 20 帧
    assert abs(coarse - fine) < 0.02, f"帧率相关：大帧 {coarse:.3f} vs 小帧 {fine:.3f}"


def test_half_the_time_is_not_half_the_distance_only_exponential():
    """指数逆近：一半时间应超过一半路程（不是线性插值）。"""
    half = _steps(0.0, 1.0, MOUTH_ATTACK_S / 2, 0.005)
    assert half > 0.5, "看起来像线性插值"


# ── 退化输入 ──────────────────────────────────────────────


def test_zero_dt_jumps_to_target():
    """没有时间信息时直接采用目标——宁可不管，也不要让嘴卡在旧值上。"""
    assert slew(0.0, 0.9, 0.0) == 0.9
    assert slew(0.0, 0.9, -1.0) == 0.9


def test_nan_dt_jumps_to_target():
    assert slew(0.0, 0.9, float("nan")) == 0.9


def test_zero_reach_means_no_smoothing():
    assert slew(0.0, 0.9, 0.016, attack_s=0.0) == 0.9
    assert slew(1.0, 0.0, 0.016, release_s=0.0) == 0.0


def test_bad_inputs_fall_back_to_target():
    assert slew("x", 0.5, 0.016) == 0.5
    assert slew(0.0, "y", 0.016) is not None


def test_already_at_target_stays():
    assert slew(0.5, 0.5, 0.016) == 0.5


def test_long_gap_converges_fully():
    """卡了一秒再来一帧，应基本到位（不影响“从静音开口”）。"""
    v = slew(0.0, 0.8, 1.0)
    assert abs(v - 0.8) < 0.001


# ── 开口上限旋钮 ──────────────────────────────────────────


def test_mouth_peak_defaults_to_no_cap():
    set_mouth_peak(1.0)
    assert get_mouth_peak() == 1.0


def test_mouth_peak_accepts_valid_value():
    try:
        assert set_mouth_peak(0.55) == 0.55
        assert get_mouth_peak() == 0.55
    finally:
        set_mouth_peak(1.0)


def test_mouth_peak_rejects_bad_values():
    """0 / 负数 / >1 / 非数字 → 回退 1.0（宁可不管，也不能把嘴锁死）。"""
    for bad in (0, -0.3, 1.5, "abc", None):
        assert set_mouth_peak(bad) == 1.0, f"{bad!r} 没被拦下"
    assert get_mouth_peak() == 1.0


# ── 接线守卫 ──────────────────────────────────────────────


def _src(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_renderer_slews_only_the_phoneme_path():
    """音素路径要过 slew；振幅路径本来就有包络，不该再叠一层。"""
    s = _src("avatar/live2d_renderer.py")
    assert "_slew_lip_open" in s
    assert "lip_open = self._slew_lip_open(lip_open)" in s


def test_renderer_applies_mouth_peak():
    s = _src("avatar/live2d_renderer.py")
    assert "get_mouth_peak()" in s
    assert "val = min(val, get_mouth_peak())" in s


def test_speaking_stop_resets_slew():
    """停止说话要把收音状态清掉，否则下一句第一帧会从上一句末尾爬。"""
    s = _src("avatar/live2d_renderer.py")
    assert "_mouth_slew_value = 0.0" in s


def test_pet_configures_mouth_peak():
    s = _src("pet.py")
    assert "set_mouth_peak(" in s
    # 2026-09-19：启动改为走**统一热生效入口**（不再各自单点调用），
    # 手改 config.json 也能生效
    assert "self._apply_runtime_config()" in s
    assert '"lip_sync"' in s


def test_config_templates_carry_lip_sync():
    import json

    cfg = json.loads((ROOT / "config.template.json").read_text(encoding="utf-8"))
    assert cfg.get("lip_sync", {}).get("mouth_peak", None) == 1.0
