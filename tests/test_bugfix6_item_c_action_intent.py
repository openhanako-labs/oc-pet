# -*- coding: utf-8 -*-
"""BugFix #6-C 单测：结构化动作意图 [action:{...}]（任意动作）。

- parse_action_intent 解析 [action:{...}]：提取 gesture/intensity/params；
  非法 JSON / 无指令 → 只剥标签、返回 (cleaned, None)、不抛异常。
- 向后兼容 [emotion:xxx]：parse_action_intent 不剥离 emotion 标签，
  emotion 解析链路（harness_adapter.parse_emotion）不受影响。
- Live2DRenderer.apply_action_intent：params 目标值按 intensity 缩放写入
  _param_intent（由每帧平滑插值消费）；gesture 名触发 play_anim。
- SpriteRenderer.apply_action_intent：gesture → play_anim；params 忽略。
"""
import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication  # noqa: E402

_app = QApplication.instance() or QApplication([])  # 复用全局单例（与既有测试一致）

from core.conversation_engine import ConversationEngine  # noqa: E402
from avatar.live2d_renderer import Live2DRenderer  # noqa: E402
from avatar.sprite_renderer import SpriteRenderer  # noqa: E402


def _parse(reply):
    """parse_action_intent 不依赖实例状态，用 None 作 self 直接调用。"""
    return ConversationEngine.parse_action_intent(None, reply)


# ── parse_action_intent（纯函数逻辑）──


def test_parse_action_intent_basic():
    reply = '好的[action:{"gesture":"wave","intensity":0.8,"params":{"ParamAngleX":15}}]'
    cleaned, intent = _parse(reply)
    assert intent is not None
    assert intent["gesture"] == "wave"
    assert intent["intensity"] == 0.8
    assert intent["params"] == {"ParamAngleX": 15}
    assert "[action:" not in cleaned
    assert cleaned.strip() == "好的"


def test_parse_action_intent_invalid_json_safe():
    reply = "来看[action:{bad json}]这个"
    cleaned, intent = _parse(reply)
    assert intent is None
    # 非法 JSON 标签被剥掉，正文保留
    assert "[action:" not in cleaned
    assert "来看" in cleaned and "这个" in cleaned


def test_parse_action_intent_none():
    reply = "普通回复没有动作"
    cleaned, intent = _parse(reply)
    assert intent is None
    assert cleaned == reply


def test_parse_action_intent_emotion_tag_preserved():
    """C：向后兼容 —— [emotion:happy] 不被 parse_action_intent 剥离。"""
    reply = "我好开心[emotion:happy][action:{\"gesture\":\"wave\"}]"
    cleaned, intent = _parse(reply)
    assert intent is not None and intent["gesture"] == "wave"
    assert "[emotion:happy]" in cleaned  # emotion 标签保留，交给 parse_emotion


def test_parse_action_intent_params_only():
    reply = '[action:{"params":{"ParamMouthOpenY":0.6}}]结束'
    cleaned, intent = _parse(reply)
    assert intent is not None
    assert intent.get("gesture") is None
    assert intent["params"] == {"ParamMouthOpenY": 0.6}
    assert cleaned.strip() == "结束"


# ── Live2DRenderer.apply_action_intent ──


def test_live2d_apply_action_intent_sets_param_target():
    """C：params 目标值按 intensity 缩放写入 _param_intent。"""
    r = object.__new__(Live2DRenderer)
    r._param_intent = {}
    r.apply_action_intent({"params": {"ParamAngleX": 15, "ParamMouthOpenY": 0.6}, "intensity": 0.5})
    # 目标值 = 原始值 * intensity
    assert r._param_intent == {"ParamAngleX": 7.5, "ParamMouthOpenY": 0.3}


def test_live2d_apply_action_intent_gesture_triggers_motion():
    """C：gesture 名 → 触发 play_anim（含已知情绪名走表情映射）。"""
    r = object.__new__(Live2DRenderer)
    r._param_intent = {}
    r.play_anim = MagicMock()
    r.apply_action_intent({"gesture": "wave"})
    r.play_anim.assert_called_once_with("wave")


def test_live2d_apply_action_intent_invalid_safe():
    r = object.__new__(Live2DRenderer)
    r._param_intent = {}
    # 非 dict → 安全返回，不抛异常
    r.apply_action_intent("not-a-dict")
    assert r._param_intent == {}


# ── SpriteRenderer.apply_action_intent ──


def test_sprite_apply_action_intent_plays_gesture():
    r = object.__new__(SpriteRenderer)
    r.play_anim = MagicMock()
    r.apply_action_intent({"gesture": "wave", "params": {"x": 1}})
    r.play_anim.assert_called_once_with("wave")


def test_sprite_apply_action_intent_ignores_params():
    r = object.__new__(SpriteRenderer)
    r.play_anim = MagicMock()
    # 只有 params 没有 gesture → 不触发播放
    r.apply_action_intent({"params": {"ParamAngleX": 10}})
    r.play_anim.assert_not_called()


def test_sprite_apply_action_intent_invalid_safe():
    r = object.__new__(SpriteRenderer)
    r.play_anim = MagicMock()
    r.apply_action_intent(None)
    r.play_anim.assert_not_called()
