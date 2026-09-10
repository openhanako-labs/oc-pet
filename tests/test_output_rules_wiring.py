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

def test_output_rules_contain_primary_tag():
    """主标签是 [feel:v,a]（连续 VA）。

    2026-09-10 收敛：原四个标签（emotion/action/expression/duration）职责重叠且
    要求「必须同时出现，缺一不可」，实测命中率 0/14。改为一个必给 + 一个可选。
    """
    rules = _adapter()._output_rules()
    assert "[feel:" in rules, "必须给出情绪坐标"
    assert "valence" in rules and "arousal" in rules, "两个维度都要解释"
    assert "必须" in rules


def test_output_rules_mention_optional_do_tag():
    """[do:] 是可选的（需 renderer 提供动作列表时才出现）。"""
    rules = _adapter()._output_rules()
    # 本测试的替身未注入 renderer → 无动作列表 → 不出现 [do:]
    assert "[do:" not in rules or "可选" in rules


def test_legacy_tags_no_longer_demanded():
    """旧标签不再作为「必须」要求（仍能被解析，但不让模型写）。

    这是本次收敛的核心：模型面对「四个标签该写哪个」选择了全不写。
    """
    rules = _adapter()._output_rules()
    assert "四个标签必须同时" not in rules
    assert "缺一不可" not in rules


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
    """核心断言：用户消息经 Hanako 通道发送时，text 必须带表达契约。"""
    a = _adapter()
    a.chat_via_hanako("举个手?")

    sent = a._session_manager.sent_texts[0]
    assert "[pet-output-rules]" in sent
    assert "[feel:" in sent, "主标签必须在通道里"
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
    """memory_extract 经 Hanako 通道时不得夹带表达契约。"""
    a = _adapter(reply='{"facts": []}')
    a.chat_via_hanako("抽取事实", source="memory_extract")

    sent = a._session_manager.sent_texts[0]
    assert "[pet-output-rules]" not in sent
    assert "[feel:" not in sent


# ══════════════════════════════════════════════════════════════
#  端到端：模型真给标签时，链路能解析出来
# ══════════════════════════════════════════════════════════════

def test_reply_with_tags_survives_parsing():
    """模型给出标签时，emotion 应被解析出来（而不是落到 neutral）。"""
    a = _adapter(
        reply="好呀！[emotion:happy][feel:0.8,0.7]"
              "[action:{\"gesture\":\"waving\",\"intensity\":0.8}]"
    )
    cleaned, emotion = a.chat_via_hanako("举个手?")

    assert emotion == "happy", "带 emotion 标签的回复必须解析出真情绪"
    assert "[emotion:" not in cleaned, "标签须从展示文本中剥离"
    assert "[feel:" not in cleaned, "[feel:] 也不得漏给用户"
