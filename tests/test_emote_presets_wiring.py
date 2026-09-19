"""接线测试：AI 可点名表情预设（[do:预设名]）

2026-09-11 背景：

    avatar/emote_presets.py 里有 53 个带步骤序列的预设
    （wink / blush_shy / pout / head_tilt / gaze_shift / …），
    比裸参数（smile=80）表现力强得多。

    但它们**只能被 _pick_emote_preset() 随机挑中**，AI 点不了名：
    `_trigger_gesture` 只查 EXPRESSION_MAP（情绪名）和 motion 文件名，
    从不查预设表。

    而且这些预设是**纯参数驱动**（缺参数按白名单跳过），
    不依赖模型是否有对应 motion 文件 —— 比动作更可靠。
"""
from __future__ import annotations

import types

import pytest

from avatar.live2d_renderer import Live2DRenderer


def _renderer(preset_ok=True):
    r = Live2DRenderer.__new__(Live2DRenderer)
    r._model = None
    r._motion_files = []
    r._motion_groups = {}
    r._live2d = None
    r._current_anim = ""
    r._expression_active = False
    r._last_expression = ""
    r._emotion_target = ""
    r._emotion_motion_cooldown = {}
    # 记录预设播放
    r._preset_calls = []

    def _play_emote(steps, name=""):
        r._preset_calls.append(steps if isinstance(steps, str) else name)
        return preset_ok

    r.play_emote_sequence = _play_emote
    return r


# ══════════════════════════════════════════════════════════════
#  预设表本身
# ══════════════════════════════════════════════════════════════

def test_presets_are_available():
    """预设表非空，且可用 available_presets 取到。"""
    from avatar.emote_presets import LIVE2D_PRESETS

    assert len(LIVE2D_PRESETS) >= 40, "emote_presets 应有几十个预设"

    r = _renderer()
    names = r.available_presets
    assert "wink" in names
    assert "blush_shy" in names
    assert names == sorted(names), "应稳定排序（prompt 输出要可复现）"


# ══════════════════════════════════════════════════════════════
#  [do:预设名] 走预设，而不是 motion
# ══════════════════════════════════════════════════════════════

@pytest.mark.parametrize("name", ["wink", "blush_shy", "pout", "head_tilt", "gaze_shift"])
def test_do_preset_plays_emote_sequence(name):
    r = _renderer()

    assert r._trigger_gesture(name, 0.6) is True
    assert r._preset_calls == [name], f"{name} 应走表情预设"


def test_preset_takes_priority_over_anim():
    """预设优先于 motion 名 —— 粒度更细，且不依赖模型有对应 motion。"""
    r = _renderer()

    # "wink" 不在 _ANIM_TO_MOTION_KW 里，若不走预设会静默失败
    assert r._trigger_gesture("wink", 0.5) is True
    assert "wink" in r._preset_calls


def test_unknown_name_still_falls_through():
    """既不是预设也不是 motion 的名字 → 返回 False（不得谎报成功）。"""
    r = _renderer()

    assert r._trigger_gesture("no_such_thing", 0.5) is False
    assert r._preset_calls == []


def test_preset_failure_still_tries_other_paths():
    """预设播放失败时不得立即放弃——仍应继续尝试 motion 路径。"""
    r = _renderer(preset_ok=False)
    tried = []
    r.play_anim = lambda anim, emotion="", frame_range=None: (tried.append(anim), False)[1]

    assert r._trigger_gesture("wink", 0.5) is False
    assert "wink" in tried, "预设失败后应继续走 play_anim，而不是提前 return"


# ══════════════════════════════════════════════════════════════
#  prompt 必须把「能点什么」告诉 AI
# ══════════════════════════════════════════════════════════════

class _StubRenderer:
    """最小 renderer stub（只提供 prompt 所需的常量）。"""

    from avatar.live2d_renderer import Live2DRenderer as _L
    _AI_DO_PROMPT = _L._AI_DO_PROMPT


def test_action_prompt_mentions_action_tag():
    """只接线不改 prompt 等于没接——AI 不知道能点什么。

    2026-09-11 变更：prompt 不再列 53 个预设名（实测那样 `[do:]` 全历史
    0 次使用），改为少量语义标签。
    2026-09-19 再修：实测模型从不输出 `[do:]`，一直是 `[action:{...}]`
    （79 条检测里 31 vs 0），所以教学标签改成后者，白名单跟着走。
    本测试只锁「必须提到真在用的那条标签且给出选项与字段名」，
    具体选项集与别名映射由 test_do_aliases.py 覆盖。
    """
    from core.harness_adapter import HanakoPetAdapter

    a = HanakoPetAdapter.__new__(HanakoPetAdapter)
    a._renderer = _StubRenderer()

    prompt = a._build_action_prompt()

    assert "[action:" in prompt
    assert "gesture" in prompt, "要给出字段名"
    assert "[do:" not in prompt, "停教已无人使用的旧缩写"
    assert "害羞" in prompt, "语义选项必须出现在 prompt 里"
    assert "[feel:" in prompt, "示例应体现动作与 [feel:] 搭配"


def test_action_prompt_survives_missing_renderer():
    from core.harness_adapter import HanakoPetAdapter

    a = HanakoPetAdapter.__new__(HanakoPetAdapter)
    a._renderer = None

    assert a._build_action_prompt() == ""
