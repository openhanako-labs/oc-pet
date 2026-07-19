"""情绪切换过渡引擎 — 与渲染层解耦。

设计要点：
- 单值 intensity ∈ [0,1]，由 on_update 回调向下游（SpriteRenderer）暴露
- 三种过渡样式：snap（瞬切，向后兼容）/ fade（ease-out）/ spring（欠阻尼弹簧）
- 每帧 tick() 由 QTimer 驱动，不开新线程
- 支持中断：transition 进行中再次 go() 时，从当前 intensity 起步过渡到新 target

使用范式（PetWindow 侧）：
    self._transition = TransitionEngine(on_update=self._renderer.set_alpha)
    # QTimer 连接 tick()
    self._transition.reset(0.0)             # 立即重置（切换动画后立即不可见）
    self._transition.go(1.0, style="fade")  # 渐显到目标强度
"""

from __future__ import annotations

import math
import time
from typing import Callable

# ── 时长常量（毫秒） ────────────────────────────────────
STYLE_DURATION_MS = {
    "snap":   0,     # 瞬切（向后兼容）
    "fade":   300,   # 缓出
    "spring": 500,   # 弹簧
}

# ── 弹簧曲线参数 ────────────────────────────────────
# 欠阻尼二阶系统：x(t) = 1 - e^(-ζω_n·t) * cos(ω_d·t)
# ζ=0.55 → 约 15% 过冲（1 个明显反弹后收敛，适合“惊讶/生气”）
# ω_n=14 → 500ms 内约 1.7 个振荡周期，起跳明显但不拉档
_SPRING_ZETA = 0.55
_SPRING_WN = 14.0


class TransitionEngine:
    """情绪切换过渡引擎。"""

    def __init__(self, on_update: Callable[[float], None]):
        """on_update: 回调，参数 intensity ∈ [0,1]。"""
        if not callable(on_update):
            raise TypeError("on_update must be callable")

        self._cb = on_update
        self._from = 0.0
        self._to = 1.0
        self._t0 = 0.0          # monotonic 时间戳
        self._duration_ms = 0
        self._style = "snap"
        self._active = False
        self._last_intensity = 1.0  # 上一个稳定值（无 transition 时返回）

    # ── 公共接口 ──

    def go(self, target: float = 1.0, style: str = "fade") -> None:
        """开始过渡到目标强度。

        支持中断：从当前实际 intensity（_current_intensity）出发，
        过渡到新 target。多次 go() 会取最后一次。

        Args:
            target: 目标强度 ∈ [0,1]
            style: snap / fade / spring
        """
        target = _clamp01(target)
        style = style if style in STYLE_DURATION_MS else "snap"

        # 从当前位置出发（支持打断已进行中的过渡）
        self._from = self._current_intensity()
        self._to = target
        self._t0 = time.monotonic()
        self._style = style
        self._duration_ms = STYLE_DURATION_MS[style]

        if self._duration_ms == 0:
            # snap：直接到目标
            self._active = False
            self._last_intensity = target
            self._cb(target)
        else:
            self._active = True
            # 立即触发一次（避免第一帧卡在旧值）
            self._emit_now()

    def reset(self, value: float = 0.0) -> None:
        """立即重置（停止当前过渡），value 直接生效。

        用法：切换动画序列前调用 reset(0.0)，让旧帧立即不可见，
        再调用 go(1.0, style) 让新帧淡入。
        """
        value = _clamp01(value)
        self._active = False
        self._last_intensity = value
        self._from = value
        self._to = value
        self._cb(value)

    def tick(self) -> None:
        """每帧调用（推荐 16ms / 60fps，50ms 也可）。

        计算当前 intensity 并通过 on_update 下发。
        无活动 transition 时直接 return，不产生回调。
        """
        if not self._active:
            return

        elapsed_ms = (time.monotonic() - self._t0) * 1000.0
        if elapsed_ms >= self._duration_ms:
            # 到点：稳定到目标值
            self._last_intensity = self._to
            self._active = False
            self._cb(self._to)
            return

        t = elapsed_ms / self._duration_ms
        eased = self._ease(t)
        cur = self._from + (self._to - self._from) * eased
        self._last_intensity = cur
        self._cb(cur)

    def is_active(self) -> bool:
        """是否有进行中的过渡。"""
        return self._active

    def get_current(self) -> float:
        """查询当前 intensity（含进行中的过渡）。"""
        return self._current_intensity()

    # ── 内部 ──

    def _current_intensity(self) -> float:
        """计算当前 intensity（不触发回调）。"""
        if not self._active:
            return self._last_intensity
        elapsed_ms = (time.monotonic() - self._t0) * 1000.0
        if elapsed_ms >= self._duration_ms:
            return self._to
        t = elapsed_ms / self._duration_ms
        eased = self._ease(t)
        return self._from + (self._to - self._from) * eased

    def _emit_now(self) -> None:
        """立即触发一次回调（用当前位置）。"""
        cur = self._current_intensity()
        self._last_intensity = cur
        self._cb(cur)

    def _ease(self, t: float) -> float:
        """过渡曲线：返回 t∈[0,1] 对应的进度 ∈[0,1]。"""
        if self._style == "fade":
            # ease-out cubic: 头快尾慢
            return 1.0 - (1.0 - t) ** 3
        if self._style == "spring":
            return _spring_curve(t)
        return t


# ── 工具函数 ────────────────────────────────────

def _clamp01(x: float) -> float:
    if x < 0.0:
        return 0.0
    if x > 1.0:
        return 1.0
    return x


def _spring_curve(t: float) -> float:
    """欠阻尼弹簧曲线，t∈[0,1]，输出 ∈ [~0.92, ~1.0]（自然过冲+收敛）。

    起点 f(0)=0；终点 f(1)≈1.000（小过冲由 cos 收敛到 ~0）；
    过程中有 ~1.7 个振荡周期，振幅随时间衰减。
    """
    if t <= 0.0:
        return 0.0
    if t >= 1.0:
        return 1.0
    zeta = _SPRING_ZETA
    wn = _SPRING_WN
    wd = wn * math.sqrt(1.0 - zeta * zeta)
    decay = math.exp(-zeta * wn * t)
    return 1.0 - decay * math.cos(wd * t)


# ── 单元测试小工具 ────────────────────────────────────

def _self_check() -> bool:
    """引擎自检：snap 必须瞬切，fade 必须单调递增，spring 必须首尾对。"""
    # snap
    eng = TransitionEngine(lambda v: None)
    eng.go(0.5, "snap")
    assert abs(eng.get_current() - 0.5) < 1e-6, "snap should be instant"

    # fade 单调 + ease-out 特征（50% 时间约 87.5% 进度）
    eng = TransitionEngine(lambda v: None)
    eng._from = 0.0
    eng._to = 1.0
    eng._t0 = time.monotonic() - 0.15  # 50% 已过去
    eng._duration_ms = STYLE_DURATION_MS["fade"]
    eng._style = "fade"
    eng._active = True
    mid = eng.get_current()
    assert 0.8 < mid < 0.95, f"fade mid should be ease-out high (≈0.875), got {mid}"

    # fade 中段确认单调性（25% 时间约 58%）
    eng._t0 = time.monotonic() - 0.075
    early = eng.get_current()
    assert early < mid, "fade should be monotonic"

    # spring 起止
    eng = TransitionEngine(lambda v: None)
    eng._from = 0.0
    eng._to = 1.0
    eng._t0 = time.monotonic() - 0.0
    eng._duration_ms = STYLE_DURATION_MS["spring"]
    eng._style = "spring"
    eng._active = True
    s0 = eng.get_current()
    assert abs(s0) < 1e-6, f"spring start should be 0, got {s0}"

    eng._t0 = time.monotonic() - 1.0  # 远超 duration
    s_end = eng.get_current()
    assert abs(s_end - 1.0) < 1e-3, f"spring end should be 1, got {s_end}"

    # reset
    eng = TransitionEngine(lambda v: None)
    eng.go(1.0, "fade")
    eng.reset(0.3)
    assert eng.get_current() == 0.3, f"reset should snap, got {eng.get_current()}"
    assert not eng.is_active(), "reset should deactivate"

    return True


if __name__ == "__main__":
    if _self_check():
        print("emotion_transitions: self-check passed")
    else:
        raise SystemExit(1)
