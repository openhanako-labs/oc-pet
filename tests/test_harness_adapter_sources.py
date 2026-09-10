# -*- coding: utf-8 -*-
"""harness_adapter 来源路由 + 历史写入 回归测试（QA 集成 bug 修复验证）

背景（QA 实测确认）：FactStore/ReflectionEngine 调用
``adapter.chat(prompt, source="memory_extract"/"memory_reflect")`` 时，旧版 chat()
只有 source in ("proactive","idle") 才走 chat_direct；其他 source 在
transport_mode="prefer_hanako" 下走 chat_via_hanako → 把抽取/反思 prompt 作为用户
消息发进 Hanako session（display_text 污染），且 chat_direct 会把 prompt+回复
append 进本地 ``_history``（后续对话按 ``list(self._history)[-10:]`` 注入 → 本地
上下文也被污染）。

修复：
1. chat() 分流把 ("memory_extract", "memory_reflect", "screen_enrich") 加入
   chat_direct 直连名单（与 proactive/idle 并列）——内部来源绝不进 Hanako session。
2. chat_direct 内部对非 user 来源不写 self._history（_records_history 守卫）。

本测试用 ``object.__new__`` 构造未初始化 adapter（避免读 .env/Hanako 配置），
stub _call_api 与 chat_via_hanako，仅验证路由与历史写入逻辑，不发真实网络。
"""
from __future__ import annotations

import collections
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.harness_adapter import HanakoPetAdapter

INTERNAL_SOURCES = ("proactive", "idle", "memory_extract", "memory_reflect", "screen_enrich")


def _make_adapter(transport_mode: str = "prefer_hanako"):
    """构造最小可用 HanakoPetAdapter（跳过 __init__ 副作用），stub LLM 通道。

    返回 (adapter, via_hanako_calls)：via_hanako_calls 记录 chat_via_hanako
    是否被调用（内部来源绝不应触发）。
    """
    adapter = object.__new__(HanakoPetAdapter)
    adapter._base_url = "https://llm.invalid/v1"
    adapter._api_key = "test-key"
    adapter._model = "test-model"
    adapter._system_prompt = "你是测试助手"
    adapter._memory_budget = 800
    adapter._history = collections.deque(maxlen=40)
    adapter.transport_mode = transport_mode
    adapter._session_manager = object()
    adapter._current_session = None
    adapter._agent_sessions = {}
    adapter._agent_pinned = {}
    adapter._pinned_session_id = None
    adapter._reply_timeout = 30.0
    adapter._context = type(
        "Ctx", (), {"build_memory_context": lambda self, max_chars=0: ""})()

    calls: list = []
    adapter.chat_via_hanako = lambda *a, **kw: calls.append((a, kw)) or ("VIA_HANAKO", "neutral")  # type: ignore[method-assign]
    adapter._calls = calls
    # stub LLM API：返回带情绪标签的固定回复，避免真实网络
    adapter._call_api = lambda messages, tools=None: "OK 回复 [emotion:happy]"  # type: ignore[method-assign]
    return adapter


# ── 1. 内部来源路由：prefer_hanako 下走 chat_direct、绝不触发 chat_via_hanako ──


def test_internal_sources_route_to_direct_not_hanako():
    """memory_extract/memory_reflect/screen_enrich（及 proactive/idle）在
    prefer_hanako 下必须走 chat_direct，不触发 chat_via_hanako。"""
    for src in INTERNAL_SOURCES:
        adapter = _make_adapter("prefer_hanako")
        reply, emotion = adapter.chat("内部指令测试", source=src)
        assert adapter._calls == [], f"{src} 不应触发 chat_via_hanako"
        assert reply == "OK 回复", f"{src} 应走 chat_direct 并拿到 stub 回复，实际 {reply!r}"
        assert emotion == "happy"


def test_internal_sources_direct_in_all_transport_modes():
    """内部来源在 hanako_only/prefer_hanako/direct 下都走 chat_direct。"""
    for mode in ("hanako_only", "prefer_hanako", "direct"):
        for src in ("memory_extract", "memory_reflect", "screen_enrich"):
            adapter = _make_adapter(mode)
            reply, _emotion = adapter.chat("内部指令测试", source=src)
            assert adapter._calls == [], f"{mode}/{src} 不应触发 chat_via_hanako"
            assert reply == "OK 回复"


# ── 2. 内部来源不写本地 _history（避免污染后续注入上下文）──────────────


def test_internal_sources_do_not_write_history():
    """内部来源调用后 _history 必须保持为空。"""
    for src in INTERNAL_SOURCES:
        adapter = _make_adapter("prefer_hanako")
        adapter.chat("内部指令测试", source=src)
        assert len(adapter._history) == 0, f"{src} 不应写入本地 _history"


def test_internal_sources_force_no_memory_injection():
    """内部来源路由强制 inject_memory=False：即使调用方误传 True 也不注入记忆。"""
    adapter = _make_adapter("prefer_hanako")
    adapter._context = type(
        "Ctx", (), {"build_memory_context": lambda self, max_chars=0: "记忆内容XYZ"})()
    captured: dict = {}
    adapter._call_api = lambda messages, tools=None: captured.update(messages=messages) or "OK"  # type: ignore[method-assign]
    adapter.chat("内部指令测试", source="memory_extract", inject_memory=True)
    contents = " ".join(str(m.get("content", "")) for m in captured["messages"])
    assert "记忆内容XYZ" not in contents, "memory_extract 不应注入记忆上下文"


# ── 3. user 来源保持原行为（向后兼容）──────────────────────────────────


def test_user_source_goes_via_hanako_in_prefer_hanako():
    """user 消息在 prefer_hanako 下仍走 Hanako session（路由不受影响）。"""
    adapter = _make_adapter("prefer_hanako")
    reply, _emotion = adapter.chat("你好", source="user")
    assert len(adapter._calls) == 1
    assert reply == "VIA_HANAKO"


def test_user_source_still_records_history_direct_mode():
    """user 消息（direct 模式）仍写入本地 _history（用户+助手两轮）。"""
    adapter = _make_adapter("direct")
    reply, emotion = adapter.chat("你好", source="user", inject_memory=False)
    assert reply == "OK 回复"
    assert emotion == "happy"
    assert len(adapter._history) == 2
    assert adapter._history[0]["role"] == "user"
    assert adapter._history[1]["role"] == "assistant"


def test_default_source_records_history():
    """缺省 source（None → user 语义）仍写入 _history。"""
    adapter = _make_adapter("direct")
    adapter.chat("你好", inject_memory=False)
    assert len(adapter._history) == 2


def test_user_source_injects_memory_when_requested():
    """user 消息 inject_memory=True 时仍注入记忆（对照组）。"""
    adapter = _make_adapter("direct")
    adapter._context = type(
        "Ctx", (), {"build_memory_context": lambda self, max_chars=0: "记忆内容XYZ"})()
    captured: dict = {}
    adapter._call_api = lambda messages, tools=None: captured.update(messages=messages) or "OK"  # type: ignore[method-assign]
    adapter.chat("你好", source="user")  # inject_memory 默认 True
    contents = " ".join(str(m.get("content", "")) for m in captured["messages"])
    assert "记忆内容XYZ" in contents


# ── 4. _records_history 判定 ─────────────────────────────────────────


def test_records_history_helper():
    assert HanakoPetAdapter._records_history("user") is True
    assert HanakoPetAdapter._records_history("") is True
    assert HanakoPetAdapter._records_history(None) is True
    for s in INTERNAL_SOURCES:
        assert HanakoPetAdapter._records_history(s) is False
