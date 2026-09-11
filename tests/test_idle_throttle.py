"""回归锁定：idle 重播节流

2026-09-11 实测问题：

    IdleLoopProcessor 每帧检查 model.IsMotionFinished()，为真就调
    renderer._start_idle()。而 _start_idle 原本**无条件 StartMotion**
    ——没有去重（对比 _start_motion_at 是有去重的）。

    后果：idle 一秒重播 3 次、一晚 38 次。任何动作都活不过一秒，
    **桌宠在不停地打断自己**。

    这是「两条路，一条有保护一条没有」的第 6 次复现。
"""
from __future__ import annotations

import time
import types

import pytest

from avatar.live2d_renderer import Live2DRenderer


class _FakeModel:
    """记录 StartMotion 调用。"""

    def __init__(self, motions=None):
        self.start_calls = []
        self._motions = motions if motions is not None else {
            "": [
                {"File": "motions/idle.motion3.json"},
                {"File": "motions/happy.motion3.json"},
            ],
        }

    def GetMotions(self):
        return self._motions

    def StartMotion(self, group, idx, prio):
        self.start_calls.append((group, idx, prio))

    def StartRandomMotion(self, *a, **k):
        self.start_calls.append(("random", a, k))


def _renderer(motions=None):
    r = Live2DRenderer.__new__(Live2DRenderer)
    r._model = _FakeModel(motions)
    r._motion_groups = {"": []}
    r._motion_files = ["motions/idle.motion3.json", "motions/happy.motion3.json"]
    r._motion_is_idle = True
    r._current_motion_idx = None
    r._live2d = types.SimpleNamespace(
        MotionPriority=types.SimpleNamespace(IDLE=1, NORMAL=2, FORCE=3)
    )
    r._note_motion_started = lambda fname="", is_idle=False: None
    r._last_idle_start_at = 0.0
    return r


# ══════════════════════════════════════════════════════════════
#  核心：连调只生效一次
# ══════════════════════════════════════════════════════════════

def test_rapid_calls_only_start_once():
    """复现 IdleLoopProcessor 逐帧调用：连调 10 次只应播一次。"""
    r = _renderer()

    for _ in range(10):
        r._start_idle()

    assert len(r._model.start_calls) == 1, (
        f"同一秒内不得重复 StartMotion，实际 {len(r._model.start_calls)} 次"
    )


def test_can_restart_after_interval(monkeypatch):
    """间隔够了应能再次播——节流不是禁止，否则 idle 会卡住不循环。"""
    r = _renderer()

    r._start_idle()
    assert len(r._model.start_calls) == 1

    # 把上次时间戳人为推远，模拟 idle 播完已过间隔
    r._last_idle_start_at = time.monotonic() - (r.IDLE_RESTART_MIN_INTERVAL + 0.1)
    r._start_idle()

    assert len(r._model.start_calls) == 2, "过了最小间隔应能重播"


def test_interval_is_short_enough_for_normal_idle():
    """节流值必须远小于正常 idle motion 长度（2-4s），否则会看出卡顿。"""
    assert 0 < Live2DRenderer.IDLE_RESTART_MIN_INTERVAL <= 1.0


# ══════════════════════════════════════════════════════════════
#  不得误伤
# ══════════════════════════════════════════════════════════════

def test_no_model_is_safe():
    r = _renderer()
    r._model = None

    r._start_idle()  # 不抛异常即可


def test_fallback_path_also_throttled():
    """GetMotions 为空时走 StartRandomMotion 兜底，也要节流。"""
    r = _renderer(motions={})

    for _ in range(5):
        r._start_idle()

    assert len(r._model.start_calls) == 1, "兜底路径同样不得高频重播"


def test_other_motions_unaffected():
    """节流只作用于 idle 重播，普通动作播放不受影响。"""
    r = _renderer()
    r._note_motion_started = lambda fname="", is_idle=False: None
    from avatar.motion_mixer import MotionMixer

    r._mixer = MotionMixer()
    r._param_intent = {}
    r._expression_active = False
    r._last_expression = ""
    r._motion_group_name = ""

    # 连播两个不同动作：都应生效
    assert r._start_motion_at(1, exclusive=True, from_arbiter=True) is True
    r._current_motion_idx = None          # 清去重条件，模拟已结束
    assert r._start_motion_at(1, exclusive=True, from_arbiter=True) is True

    assert len(r._model.start_calls) == 2
