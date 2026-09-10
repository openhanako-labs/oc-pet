"""接线测试：输出规则必须送达 Hanako 通道

覆盖 2026-09-10 接线。背景（实测数据）：

    `[输出规则]`（教 LLM 输出 [emotion:]/[action:]/[expression:]/[duration:]）
    原本只内联在 chat_direct / _chat_stream_direct —— 而实际运行走
    chat_via_hanako（transport_mode=prefer_hanako），标签契约从未送达。

    后果实测：14 次回复，0 次带 [emotion:]，14 次全部走兜底静态表。
    用户说「举个手」，桌宠只微笑不举手（动作退化成 emotion=neutral → idle）。

本测试锁住：Hanako 通道必须携带输出规则；机器读的来源不得携带。
"""
from __future__ import annotations

import types

import pytest

from core.harness_adapter import HanakoPetAdapter


# ── 被测对象的构造 ────────────────────────────────────────────

class _FakeSM:
    """记录 send_and_wait 收到的 text。"""

    def __init__(self, reply="好的~[emotion:happy]"):
        self.sent_texts = []
        self._reply = reply

    def send_and_wait(self, session, text, timeout=None, display_text=None, ui_context=None):
        self.sent_texts.append(text)
        return types.SimpleNamespace(
            text=self._reply, error=None, aborted=False, tool_calls=(),
        )


def _adapter(reply="好的~[emotion:happy]"):
    a = HanakoPetAdapter.__new__(HanakoPetAdapter)
    a.agent_id = "ophelia"
    a._session_manager = _FakeSM(reply)
    a._current_session = object()          # 非 None → 跳过 session 创建
    a._reply_timeout = 30.0
    a._agent_pinned = {}
    a._agent_sessions = {}
    a._pinned_session_id = None
    a._renderer = None
    a._pet_renderer = None
    return a


# ══════════════════════════════════════════════════════════════
#  规则文本本身
# ══════════════════════════════════════════════════════════════

def test_output_rules_contain_all_four_tags():
    rules = _adapter()._output_rules()
    for tag in ("[emotion:", "[action:", "[expression:", "[duration:"):
        assert tag in rules, f"输出规则缺少 {tag}"
    assert "必须" in rules


def test_rules_are_single_source_of_truth():
    """两个直连路径与 Hanako 通道共用同一份文本（不再是三处内联）。"""
    import pathlib
    src = pathlib.Path(__file__).resolve().parent.parent / "core" / "harness_adapter.py"
    body = src.read_text(encoding="utf-8")

    assert body.count('1. 回复简短自然') == 1, "规则文本应只出现一次（唯一来源）"
    assert body.count("self._output_rules()") == 3, "三处消费点都应调用 _output_rules()"


# ══════════════════════════════════════════════════════════════
#  来源分流
# ══════════════════════════════════════════════════════════════

@pytest.mark.parametrize("source", ["memory_extract", "memory_reflect", "screen_enrich"])
def test_machine_sources_get_no_rules(source):
    """这些来源的输出是给机器读的，注入标签会污染。"""
    assert _adapter()._needs_output_rules(source) is False


@pytest.mark.parametrize("source", ["user", "proactive", "idle", ""])
def test_display_sources_get_rules(source):
    assert _adapter()._needs_output_rules(source) is True


# ══════════════════════════════════════════════════════════════
#  端到端：Hanako 通道真的把规则发出去了
# ══════════════════════════════════════════════════════════════

def test_hanako_path_carries_output_rules():
    """核心断言：用户消息经 Hanako 通道发送时，text 必须带标签契约。"""
    a = _adapter()
    a.chat_via_hanako("举个手?")

    sent = a._session_manager.sent_texts[0]
    assert "[pet-output-rules]" in sent
    assert "[emotion:xxx]" in sent
    assert "[duration:3]" in sent
    assert "举个手?" in sent, "原消息必须保留"
    assert sent.rstrip().endswith("举个手?"), "规则应在消息之前"


def test_hanako_path_keeps_pet_context():
    """原有的 [pet-context] 注入不能被接线改坏。"""
    a = _adapter()
    a.chat_via_hanako("在吗", extra_context="[窗口] VSCode")

    sent = a._session_manager.sent_texts[0]
    assert "[pet-context]" in sent
    assert "VSCode" in sent
    assert "[pet-output-rules]" in sent
    assert sent.rstrip().endswith("在吗")


def test_machine_source_via_hanako_has_no_rules():
    """memory_extract 经 Hanako 通道时不得夹带标签契约。"""
    a = _adapter(reply='{"facts": []}')
    a.chat_via_hanako("抽取事实", source="memory_extract")

    sent = a._session_manager.sent_texts[0]
    assert "[pet-output-rules]" not in sent
    assert "[emotion:xxx]" not in sent


# ══════════════════════════════════════════════════════════════
#  端到端：模型真给标签时，链路能解析出来
# ══════════════════════════════════════════════════════════════

def test_reply_with_tags_survives_parsing():
    """模型给出完整四标签时，emotion 应被解析出来（而不是落到 neutral）。"""
    a = _adapter(
        reply="好呀！[emotion:happy][action:{\"gesture\":\"waving\",\"intensity\":0.8}]"
              "[expression:smile=90][duration:5]"
    )
    cleaned, emotion = a.chat_via_hanako("举个手?")

    assert emotion == "happy", "带标签的回复必须解析出真情绪"
    assert "[emotion:" not in cleaned, "标签须从展示文本中剥离"
