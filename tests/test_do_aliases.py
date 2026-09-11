"""接线测试：语义别名 + 精简 prompt（[do:] 全历史 0 次使用之后）

2026-09-11 实测：

    上一轮接了「AI 可点名 53 个表情预设」，但**实测 `[do:]` 全历史 0 次使用**。
    原因不是通道bug，是 prompt 太长：
    把 53 个预设名 + 13 个 motion 名（共 66 个）挤成一行，无说明、无分类。
    模型无法从 "angry_glare/arm_wave/blink3/blush/blush_deny/blush_shy/…" 里选择。

    而 [feel:] 从 0/14 变 2/2，是因为它给了**明确说明 + 示例**。

    修法（参照 Amadeus 的 TRIGGER_ALIASES）：
    只暴露少量**语义标签**（中文、带说明），内部归一到具体预设/motion。

本测试锁住这个不变量：
    prompt 里给 AI 的选择必须是「少而带语义」的，不是一个长名字表。
"""
from __future__ import annotations

import types

import pytest

from avatar.live2d_renderer import Live2DRenderer


def _renderer(motions=None):
    r = Live2DRenderer.__new__(Live2DRenderer)
    r._model = None
    r._motion_files = motions if motions is not None else []
    r._motion_groups = {"": []}
    r._motion_group_name = ""
    r._live2d = types.SimpleNamespace(
        MotionPriority=types.SimpleNamespace(IDLE=1, NORMAL=2, FORCE=3)
    )
    r._current_anim = ""
    r._current_motion_idx = None
    r._motion_is_idle = True
    r._expression_active = False
    r._last_expression = ""
    r._emotion_target = ""
    r._emotion_motion_cooldown = {}
    r._last_idle_start_at = 0.0
    r._note_motion_started = lambda *a, **k: None
    r._preset_calls = []
    r.play_emote_sequence = lambda steps, name="": (
        r._preset_calls.append(steps if isinstance(steps, str) else name), True
    )[1]
    return r


# ══════════════════════════════════════════════════════════════
#  别名表本身
# ══════════════════════════════════════════════════════════════

def test_alias_table_is_small_and_semantic():
    """别名表要「少」——这是它存在的理由。"""
    n = len(Live2DRenderer._AI_DO_ALIASES)
    assert 10 <= n <= 40, f"别名 {n} 条；太多就回到「选择困难」的老问题"


def test_alias_values_resolve_to_known_targets():
    """每个别名都要指向存在的预设或已知动作名，不能是死映射。"""
    from avatar.emote_presets import LIVE2D_PRESETS

    actions = set(Live2DRenderer._ANIM_TO_MOTION_KW)
    dead = []
    for alias, target in Live2DRenderer._AI_DO_ALIASES.items():
        if target not in LIVE2D_PRESETS and target not in actions:
            dead.append((alias, target))
    assert not dead, f"别名指向了不存在的目标: {dead}"


def test_prompt_lists_far_fewer_options_than_full_preset_table():
    """prompt 选项数必须远小于预设总数。"""
    from avatar.emote_presets import LIVE2D_PRESETS

    listed = Live2DRenderer._AI_DO_PROMPT.count("/") + 1
    assert listed <= 25, f"prompt 列了 {listed} 个选项，太多了"
    assert listed < len(LIVE2D_PRESETS) / 2, (
        "prompt 选项应远少于预设总数——否则又是一张长名字表"
    )


# ══════════════════════════════════════════════════════════════
#  别名解析
# ══════════════════════════════════════════════════════════════

@pytest.mark.parametrize("word,target", [
    ("害羞", "blush_shy"),
    ("微笑", "smile_soft"),
    ("惊讶", "surprise_gasp"),
    ("思考", "think_look"),
    ("点头", "nod"),
    ("摇头", "head_shake"),
    ("叹气", "sigh"),
    ("眨眼", "wink"),
])
def test_alias_maps_to_preset(word, target):
    """中文语义词应归一到预设并播出去。"""
    r = _renderer()

    assert r._trigger_gesture(word, 0.6) is True
    assert r._preset_calls == [target], f"{word} 应归一到 {target}"


def test_alias_maps_to_motion():
    """肢体类别名走 motion 路径。"""
    r = _renderer(motions=["motions/waving.motion3.json"])
    r._model = types.SimpleNamespace(
        StartMotion=lambda *a: None, StopAllMotions=lambda: None,
        ResetExpressions=lambda: None,
    )
    from avatar.motion_mixer import MotionMixer

    r._mixer = MotionMixer()
    r._param_intent = {}

    assert r._trigger_gesture("挥手", 0.7) is True
    assert r._preset_calls == [], "肢体类不该走预设"


def test_unknown_word_is_safe():
    r = _renderer()
    assert r._trigger_gesture("完全不存在的词", 0.5) is False
    assert r._preset_calls == []


def test_unmatched_gesture_in_request_is_visible(caplog):
    """motion 未找到但 params 生效时，必须留痕。

    2026-09-11 实测：模型常自造 gesture（如 concern）。原实现因为
    `if req.params: played = True` 把整条请求当成功，日志只说
    “已提交 MotionRequest”，而动作部分实际没播——又一例静默部分失败。
    """
    import logging

    from avatar.motion_mixer import Layer, MotionMixer, MotionRequest

    r = _renderer()
    r._mixer = MotionMixer()
    r._param_intent = {}

    with caplog.at_level(logging.INFO, logger="avatar.live2d_renderer"):
        r.submit_motion_request(MotionRequest(
            layer=Layer.DIALOG, motion_group="concern",
            params={"ParamAngleX": 6}, duration=3.0, name="t",
        ))

    msgs = " ".join(rec.getMessage() for rec in caplog.records)
    assert "concern" in msgs, "未匹配的 gesture 名应出现在日志里"
    assert "未匹配到 motion" in msgs


def test_matched_gesture_does_not_log_warning(caplog):
    """能找到 motion 时不应误报。"""
    import logging

    from avatar.motion_mixer import Layer, MotionMixer, MotionRequest

    r = _renderer(motions=["motions/waving.motion3.json"])
    r._mixer = MotionMixer()
    r._param_intent = {}
    r._model = types.SimpleNamespace(
        StartMotion=lambda *a: None, StopAllMotions=lambda: None,
        ResetExpressions=lambda: None,
    )

    with caplog.at_level(logging.INFO, logger="avatar.live2d_renderer"):
        r.submit_motion_request(MotionRequest(
            layer=Layer.DIALOG, motion_group="waving", duration=3.0, name="t",
        ))

    msgs = " ".join(rec.getMessage() for rec in caplog.records)
    assert "未匹配到 motion" not in msgs, "找得到时不得误报"


def test_direct_preset_name_still_works():
    """别名是**新增**的一层，不是替换——直接给预设名仍应可用。"""
    r = _renderer()

    assert r._trigger_gesture("blush_shy", 0.6) is True
    assert r._preset_calls == ["blush_shy"]


# ══════════════════════════════════════════════════════════════
#  prompt 端
# ══════════════════════════════════════════════════════════════

class _Stub:
    _AI_DO_PROMPT = Live2DRenderer._AI_DO_PROMPT


def test_prompt_mentions_do_tag_and_examples():
    from core.harness_adapter import HanakoPetAdapter

    a = HanakoPetAdapter.__new__(HanakoPetAdapter)
    a._renderer = _Stub()

    p = a._build_action_prompt()

    assert "[do:" in p
    assert "害羞" in p
    assert "[feel:" in p, "示例里应同时出现 [feel:]，暗示两者搭配"
    assert "就不加" in p, "应允许模型不加——否则会硬凑"
    assert "只能从这些里选" in p, "应给出约束，否则模型会自造名字"


def test_prompt_is_short():
    from core.harness_adapter import HanakoPetAdapter

    a = HanakoPetAdapter.__new__(HanakoPetAdapter)
    a._renderer = _Stub()

    p = a._build_action_prompt()

    assert len(p) < 300, f"prompt 太长（{len(p)} 字符）——这正是 [do:] 从未被使用的原因"


def test_prompt_does_not_dump_raw_preset_names():
    """源码级守卫：prompt 不得再把预设名整表 join 进去。"""
    import pathlib

    src = (pathlib.Path(__file__).resolve().parent.parent
           / "core" / "harness_adapter.py").read_text(encoding="utf-8")

    assert "available_presets" not in src or "_AI_DO_PROMPT" in src, (
        "不得再把 available_presets 整表塞进 prompt"
    )
