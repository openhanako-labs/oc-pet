"""回归锁定：force_idle 在已处于目标状态时无事可做

2026-09-11 实测问题：

    bubble_mixin 在**每个** state == "idle" 事件都调 renderer.force_idle()，
    而 Hanako 会在数秒内连推多次。

    实测：3 秒内 4 次 force_idle，每次 = 三重 ResetExpressions
    + 双重 StopAllMotions + FORCE 优先级 StartMotion。
    而那时其实已经 idle、没有表情要清 —— 唯一效果是把正在 loop 的 idle
    从头重播（一次可见的抽动）。

    与 _start_idle 无去重是同一类问题：**两条路，一条有保护一条没有**。

不变量（本次只加这一个）：
    已经在目标状态时，force_idle 无事可做。
"""

from __future__ import annotations

import types

import pytest

from avatar.live2d_renderer import Live2DRenderer
from avatar.motion_mixer import Layer, MotionMixer, MotionRequest


class _FakeModel:
    """记录清场调用次数。"""

    def __init__(self):
        self.reset_count = 0
        self.stop_count = 0
        self.start_count = 0

    def ResetExpressions(self):
        self.reset_count += 1

    def StopAllMotions(self):
        self.stop_count += 1

    def GetMotions(self):
        return {"": [{"File": "motions/idle.motion3.json"}]}

    def StartMotion(self, *a, **k):
        self.start_count += 1

    def StartRandomMotion(self, *a, **k):
        self.start_count += 1


def _renderer(motion_is_idle=True, expression_active=False, mixer=None):
    r = Live2DRenderer.__new__(Live2DRenderer)
    r._model = _FakeModel()
    r._mixer = mixer if mixer is not None else MotionMixer()
    r._motion_is_idle = motion_is_idle
    r._expression_active = expression_active
    r._last_expression = ""
    r._expression_suppress_until = 0.0
    r._motion_groups = {"": []}
    r._motion_files = ["motions/idle.motion3.json"]
    r._live2d = types.SimpleNamespace(
        MotionPriority=types.SimpleNamespace(IDLE=1, NORMAL=2, FORCE=3),
        MotionGroup=types.SimpleNamespace(IDLE="Idle"),
    )
    r._last_idle_start_at = 0.0
    r._note_motion_started = lambda fname="", is_idle=False: None
    return r


# ══════════════════════════════════════════════════════════════
#  核心：已处于目标状态 → 无事可做
# ══════════════════════════════════════════════════════════════

def test_already_idle_is_noop():
    """已 idle + 无表情 + mixer 空闲 → 不清场。"""
    r = _renderer()

    r.force_idle()

    assert r._model.reset_count == 0, "无事可做时不得 ResetExpressions"
    assert r._model.stop_count == 0, "无事可做时不得 StopAllMotions"
    assert r._model.start_count == 0, "无事可做时不得重启 idle motion"


def test_repeated_calls_all_noop():
    """复现 bubble_mixin 连调：已 idle 时连调 8 次都不该干活。"""
    r = _renderer()

    for _ in range(8):
        r.force_idle()

    assert r._model.reset_count == 0
    assert r._model.start_count == 0


# ══════════════════════════════════════════════════════════════
#  保护不得把清场能力锁死
# ══════════════════════════════════════════════════════════════

def test_stuck_expression_still_forced():
    """有活跃表情要清 → 必须照常清（保留强制能力）。"""
    r = _renderer(expression_active=True)

    r.force_idle()

    assert r._model.reset_count > 0, "有活跃表情时必须清场"
    assert r._expression_active is False, "清场后簿记应复位"


def test_non_idle_motion_still_forced():
    """正在播非 idle 动作 → 必须能强制回 idle。"""
    r = _renderer(motion_is_idle=False)

    r.force_idle()

    assert r._model.stop_count > 0, "有非 idle 动作时必须清场"
    assert r._model.start_count > 0, "并重新起 idle"


def test_active_mixer_request_still_forced():
    """mixer 有活跃请求 → 必须能强制回落。"""
    mixer = MotionMixer()
    mixer.submit(MotionRequest(layer=Layer.DIALOG, motion_group="waving", name="t"))
    r = _renderer(mixer=mixer)
    assert not mixer.is_idle(), "前置条件：mixer 应有活跃请求"

    r.force_idle()

    assert r._model.stop_count > 0, "mixer 有活跃请求时必须清场"


# ══════════════════════════════════════════════════════════════
#  清一次之后就该安静
# ══════════════════════════════════════════════════════════════

def test_after_one_force_subsequent_calls_noop():
    """清场一次后进入目标状态，后续调用应全部无操作。"""
    r = _renderer(expression_active=True)

    r.force_idle()
    after_first = r._model.reset_count
    assert after_first > 0

    for _ in range(5):
        r.force_idle()

    assert r._model.reset_count == after_first, "清完之后不得再重复清场"
