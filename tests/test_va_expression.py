"""接线测试：连续 VA 表达通道（[feel:] / [do:]）

2026-09-10 设计（DESIGN-expression.md）第 1+2 步。

背景：oc-pet 的连续表情层早就建好并逐帧在跑——

    _EMOTION_VA（7 档情绪 → VA 坐标）
    self._va_cur / self._va_target
    _va_interpolate_targets()（取最近两档做线性插值）
    逐帧：_va_target ──平滑──▶ _va_cur ──插值──▶ 面部参数

但 `_va_target` 只有一个来源：`_EMOTION_VA.get(emotion)` —— 从离散标签查表。
AI 只能精确命中 7 个坐标点，说不出「有点开心但不太兴奋」。

本测试锁住新通道：
- `[feel:v,a]` 解析并驱动 `_va_target`
- 保持期内不被离散情绪覆盖（否则新通道会被旧通道踩掉）
- `[do:name]` → gesture
- 旧标签仍向后兼容
"""
from __future__ import annotations

import time

import pytest

from core.conversation_engine import ConversationEngine


def _engine():
    """只测纯解析逻辑，不初始化完整引擎。"""
    return ConversationEngine.__new__(ConversationEngine)


# ══════════════════════════════════════════════════════════════
#  解析：[feel:v,a]
# ══════════════════════════════════════════════════════════════

def test_feel_tag_parsed():
    cleaned, intent = _engine().parse_action_intent("有点开心 [feel:0.5,0.3]")

    assert intent is not None
    assert intent["va"] == [0.5, 0.3]
    assert "[feel:" not in cleaned
    assert "有点开心" in cleaned


def test_feel_tag_accepts_negatives():
    _, intent = _engine().parse_action_intent("哼 [feel:-0.6,0.8]")

    assert intent["va"] == [-0.6, 0.8]


def test_feel_tag_accepts_leading_dot_and_spaces():
    _, intent = _engine().parse_action_intent("[feel: .5 , -.25 ]")

    assert intent["va"] == [0.5, -0.25]


def test_feel_values_clamped():
    """越界值夹紧，不抛异常。"""
    _, intent = _engine().parse_action_intent("[feel:5,-9]")

    assert intent["va"] == [1.0, -1.0]


def test_malformed_feel_tag_is_stripped_not_crashing():
    """数值非法 → 无 va，但标签仍需剥掉（不能漏给用户看）。"""
    cleaned, intent = _engine().parse_action_intent("你好 [feel:abc,def]")

    assert "[feel:" not in cleaned
    assert intent is None or "va" not in intent


def test_single_value_feel_is_rejected():
    """只给一个数不构成 VA 坐标。"""
    _, intent = _engine().parse_action_intent("[feel:0.5]")

    assert intent is None or "va" not in intent


# ══════════════════════════════════════════════════════════════
#  解析：[do:name]
# ══════════════════════════════════════════════════════════════

def test_do_tag_maps_to_gesture():
    cleaned, intent = _engine().parse_action_intent("好呀 [feel:0.7,0.6] [do:waving]")

    assert intent["gesture"] == "waving"
    assert intent["va"] == [0.7, 0.6]
    assert "[do:" not in cleaned


def test_do_tag_does_not_override_action_json():
    """[action:{...}] 优先级更高，[do:] 不覆盖它。"""
    reply = '[do:idle] [action:{"gesture":"waving","intensity":0.8}]'
    _, intent = _engine().parse_action_intent(reply)

    assert intent["gesture"] == "waving"


def test_do_tag_case_and_space_tolerant():
    _, intent = _engine().parse_action_intent("[do: Waving ]")

    assert intent["gesture"] == "waving"


# ══════════════════════════════════════════════════════════════
#  向后兼容：旧标签仍能解析
# ══════════════════════════════════════════════════════════════

def test_legacy_expression_still_parsed():
    _, intent = _engine().parse_action_intent(
        "[expression:smile=80,blush=40] [duration:3]"
    )

    assert intent["params"]["smile"] == 80.0
    assert intent["params"]["blush"] == 40.0
    assert intent["duration"] == 3.0


def test_legacy_action_json_still_parsed():
    reply = '[action:{"gesture":"waving","intensity":0.7}] 举手啦'
    _, intent = _engine().parse_action_intent(reply)

    assert intent["gesture"] == "waving"


def test_plain_reply_yields_no_intent():
    cleaned, intent = _engine().parse_action_intent("普通的一句话")

    assert intent is None
    assert cleaned == "普通的一句话"


# ══════════════════════════════════════════════════════════════
#  渲染器：VA 真的驱动 _va_target，且不被离散情绪覆盖
# ══════════════════════════════════════════════════════════════

def _renderer():
    from avatar.live2d_renderer import Live2DRenderer

    r = Live2DRenderer.__new__(Live2DRenderer)
    r._model = None
    r._motion_files = []
    r._motion_groups = {}
    r._live2d = None
    r._param_intent = {}
    r._current_emotion = "neutral"
    r._emotion_target = "neutral"
    r._last_expression = ""
    r._expression_active = False
    r._va_target = (0.0, 0.0)
    r._va_cur = (0.0, 0.0)
    r._va_hold_until = 0.0
    return r


def test_feel_sets_va_target():
    r = _renderer()
    r.apply_action_intent({"va": [0.5, 0.3]})

    assert r._va_target == (0.5, 0.3), "连续 VA 必须真的写进渲染器"


def test_va_survives_discrete_emotion_overwrite():
    """核心断言：新通道不能被旧通道踩掉。

    真实调用顺序（pet.py:2851 → 2873）：
      apply_action_intent([feel:...])  →  设 _va_target
      _sync_renderer_master_emotion()  →  set_master_emotion 用离散 emotion 查表
    没有保持机制的话，后者会立刻盖掉前者。
    """
    r = _renderer()
    r.apply_action_intent({"va": [0.5, 0.3]})

    r.set_master_emotion("happy")     # 真实链路会紧接着调它

    assert r._va_target == (0.5, 0.3), "离散情绪不得覆盖保持期内的连续 VA"


def test_set_emotion_also_respects_hold():
    r = _renderer()
    r.apply_action_intent({"va": [-0.5, -0.4]})

    r.set_emotion("angry")

    assert r._va_target == (-0.5, -0.4)


def test_hold_expires_and_emotion_wins_again():
    """保持不是永久封锁——到期后离散情绪恢复正常写入。"""
    r = _renderer()
    r.apply_action_intent({"va": [0.5, 0.3]})
    r._va_hold_until = time.monotonic() - 0.01   # 模拟已到期

    r.set_master_emotion("sad")

    assert r._va_target == r._EMOTION_VA["sad"], "到期后应恢复查表行为"


def test_hold_duration_follows_declared_duration():
    r = _renderer()
    before = time.monotonic()
    r.apply_action_intent({"va": [0.5, 0.3], "duration": 5.0})

    assert r._va_hold_until >= before + 4.9


def test_no_feel_means_normal_behavior():
    """不给 [feel:] 时行为完全不变。"""
    r = _renderer()
    r.set_master_emotion("happy")

    assert r._va_target == r._EMOTION_VA["happy"]


def test_invalid_va_is_ignored_safely():
    r = _renderer()
    r._va_target = (0.1, 0.1)

    r.apply_action_intent({"va": ["x", "y"]})
    r.apply_action_intent({"va": [1]})
    r.apply_action_intent({"va": "nope"})

    assert r._va_target == (0.1, 0.1), "非法 va 不得改动现有目标"


def test_va_coexists_with_gesture():
    """[feel:] + [do:] 应同时生效（连续表情 + 离散动作）。"""
    from avatar.motion_mixer import MotionMixer

    r = _renderer()
    r._motion_files = ["motions/waving.motion3.json"]
    r._note_motion_started = lambda *a, **k: None
    r._mixer = MotionMixer()

    r.apply_action_intent({"va": [0.7, 0.6], "gesture": "waving"})

    assert r._va_target == (0.7, 0.6)
