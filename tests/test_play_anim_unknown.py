# -*- coding: utf-8 -*-
"""动作名不认识时，**不许污染动画状态**。

2026-09-19 实测（用户桌宠）：用一个不存在的动作名触发
（`pet_play_anim("___不存在的动作___")`），**桌宠画面整体坏掉**
（帧差 204 = 画面完全变了），而日志里没有任何报错。

根因：`Live2DRenderer.play_anim` **第一行就** `self._current_anim = anim`，
名字却没验证；返回 False 之后调用方 `_set_anim_seq` 又**无视返回值**继续抄状态。
那个 bool 契约渲染器早就写着——"False 表示无匹配（调用方不得声称已触发）"——
**契约在那里，没人守。**
"""
from __future__ import annotations

import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RENDERER = ROOT / "avatar" / "live2d_renderer.py"
ANIM_MIXIN = ROOT / "pet_mixins" / "animation_mixin.py"


def _src(p: Path) -> str:
    return p.read_text(encoding="utf-8")


class _FakeRenderer:
    """只带 `play_anim` 需要的东西。"""

    def __init__(self, playable: bool = True):
        self._current_anim = "idle"
        self._current_emotion = ""
        self._motion_groups = {"": []}
        self._model = None
        self._playable = playable
        self.warned = []

    def _play_motion_kw(self, *kws):
        return self._playable

    def set_emotion_expression_only(self, emotion):
        self._current_emotion = emotion


def _play_anim(rend, anim, emotion=""):
    import avatar.live2d_renderer as mod

    fn = types.MethodType(mod.Live2DRenderer.play_anim, rend)
    # 映射表直接用真的（那才是被测对象的一部分）
    rend._ANIM_TO_MOTION_KW = mod.Live2DRenderer._ANIM_TO_MOTION_KW
    return fn(anim, emotion=emotion)


# ── 核心：失败时不许改状态 ────────────────────────────────


def test_unknown_anim_does_not_touch_current_anim():
    """**这条就是那个 bug**：不认识的名字不能污染 `_current_anim`。"""
    rend = _FakeRenderer(playable=False)
    ok = _play_anim(rend, "___不存在的动作___")
    assert ok is False, "无匹配必须返回 False"
    assert rend._current_anim == "idle", (
        "名字不认识时 _current_anim 必须保持原样——否则渲染循环会去找一个"
        "不存在的序列，画面整体坏掉"
    )


def test_known_anim_sets_state():
    rend = _FakeRenderer(playable=True)
    assert _play_anim(rend, "happy") is True
    assert rend._current_anim == "happy"


def test_known_anim_by_emotion_sets_state():
    """只用 emotion 也能播（映射表里 emotion 是第二候选）。"""
    rend = _FakeRenderer(playable=True)
    assert _play_anim(rend, "___没有___", emotion="happy") is True


def test_unknown_anim_still_applies_emotion_expression():
    """动作不认识、但情绪是认识的时候，表情那一头照旧（两个通道互不牵连）。"""
    rend = _FakeRenderer(playable=False)
    _play_anim(rend, "___没有___", emotion="happy")
    assert rend._current_emotion == "happy"


# ── 调用方必须看返回值 ────────────────────────────────────


def test_set_anim_seq_honours_return_value():
    src = _src(ANIM_MIXIN)
    assert "if not self._renderer.play_anim(seq_name, emotion=emotion):" in src, (
        "_set_anim_seq 必须看返回值（契约：False = 没播，不得继续当播了处理）"
    )
    assert "不是可用动作，已忽略" in src


def test_set_anim_seq_returns_bool():
    src = _src(ANIM_MIXIN)
    i = src.find("def _set_anim_seq")
    body = src[i:i + 2200]
    assert "return False" in body and "return True" in body, (
        "切换结果要能传出去，否则调用方还是在猜"
    )


# ── 源码护栏：先验证、后改状态 ─────────────────────────────


def _code_lines(block: str) -> list:
    """剥掉注释行——**今天的教训**：注释里写了代码字符串，
    会让“按字符串找代码”的断言四处乱报（QTimer.singleShot 已经坑过一次）。"""
    out = []
    for line in block.splitlines():
        s = line.strip()
        if s.startswith("#"):
            continue
        # 行尾注释也去掉（粗糙但够用：本项目注释里的 # 不会出现在字符串里）
        s = s.split("#")[0].strip()
        out.append(s)
    return out


def test_play_anim_does_not_mutate_state_before_match():
    """`_current_anim = anim` 必须出现在“匹配成功”之后（按行号比，不信字符串）。"""
    src = _src(RENDERER)
    i = src.find("def play_anim(self, anim: str, emotion: str = \"\", frame_range=None) -> bool:")
    assert i != -1, "找不到 play_anim"
    j = src.find("def set_emotion_expression_only", i)
    lines = _code_lines(src[i:j])

    idx_assign = next(
        (k for k, s in enumerate(lines) if s == "self._current_anim = anim"), None
    )
    idx_match = next(
        (k for k, s in enumerate(lines) if "_play_motion_kw(*kws)" in s), None
    )
    assert idx_assign is not None, "成功路径仍应设置 _current_anim"
    assert idx_match is not None, "找不到匹配判断"
    assert idx_assign > idx_match, (
        "`_current_anim = anim` 必须在匹配判断之后——放在开头就是那个画面坏掉的 bug"
    )


def test_play_anim_warns_on_unknown_name():
    src = _src(RENDERER)
    i = src.find("def play_anim(self, anim: str, emotion: str = \"\", frame_range=None) -> bool:")
    lines = _code_lines(src[i:src.find("def set_emotion_expression_only", i)])
    assert any("logger.warning" in s for s in lines), "不认识的名字要留痕，不能静默"
