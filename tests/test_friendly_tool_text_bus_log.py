# -*- coding: utf-8 -*-
"""回归测试：工具结果友好化不应把内部总线/日志泄漏到气泡。

验证对象：conversation_engine._friendly_tool_text
  - 修复前：[bus] Queue migrated… 这类内部日志被当成普通文本截断后直接上气泡
    （日志实证：Showing bubble: [bus] Queue migrated to TrackRef format [bus]…）
  - 修复后：剥离 [bus]/[WS]/[httpx] 等内部标签，解析剩余 JSON；解析不出时按
    能力名兜底友好话，绝不泄漏内部日志/破损 JSON。

仅测纯函数逻辑（不触发重型 __init__）：用 __new__ 构造无状态实例调用方法。
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.capability_registry import RouteResult
from core.conversation_engine import ConversationEngine


def _engine():
    # 方法均不依赖 __init__ 状态，用 __new__ 避免重型初始化
    return ConversationEngine.__new__(ConversationEngine)


def _rr(capability: str, tool_result: str = "", text: str = "") -> RouteResult:
    return RouteResult(capability=capability, tool_result=tool_result, text=text)


def test_bus_log_with_json_shows_clean_summary():
    """[bus] 日志 + 尾随 JSON → 剥离 [bus] 并解析为干净摘要，不泄漏内部文本。"""
    raw = (
        "[bus] Queue migrated to TrackRef format\n"
        "[bus] Queue migrated to TrackRef format\n"
        '{"ok":true,"event":"next"}'
    )
    out = _engine()._friendly_tool_text(_rr("audio_bus", raw))
    assert "[bus]" not in out
    assert "{" not in out
    assert "执行成功" in out


def test_bus_log_only_no_json_uses_capability_fallback():
    """纯内部日志无 JSON → 能力兜底话术，不泄漏。"""
    raw = "[bus] Queue migrated to TrackRef format\n[bus] Queue migrated to TrackRef format"
    out = _engine()._friendly_tool_text(_rr("audio_bus", raw))
    assert "[bus]" not in out
    assert out == "好的，已为你处理音乐～"


def test_unknown_capability_bus_log_generic_fallback():
    """未知能力 + 内部噪声 → 通用兜底，不泄漏内部文本。"""
    raw = "[WS] connected\n[httpx] GET /x 200"
    out = _engine()._friendly_tool_text(_rr("some_plugin_tool", raw))
    assert "[WS]" not in out and "[httpx]" not in out
    assert out == "好的，已经帮你处理好啦～"


def test_valid_json_dict_ok_summarized():
    """合法 JSON 对象 → 走现有摘要逻辑，不泄漏原始 JSON。"""
    raw = '{"ok":true,"name":"晴天"}'
    out = _engine()._friendly_tool_text(_rr("play_music", raw))
    assert "晴天" in out
    assert "{" not in out


def test_valid_json_array_summarized():
    """合法 JSON 数组 → 共 N 项。"""
    raw = '[{"id":1},{"id":2},{"id":3}]'
    out = _engine()._friendly_tool_text(_rr("list_files", raw))
    assert "共 3 项" in out


def test_readable_plain_text_preserved():
    """非 JSON 的可读自然语言 → 保留（兼容正常返回），不强制兜底。"""
    raw = "已发送邮件给张三"
    out = _engine()._friendly_tool_text(_rr("send_mail", raw))
    assert out == "已发送邮件给张三"


def test_empty_tool_result_returns_text():
    """无 tool_result → 返回 text（日报/会话信息等内部能力文案）。"""
    out = _engine()._friendly_tool_text(_rr("daily_diary", text="今天的日报来啦～"))
    assert out == "今天的日报来啦～"


def test_sanitize_strips_internal_tag_lines():
    """_sanitize_tool_result 单独校验：丢弃纯标签行，保留标签后 JSON。"""
    eng = _engine()
    raw = "[bus] noise line\n[bus] more noise {\"ok\":true}"
    cleaned = eng._sanitize_tool_result(raw)
    assert "[bus]" not in cleaned
    assert cleaned == '{"ok":true}'
