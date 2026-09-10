# -*- coding: utf-8 -*-
"""BugFix #1 回归测试：对话回复气泡/TTS 缺失（"你好"无回复）全链路。

2026-08-24 18:58（同日 16:37 亦复现）用户发「你好」后桌宠气泡停在"思考中..."：
- 日志有 `处理消息 [miku]: 你好` + `API OK`，但**没有** `LLM 回复` / `Showing bubble` / TTS。
- 根因不在 fallback 链（日志无 `Hanako 不可用`/`Fallback -> chat_direct`），而是
  `chat_via_hanako → send_and_wait` 的 Hanako WS turn 事件未匹配到桌宠 turn，
  引擎线程被冻结满 reply_timeout(180s)，on_reply 永不触发。
- 18:58:09 的 `API OK` 是主线程 `_record_conversation_facts` 并行触发的
  memory_extract 直连 LLM（red herring），不是用户消息路径。

本测试覆盖：
1. 对话打断 → 处理新消息 → LLM OK → on_reply（气泡）必须触发（端到端 mock 全链路）
2. 过期消息（代际被新消息推进）必须被跳过，不调用 LLM、不触发 on_reply
3. prefer_hanako 下 Hanako 不可用（send 前）→ fallback chat_direct → 正常回复
"""
from __future__ import annotations

import collections
import os
import sys
import threading
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.conversation_engine import ConversationEngine
from core.harness_adapter import HanakoPetAdapter


class _FakeExecutor:
    """替代 ThreadPoolExecutor：只记录 submit，不真跑线程。"""

    def __init__(self):
        self.submitted: list = []

    def submit(self, fn, *args, **kwargs):
        self.submitted.append((fn, args, kwargs))
        return None

    def shutdown(self, wait=True):
        pass


class _FakeAdapter:
    """最小 adapter：chat 返回固定回复，记录调用参数。

    与真实 HanakoPetAdapter 一致：回复文本中的 [emotion:xxx] 标签由 adapter
    解析剥离后返回 (cleaned_text, emotion)。
    """

    def __init__(self, reply: str = "你好呀～[emotion:happy]", emotion: str = "happy"):
        self._reply = reply
        self._emotion = emotion
        self.calls: list[dict] = []
        self._current_session = None

    def chat(self, message, inject_memory=True, extra_context="", tools=None, source="user"):
        self.calls.append({
            "message": message,
            "inject_memory": inject_memory,
            "extra_context": extra_context,
            "tools": tools,
            "source": source,
        })
        cleaned, parsed = HanakoPetAdapter.parse_emotion(self._reply)
        return cleaned or "…", parsed or self._emotion


def _make_engine(adapter=None):
    """构造最小可用 ConversationEngine（跳过 __init__ 副作用），直接驱动 _process_message。"""
    engine = object.__new__(ConversationEngine)
    engine._lock = threading.Lock()
    engine._generation = 0
    engine._queue = collections.deque(maxlen=200)
    engine._interrupt_event = threading.Event()
    engine._session_manager = None
    engine._adapter = adapter or _FakeAdapter()
    engine._perception = type("P", (), {"build_context": lambda self, *a, **kw: "感知上下文"})()
    engine._unified_router = type("R", (), {"route": lambda self, *a, **k: None})()
    engine._capability_router = None
    engine._tool_registry = None
    engine._tools = []
    engine._tts_executor = _FakeExecutor()
    engine._synth_and_reply = lambda *a, **k: None
    engine.replies: list = []
    
    # P0: Poke 缓存 + character_id（跳过 __init__ 副作用，需手动初始化）
    engine._poke_cache: list[dict] = []
    engine._poke_timer = None
    engine._character_id = "miku"

    def _on_reply(reply, emotion, anim, audio_path):
        engine.replies.append((reply, emotion, anim, audio_path))

    engine.on_reply = _on_reply
    engine._original_on_reply = _on_reply
    return engine


# ── 1. 端到端全链路：对话打断 → 处理新消息 → LLM OK → 气泡回调 ─────────────


def test_interrupt_then_new_message_llm_ok_triggers_on_reply():
    """复现 18:58 场景的"应有"行为：打断旧消息 → 新消息 LLM OK → on_reply 必须触发。"""
    engine = _make_engine()
    # 用户发新消息：先打断（gen 0→1），再发送（gen 1→2，消息带 gen=2）
    engine.interrupt(reason="new_message")
    engine.send("你好", character="miku")

    msg = engine._queue.popleft()
    assert msg["gen"] == 2, "用户消息应携带打断后的最新代际"
    assert not engine._is_stale(msg["gen"]), "新消息代际不应过期"

    engine._process_message(msg)

    # LLM 被调用，且消息带来源 Tag（P0: 消息来源标记）
    assert len(engine._adapter.calls) == 1
    assert engine._adapter.calls[0]["message"] == "[消息来源(user)] 你好"
    assert engine._adapter.calls[0]["source"] == "user"
    # 气泡回调（文字先行，audio_path=""；TTS 由线程池后续回调）
    assert len(engine.replies) == 1, "LLM OK 后必须触发 on_reply（气泡）"
    reply, emotion, anim, audio = engine.replies[0]
    assert reply == "你好呀～"
    assert emotion == "happy"
    assert audio == ""
    # TTS 合成已提交（不阻塞主链路）
    assert len(engine._tts_executor.submitted) == 1


def test_reply_preserved_when_no_interrupt_during_llm():
    """LLM 期间没有新打断 → 回复必须正常上气泡（不被 _is_stale 误杀）。"""
    engine = _make_engine()
    engine.send("早上好", character="miku")
    msg = engine._queue.popleft()
    engine._process_message(msg)
    assert len(engine.replies) == 1
    assert engine.replies[0][0] == "你好呀～"


# ── 2. 过期消息（代际被推进）必须跳过 ─────────────────────────────────────


def test_stale_message_skipped_no_llm_no_reply():
    """旧消息处理前用户又发了新消息 → 旧消息跳过，不调 LLM、不上气泡。"""
    engine = _make_engine()
    engine.interrupt(reason="new_message")          # gen 0→1
    engine.send("第一条", character="miku")          # gen 1→2
    old_msg = engine._queue.popleft()

    engine.interrupt(reason="new_message")          # gen 2→3（用户又发消息）
    engine.send("第二条", character="miku")          # gen 3→4

    assert engine._is_stale(old_msg["gen"])          # 旧代际 2 < 当前 4
    engine._process_message(old_msg)

    assert engine._adapter.calls == [], "过期消息不应调用 LLM"
    assert engine.replies == [], "过期消息不应触发 on_reply"


# ── 3. prefer_hanako fallback：Hanako 不可用（send 前）→ chat_direct ──────


def _make_adapter_for_fallback():
    adapter = object.__new__(HanakoPetAdapter)
    adapter._base_url = "https://llm.invalid/v1"
    adapter._api_key = "test-key"
    adapter._model = "test-model"
    adapter._system_prompt = "你是测试助手"
    adapter._memory_budget = 800
    adapter._history = collections.deque(maxlen=40)
    adapter.transport_mode = "prefer_hanako"
    adapter._session_manager = None  # → chat_via_hanako 抛 HanakoUnavailableBeforeSend
    adapter._current_session = None
    adapter._agent_sessions = {}
    adapter._agent_pinned = {}
    adapter._pinned_session_id = None
    adapter._reply_timeout = 30.0
    adapter._context = type(
        "Ctx", (), {"build_memory_context": lambda self, max_chars=0: ""})()
    adapter._call_api = lambda messages, tools=None: "晚上好呀，月曦夜。[emotion:happy]"
    return adapter


def test_prefer_hanako_falls_back_to_direct_when_unavailable_before_send():
    """Hanako send 前不可用 → prefer_hanako 必须 fallback chat_direct，回复可用。"""
    adapter = _make_adapter_for_fallback()
    reply, emotion = adapter.chat("你好", source="user")
    assert reply == "晚上好呀，月曦夜。"
    assert emotion == "happy"
    # user 来源 fallback 后仍写本地历史（上下文延续）
    assert len(adapter._history) == 2
    assert adapter._history[0]["role"] == "user"
    assert adapter._history[1]["role"] == "assistant"


def test_hanako_only_does_not_fallback():
    """hanako_only 模式：Hanako 不可用（send 前）→ 不 fallback，抛异常。"""
    from core.hanako_ws_client import HanakoUnavailableBeforeSend
    adapter = _make_adapter_for_fallback()
    adapter.transport_mode = "hanako_only"
    try:
        adapter.chat("你好", source="user")
    except HanakoUnavailableBeforeSend:
        pass
    else:
        raise AssertionError("hanako_only 模式不应 fallback，应抛 HanakoUnavailableBeforeSend")
