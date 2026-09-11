"""回归锁定：仲裁保护不得被「无时长的请求」锁死

2026-09-11 实测（本次会话真实撞到）：

    behavior_mixin 提交主动挥手时用：
        layer=Layer.USER_INITIATED    # = 4，最高
        duration 缺省 = 0.0           # 「直到被替换」→ 永不过期

    而上一版的仲裁保护判据是 `get_active_layer() >= Layer.DIALOG`（3）。
    4 >= 3 → **一次主动挥手之后，所有 play_anim / 自动动作永久被拦**。

    实测：15 分钟内 25 次随机动作只播了 2 次，且没有任何报错。
    而 `自动随机动作` 那行日志是无条件打的，所以从日志看不出被拦。

三层缺陷：
  1. 判据用「层」当代理 —— 层高 ≠ 有时长
  2. duration=0 的请求永不过期 → mixer 状态永久 stale
  3. is_idle() 读同一状态 → 永久报「不空闲」

不变量（本次只加这一个，替换掉上一版）：
    **只有「声明了时长且尚未到期」的请求才值得保护。**
    duration<=0 的语义就是「随时可被替换」，拦它反而是 bug。
"""

from __future__ import annotations

import time

import pytest

from avatar.motion_mixer import Layer, MotionMixer, MotionRequest


# ══════════════════════════════════════════════════════════════
#  has_bounded_active：判据本身
# ══════════════════════════════════════════════════════════════

def test_no_active_is_not_bounded():
    assert MotionMixer().has_bounded_active() is False


def test_unbounded_request_is_not_protected():
    """实测的元凶：USER_INITIATED + duration=0。"""
    m = MotionMixer()
    m.submit(MotionRequest(
        layer=Layer.USER_INITIATED, motion_group="waving", name="proactive_waving",
    ))

    assert m.has_bounded_active() is False, (
        "无时长的请求永不过期，不得作为保护对象——否则一次主动挥手锁死所有动作"
    )
    # 但它确实占着最高层（这正是不能拿层当判据的原因）
    assert m.get_active_layer() == Layer.USER_INITIATED


def test_bounded_request_is_protected():
    m = MotionMixer()
    m.submit(MotionRequest(
        layer=Layer.DIALOG, motion_group="waving", duration=3.0, name="intent",
    ))

    assert m.has_bounded_active() is True


def test_expired_bounded_request_is_not_protected():
    m = MotionMixer()
    m.submit(MotionRequest(
        layer=Layer.DIALOG, motion_group="waving", duration=0.05, name="intent",
    ))
    time.sleep(0.25)   # 裕量取大，避开 Windows monotonic 的 15.6ms 粒度

    assert m.has_bounded_active() is False


def test_force_reset_clears_protection():
    m = MotionMixer()
    m.submit(MotionRequest(layer=Layer.DIALOG, duration=30.0, name="intent"))
    assert m.has_bounded_active() is True

    m.force_reset()

    assert m.has_bounded_active() is False


# ══════════════════════════════════════════════════════════════
#  端到端：保护不再拦死后续动作
# ══════════════════════════════════════════════════════════════

class _FakeModel:
    def __init__(self):
        self.start_calls = 0

    def StartMotion(self, *a, **k):
        self.start_calls += 1

    def StopAllMotions(self):
        pass

    def ResetExpressions(self):
        pass


def _renderer():
    import types

    from avatar.live2d_renderer import Live2DRenderer

    r = Live2DRenderer.__new__(Live2DRenderer)
    r._model = _FakeModel()
    r._mixer = MotionMixer()
    r._motion_files = ["motions/idle.motion3.json", "motions/waving.motion3.json"]
    r._motion_groups = {"": []}
    r._motion_group_name = ""
    r._current_motion_idx = None
    r._motion_is_idle = False     # 关键：非 idle 才走去重分支之外
    r._expression_active = False
    r._last_expression = ""
    r._live2d = types.SimpleNamespace(
        MotionPriority=types.SimpleNamespace(IDLE=1, NORMAL=2, FORCE=3)
    )
    r._note_motion_started = lambda *a, **k: None
    r._last_idle_start_at = 0.0
    return r


def test_unbounded_proactive_does_not_block_later_motions():
    """复现实测场景：先主动挥手（无时长），再跑自动动作 —— 必须能播。"""
    r = _renderer()

    # 1) behavior_mixin 的提交形态：USER_INITIATED + 无 duration
    r._mixer.submit(MotionRequest(
        layer=Layer.USER_INITIATED, motion_group="waving", name="proactive_waving",
    ))

    # 2) 后续自动动作（exclusive=True，非 arbiter）
    assert r._start_motion_at(1, exclusive=True) is True, (
        "无时长的请求不得拦下后续动作——这正是 25 次只播 2 次的成因"
    )


def test_bounded_ai_action_still_protected():
    """修 bug 不得把原本要保护的能力一起弄掉。"""
    r = _renderer()
    r._mixer.submit(MotionRequest(
        layer=Layer.DIALOG, motion_group="waving", duration=3.0, name="intent",
    ))

    assert r._start_motion_at(1, exclusive=True) is False, "AI 声明的时长内仍应受保护"


# ══════════════════════════════════════════════════════════════
#  提交方：主动挥手必须给时长
# ══════════════════════════════════════════════════════════════

def test_behavior_mixin_passes_duration():
    """源码级守卫：两处 USER_INITIATED 提交都必须带 duration。"""
    import pathlib

    src = (pathlib.Path(__file__).resolve().parent.parent
           / "pet_mixins" / "behavior_mixin.py").read_text(encoding="utf-8")

    # 两处：_trigger_proactive_waving 与 screen proactive
    assert src.count("proactive_waving") >= 1
    assert src.count("screen_proactive_waving") >= 1
    assert src.count("duration=float(getattr(renderer") == 2, (
        "两处 USER_INITIATED 提交都必须给 duration——"
        "缺省 0.0 会让 mixer 永久 stale"
    )
