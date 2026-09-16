"""情绪 → TTS prosody 映射测试。

核心约束（借自 Amadeus 的调参经验）：

    **语速绝对不能变。只允许 pitch / volume 改变语气语调。**

改语速会让听感从"她心情变了"变成"换了一个人说话"——情绪调节
反而破坏角色一致性。这里把这条规矩钉死。
"""
from __future__ import annotations

import inspect

import pytest

from tts_provider import emotion_prosody as ep


# ── 硬约束：语速恒定 ─────────────────────────────────


def test_rate_is_always_zero_for_every_emotion():
    """★ 核心：任何情绪下 rate 都必须是 +0%。"""
    for emo in list(ep._EMOTION_PROSODY) + ["", "unknown", "joy", "furious"]:
        assert ep.rate_for(emo) == "+0%", f"{emo} 不该改语速"


def test_emotion_prosody_table_has_no_rate():
    """表里只有 (pitch, volume) 两项——多一项就意味着有人偷偷加了 rate。"""
    for emo, val in ep._EMOTION_PROSODY.items():
        assert len(val) == 2, f"{emo} 的 prosody 元组应只有 pitch/volume"


def test_edge_provider_never_changes_rate_by_emotion():
    """★ 接线检查：provider 里不得再出现按 emotion 改 rate 的逻辑。"""
    from tts_provider import edge_tts
    src = inspect.getsource(edge_tts.EdgeTtsProvider.synthesize)
    assert "emotion_tts_map" not in src, "旧的改语速映射必须已移除"
    assert "eff_rate = self._rate" in src, "rate 应恒等于配置基线"
    assert "prosody_for" in src, "应改用 emotion_prosody"


# ── 映射正确性 ───────────────────────────────────────


@pytest.mark.parametrize("emo,expect", [
    ("neutral",   ("+0Hz",  "+0%")),
    ("happy",     ("+8Hz",  "+6%")),
    ("sad",       ("-12Hz", "-8%")),
    ("angry",     ("-2Hz",  "+14%")),
    ("surprised", ("+18Hz", "+10%")),
])
def test_core_emotions_map_as_expected(emo, expect):
    assert ep.prosody_for(emo) == expect


def test_happy_raises_pitch_sad_lowers_it():
    """情绪方向要符合直觉：开心上扬、难以下沉。"""
    h_pitch = ep.prosody_for("happy")[0]
    s_pitch = ep.prosody_for("sad")[0]
    assert h_pitch.startswith("+")
    assert s_pitch.startswith("-")


def test_angry_is_louder_than_sad():
    """生气更响，难过更轻。"""
    def vol(emo):
        return int(ep.prosody_for(emo)[1].rstrip("%"))
    assert vol("angry") > 0 > vol("sad")


def test_all_emotions_produce_valid_prosody():
    """表里每个值都必须是 Edge TTS 能接受的格式。"""
    for emo in ep._EMOTION_PROSODY:
        pitch, volume = ep.prosody_for(emo)
        assert ep.is_valid_prosody(pitch), f"{emo} pitch 非法: {pitch}"
        assert ep.is_valid_prosody(volume), f"{emo} volume 非法: {volume}"


# ── 归一化 ───────────────────────────────────────────


@pytest.mark.parametrize("raw,expect", [
    ("HAPPY", "happy"),
    ("  Sad  ", "sad"),
    ("furious", "angry"),
    ("joyful", "happy"),
    ("shocked", "surprised"),
    ("", "neutral"),
    (None, "neutral"),
    ("nonexistent_emotion", "neutral"),
])
def test_normalize_emotion(raw, expect):
    assert ep.normalize_emotion(raw) == expect


def test_unknown_emotion_falls_back_to_neutral():
    """未知情绪不得抛错——回落 neutral，保证出声不受影响。"""
    assert ep.prosody_for("totally_made_up") == ep.prosody_for("neutral")


def test_no_alias_shadowed_by_direct_key():
    """★ 别名不得与直接键重名——重名条目永远取不到，是死代码。

    （初版把 joy/soft/annoyed/tired 同时放进两处，实测发现 joy 走的是
    直接键而非别名，两处值还不一样。）
    """
    overlap = set(ep._ALIASES) & set(ep._EMOTION_PROSODY)
    assert not overlap, f"这些别名被直接键遮蔽: {sorted(overlap)}"


def test_aliases_point_to_known_emotions():
    """别名的目标必须真的在表里，否则归一后仍回落 neutral。"""
    for alias, target in ep._ALIASES.items():
        assert target in ep._EMOTION_PROSODY, f"别名 {alias} → {target} 不存在"


def test_renderer_emotions_all_covered():
    """渲染器认识的七个情绪都必须有映射（否则表情动了声音没动）。"""
    for emo in ("neutral", "happy", "cute", "surprised", "thinking", "sad", "angry"):
        assert emo in ep._EMOTION_PROSODY, f"渲染器情绪 {emo} 缺 prosody"


# ── prosody 加法 ─────────────────────────────────────


def test_add_prosody_same_unit():
    assert ep.add_prosody("+5Hz", "+3Hz") == "+8Hz"
    assert ep.add_prosody("-10%", "+4%") == "-6%"


def test_add_prosody_negative_result():
    assert ep.add_prosody("+2Hz", "-10Hz") == "-8Hz"


def test_add_prosody_unit_mismatch_returns_delta():
    """单位不一致时返回 delta——宁可只取情绪增量，也不拼非法字符串。"""
    assert ep.add_prosody("+5Hz", "+3%") == "+3%"
    assert ep.add_prosody("+3%", "+5Hz") == "+5Hz"


def test_add_prosody_unparseable_returns_delta():
    assert ep.add_prosody("", "+5Hz") == "+5Hz"
    assert ep.add_prosody("+5Hz", "") == "+5Hz"
    assert ep.add_prosody("fast", "+5Hz") == "+5Hz"


# ── is_valid_prosody ─────────────────────────────────


@pytest.mark.parametrize("val,ok", [
    ("+8Hz", True), ("-12%", True), ("+0%", True), ("+0Hz", True),
    ("", False), ("8", False), ("fast", False), ("+8", False), (None, False),
])
def test_is_valid_prosody(val, ok):
    assert ep.is_valid_prosody(val) is ok


# ── instruct 提示（Qwen / MiMo / CosyVoice 通用）─────


@pytest.mark.parametrize("emo", ["happy", "sad", "angry", "cute", "surprised", "thinking"])
def test_instruct_for_covers_renderer_emotions(emo):
    """渲染器的情绪都要有 instruct（原先 conversation_engine 的表只有 5 项，缺 surprised）。"""
    hint = ep.instruct_for(emo)
    assert hint, f"{emo} 缺 instruct"


def test_instruct_neutral_is_empty():
    """中性不该塞额外指令——引擎默认表现通常已足够。"""
    assert ep.instruct_for("neutral") == ""


def test_instruct_unknown_is_empty():
    assert ep.instruct_for("totally_made_up") == ""
    assert ep.instruct_for("") == ""


def test_instruct_never_mentions_speed():
    """★ 硬约束：instruct 里绝不能出现"快/慢"这类改语速的词。"""
    banned = ("快", "慢", "语速", "speed", "faster", "slower")
    for emo in ep._INSTRUCT:
        hint = ep.instruct_for(emo)
        for word in banned:
            assert word not in hint, f"{emo} 的 instruct 提到了语速词 {word!r}: {hint}"


def test_instruct_resolves_aliases():
    """别名应能落到 instruct（joy → happy 等）。"""
    assert ep.instruct_for("joy") == ep.instruct_for("happy")
    assert ep.instruct_for("furious") == ep.instruct_for("angry")
    assert ep.instruct_for("shy") == ep.instruct_for("cute")


def test_conversation_engine_uses_shared_instruct():
    """★ 接线检查：conversation_engine 不得再内联 instruct_map。"""
    import inspect
    from core.conversation_engine import ConversationEngine
    src = inspect.getsource(ConversationEngine)
    assert "instruct_map" not in src, "内联表必须已移除（两处重复，缺 surprised）"
    assert "instruct_for" in src, "应改用共享的 instruct_for"
