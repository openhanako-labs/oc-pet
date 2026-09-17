# -*- coding: utf-8 -*-
"""回归：方括号协议标签被元信息正则吃剩的残渣不得上屏。

现象（2026-09-16 12:37:08 / 12:43:32 实测，日志两处）：

    pet_mixins.bubble_mixin: Showing bubble: [ 。 [emotion=neutral]

同一会话的模型原始输出是：

    [mood:被冷落] 你倒是说点什么啊——总不会打算让我对着空对话框坐到天黑吧。

根因：clean_bubble_text（core/hanako_monitor.py）里的元信息剥离正则

    \\b(?:MOOD|mood|thinking|tool|status)[:：]?[^\\n。！？!?]*

从 "mood:" 一路吃到句末标点，把整句话连同方括号前缀一起吞掉，
只留下开头的 "[" 和末尾的 "。"。

修复（2026-09-17）：
  1. 在元信息剥离**之前**先消费整对方括号协议标签
     （mood / feel / do / emotion / expression / action / duration / message）
  2. 清掉残留的孤立方括号
  3. 清洗后只剩标点/括号 → 返回空串（不上气泡）

配套：pet.py 的 `compact_bubble_text(reply) or reply` 已改掉——
清洗为空时回退原始文本会把标签原样上屏。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 只有标点/括号的残渣集合（这些绝不能作为气泡正文）
_RESIDUE = ("[ 。", "[", "。", "]", "[ ]", "[]", "，", "…", "...", "（", "）")


def _bubble(reply: str) -> str:
    """走生产解析链，返回最终会显示的气泡文本。"""
    from core.harness_adapter import HanakoPetAdapter
    from core.conversation_engine import ConversationEngine
    from core.hanako_monitor import clean_bubble_text, compact_bubble_text

    engine = ConversationEngine.__new__(ConversationEngine)
    adapter = HanakoPetAdapter.__new__(HanakoPetAdapter)

    text, _intent = engine.parse_action_intent(reply)
    text, _emotion = adapter.parse_emotion(text)
    bubble = compact_bubble_text(text)
    if not bubble:
        bubble = text if clean_bubble_text(text) else ""
    return bubble


# ── 一、复现原 bug：mood 标签不得留残渣 ─────────────────────────────────────


def test_mood_tag_does_not_leave_bracket_residue():
    """原样复现 2026-09-16 那条把气泡变成 '[ 。' 的回复。"""
    reply = "[mood:被冷落] 你倒是说点什么啊——总不会打算让我对着空对话框坐到天黑吧。"
    bubble = _bubble(reply)
    assert bubble not in _RESIDUE, f"气泡仍是残渣: {bubble!r}"
    assert "你倒是说点什么啊" in bubble, f"正文被吃掉了: {bubble!r}"
    assert "mood" not in bubble, f"标签泄漏到气泡: {bubble!r}"


def test_mood_tag_after_emotion_tag_keeps_text():
    reply = "[emotion:neutral]\n\n[mood:被冷落] 你倒是说点什么啊"
    bubble = _bubble(reply)
    assert bubble not in _RESIDUE, f"气泡仍是残渣: {bubble!r}"
    assert "你倒是说点什么啊" in bubble


def test_mood_tag_after_feel_tag_keeps_text():
    """[feel:] + [mood:] 同时出现时，正文与 VA 都要保住。"""
    from core.harness_adapter import HanakoPetAdapter
    from core.conversation_engine import ConversationEngine

    engine = ConversationEngine.__new__(ConversationEngine)
    adapter = HanakoPetAdapter.__new__(HanakoPetAdapter)
    text, intent = engine.parse_action_intent("[feel:0.3,0.2]\n\n[mood:关心] 起来走两步吧")
    text, _ = adapter.parse_emotion(text)
    assert intent and intent.get("va") == [0.3, 0.2], "VA 应被解析出来"
    assert "起来走两步吧" in text, f"正文被标签处理吃掉: {text!r}"


# ── 二、清洗后只剩标点 → 必须判空（不回退原始文本） ──────────────────────────


def test_punctuation_only_residue_is_empty():
    from core.hanako_monitor import clean_bubble_text

    for junk in ("[ 。", "[", "。", "]", "（", "…", "[]", "[ ]"):
        assert clean_bubble_text(junk) == "", f"{junk!r} 应判空，实得 {clean_bubble_text(junk)!r}"


def test_tag_only_reply_yields_empty_bubble():
    """模型只输出协议标签、没有正文 → 气泡为空，而不是显示标签本身。"""
    assert _bubble("[feel:0.3,0.2] [emotion:neutral]") == ""
    assert _bubble("[emotion:neutral]") == ""


# ── 三、正常回复不得被误伤 ─────────────────────────────────────────────────


def test_normal_reply_survives_untouched():
    from core.hanako_monitor import clean_bubble_text

    for ok in (
        "慢慢来，跑通一个就是一个。",
        "嗯，你好。我在这。",
        "这个假设没验证过，后面推的都要重来。",
        "在，怎么了？",
    ):
        assert clean_bubble_text(ok) == ok, f"正常回复被改写: {ok!r} -> {clean_bubble_text(ok)!r}"


def test_mood_word_inside_sentence_not_overstripped():
    """正文里出现 mood/thinking 这类词时，不应把整句吃掉。

    2026-09-17：元信息正则的冒号从可选改为必需。原 `[:：]?` 会让
    "你 mood 看起来不错。" 被吃到句末标点，只剩 "你 。"。
    """
    from core.hanako_monitor import clean_bubble_text

    for ok in ("你 mood 看起来不错。", "我在 thinking 这件事", "tool 用完了"):
        cleaned = clean_bubble_text(ok)
        assert cleaned, f"整句被吃掉了: {ok!r} -> {cleaned!r}"
        assert len(cleaned) >= len(ok) - 2, f"正文被吃掉: {ok!r} -> {cleaned!r}"


def test_tag_word_with_colon_still_stripped():
    """带冒号的形式仍按元信息剥掉（不能因为修误伤而放开）。"""
    from core.hanako_monitor import clean_bubble_text

    assert clean_bubble_text("thinking: 我在想事情") == ""
    assert clean_bubble_text("status: ok") == ""
    assert clean_bubble_text("mood: 被冷落") == ""


def test_parens_in_normal_text_not_stripped():
    """括号开头的正常文本不得被当残渣清掉。"""
    from core.hanako_monitor import clean_bubble_text

    for ok in ("（笑）今天天气不错。", "(笑) 今天天气不错。", "嗯（想了想）还是算了。"):
        assert clean_bubble_text(ok) == ok, f"正常括号文本被误伤: {ok!r}"


def test_structured_vibe_block_still_stripped():
    """已有能力不能回退：结构化 Vibe 块仍要整段剥掉。

    方括号形式由 clean_bubble_text 自己处理（文件回退路径直接调它，
    不经过 parse_emotion）；纯文本行形式由前缀剥离处理。
    """
    from core.hanako_monitor import clean_bubble_text

    assert clean_bubble_text("[Vibe: 你好]") == ""
    assert clean_bubble_text("Vibe: 你好") == ""
    assert clean_bubble_text("[ Sparks: 想聊天 ]") == ""
