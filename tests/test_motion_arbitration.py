"""接线测试：动作仲裁保护（选项 A）

2026-09-10 实测问题：

    22:23:56  播放动作 idx=2（waving）   ← AI 点名的动作
    22:23:57  播放动作 idx=1（happy）    ← 1 秒后被情绪顶掉
    22:23:58  播放动作 idx=0（idle）     ← 再 1 秒后被 idle 顶掉

根因：`play_anim` / `_start_motion_at` **不经过 MotionMixer**，而 `exclusive=True`
会无条件 `StopAllMotions()`。混流层建好了，只是这条通道没问过它。

选项 A：AI 明确点名的动作（Layer.DIALOG，带 duration）在它自己声明的时长内，
不被情绪/状态驱动的重播顶掉。动作本人（from_arbiter=True）与用户手动
（force_restart=True）放行。
"""
from __future__ import annotations

import time
import types

import pytest

from avatar.live2d_renderer import Live2DRenderer
from avatar.motion_mixer import Layer, MotionMixer, MotionRequest


class _FakeModel:
    """记录底层调用。"""

    def __init__(self):
        self.start_calls = []
        self.stop_calls = 0
        self.reset_calls = 0

    def StartMotion(self, group, idx, prio):
        self.start_calls.append((group, idx, prio))

    def StopAllMotions(self):
        self.stop_calls += 1

    def ResetExpressions(self):
        self.reset_calls += 1


def _renderer():
    r = Live2DRenderer.__new__(Live2DRenderer)
    r._motion_files = [
        "motions/idle.motion3.json",
        "motions/happy.motion3.json",
        "motions/waving.motion3.json",
    ]
    r._motion_groups = {"": []}
    r._motion_group_name = ""
    r._mixer = MotionMixer()
    r._param_intent = {}
    r._current_motion_idx = None
    r._motion_is_idle = True
    r._expression_active = False
    r._last_expression = ""
    r._live2d = types.SimpleNamespace(
        MotionPriority=types.SimpleNamespace(IDLE=1, NORMAL=2, FORCE=3)
    )
    r._model = _FakeModel()
    # 隔离：本测试只关心仲裁判定，不关心叠加层簿记
    r._note_motion_started = lambda fname="", is_idle=False: None
    return r


def _submit_dialog(r, duration=3.0):
    return r._mixer.submit(MotionRequest(
        layer=Layer.DIALOG, motion_group="waving",
        params={"smile": 80.0}, duration=duration, name="intent",
    ))


# ══════════════════════════════════════════════════════════════
#  保护生效
# ══════════════════════════════════════════════════════════════

def test_ai_action_is_blocked_from_being_stomped():
    r = _renderer()
    assert _submit_dialog(r)

    # 动作本人：放行（from_arbiter）
    assert r._start_motion_at(2, exclusive=True, from_arbiter=True) is True
    stops_after_action = r._model.stop_calls

    # 情绪/状态驱动的重播：被拦，且**不得清场**
    assert r._start_motion_at(1, exclusive=True) is False
    assert r._model.stop_calls == stops_after_action, "被拦时不应 StopAllMotions"


def test_blocked_replay_does_not_start_motion():
    r = _renderer()
    _submit_dialog(r)
    r._start_motion_at(2, exclusive=True, from_arbiter=True)
    n = len(r._model.start_calls)

    r._start_motion_at(0, exclusive=True)

    assert len(r._model.start_calls) == n, "被拦时不应启动新 motion"


def test_action_itself_is_allowed():
    """submit_motion_request 走 from_arbiter=True，不能被自己的保护拦住。"""
    r = _renderer()
    req = MotionRequest(layer=Layer.DIALOG, motion_group="waving",
                        params={"smile": 80.0}, duration=3.0, name="intent")

    assert r.submit_motion_request(req) is True
    assert (r._motion_group_name, 2, 2) in r._model.start_calls


def test_force_restart_bypasses_protection():
    """用户手动点菜单 = 明确意图，应能顶掉。"""
    r = _renderer()
    _submit_dialog(r)
    r._start_motion_at(2, exclusive=True, from_arbiter=True)

    assert r._start_motion_at(1, exclusive=True, force_restart=True) is True


# ══════════════════════════════════════════════════════════════
#  保护不该误伤
# ══════════════════════════════════════════════════════════════

def test_no_ai_action_means_normal_behavior():
    """无 AI 动作在场（层 IDLE）时行为完全不变。"""
    r = _renderer()
    assert r._start_motion_at(0, exclusive=True, from_arbiter=True) is True
    assert r._start_motion_at(1, exclusive=True) is True, "无保护对象时不得拦"


def test_bounded_request_is_protected_regardless_of_layer():
    """★ 语义变更（2026-09-11）：判据从「层≥DIALOG」改为「声明了时长」。

    上一版用层当代理，结果是：behavior_mixin 的 USER_INITIATED（4）+ duration=0
    会永久锁死 mixer（详见 test_arbitration_not_stuck.py）。

    现在只问一件事：**这个请求自己声明了时长吗？**
    声明了 = 它是一段有头有尾的表演，值得让它演完（无论来自哪一层）；
    没声明 = “直到被替换”，那正是应当允许替换的情形。

    注：SCREEN 层目前实际未被任何地方提交（全仓只有 DIALOG 与
    USER_INITIATED 两处），本条覆盖的是判据本身的语义。
    """
    r = _renderer()
    r._mixer.submit(MotionRequest(layer=Layer.SCREEN, duration=3.0, name="screen"))

    assert r._start_motion_at(0, exclusive=True) is False, "声明了时长就应受保护"


def test_unbounded_lower_layer_does_not_protect():
    """反向：没声明时长的低层请求不构成保护。"""
    r = _renderer()
    r._mixer.submit(MotionRequest(layer=Layer.SCREEN, name="screen"))

    assert r._start_motion_at(0, exclusive=True) is True


def test_protection_expires_with_duration():
    """保护随 duration 到期自动解除——这是选项 A 的关键：不是永久封锁。"""
    r = _renderer()
    _submit_dialog(r, duration=0.05)
    r._start_motion_at(2, exclusive=True, from_arbiter=True)

    assert r._start_motion_at(1, exclusive=True) is False, "有效期内应被拦"
    time.sleep(0.25)   # 裕量取大，避开 Windows monotonic 的 15.6ms 粒度
    assert r._start_motion_at(0, exclusive=True) is True, "到期后应放行"


# ══════════════════════════════════════════════════════════════
#  play_anim 是受保护的那条通道
# ══════════════════════════════════════════════════════════════

def test_play_anim_respects_arbitration():
    """情绪驱动的 play_anim 正是需要被拦的通道。"""
    r = _renderer()
    _submit_dialog(r)
    r._start_motion_at(2, exclusive=True, from_arbiter=True)

    assert r.play_anim("happy") is False, "AI 动作活跃期内，情绪动画不得抢占"


def test_play_anim_works_when_no_action_active():
    r = _renderer()
    assert r.play_anim("happy") is True
