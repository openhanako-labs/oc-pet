# -*- coding: utf-8 -*-
"""QA 独立回归（fresh eyes）BugFix #4 边界验证（commit 9d727c8）。

team-lead 指定边界：
  a) clean_bubble_text / parse_emotion 对「Vibe:」在句子中间（非行首）**不**误删
  b) <mood> 含多行 Vibe/Reflections/Will/Sparks 整块剥离后正文保留
  c) 心跳：chat 阻塞期间 push、返回后停止、不覆盖回复气泡（mock on_progress）
  d) UI：基础 tab 无下拉框后 M5「切换选中桌宠」保存 agents[].enabled 仍生效

额外补充（fresh eyes 自选边界）：
  e) <mood> 未闭合（缺 </mood>）时正文仍保留（正则非贪婪 + HTML 标签兜底）
  f) 全文本都是 mood 块 → 返回空串（气泡/TTS 路径安全，不抛异常）
  g) 快速 chat（<6s）不推送「还在想...」（避免对正常回复产生噪声气泡）

只写测试；若失败即真实 bug，不改源码。
"""
from __future__ import annotations

import os
import sys
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── a/b/e/f) mood 过滤边界：clean_bubble_text + parse_emotion 一致性 ────────


def _both_clean(text: str) -> tuple:
    """同一文本分别过 clean_bubble_text 与 parse_emotion，返回两个清理结果。"""
    from core.hanako_monitor import clean_bubble_text
    from core.harness_adapter import HanakoPetAdapter

    return clean_bubble_text(text), HanakoPetAdapter.parse_emotion(text)[0]


def test_mid_sentence_vibe_not_stripped():
    """a) 「Vibe:」出现在句子中间（非行首）→ 两处过滤都不得误删正常回复。

    用户/LLM 正常回复里可能恰好出现 "Vibe: 感觉很好" 这种说法；
    前缀剥离正则用 ^（MULTILINE 行首锚点）限定，只有行首 Vibe: 才剥。
    """
    text = "今天心情不错，Vibe: 感觉很好，想跟你聊聊天"
    cleaned, parsed = _both_clean(text)
    assert "Vibe: 感觉很好" in cleaned, f"行中 Vibe 不应被删: {cleaned!r}"
    assert "Vibe: 感觉很好" in parsed, f"行中 Vibe 不应被删: {parsed!r}"
    assert "今天心情不错" in cleaned


def test_mood_block_multiline_stripped_body_preserved():
    """b) <mood> 多行整块（Vibe/Reflections/Will/Sparks）剥离，正文保留。"""
    text = (
        "<mood>\n"
        "Vibe: 晚上21:19，工作日尾声，用户在后台跑构建。\n"
        "Reflections: 用户在专注工作。\n"
        "Will: 保持安静。\n"
        "Sparks: 想提醒用户休息。\n"
        "</mood>\n"
        "你回来啦！今天辛苦啦～"
    )
    cleaned, parsed = _both_clean(text)
    for keyword in ("Vibe", "Reflections", "Will", "Sparks", "晚上21:19", "保持安静"):
        assert keyword not in cleaned, f"不应残留 {keyword}: {cleaned!r}"
        assert keyword not in parsed, f"不应残留 {keyword}: {parsed!r}"
    assert "你回来啦" in cleaned, f"正文应保留: {cleaned!r}"
    assert "你回来啦" in parsed, f"正文应保留: {parsed!r}"


def test_unclosed_mood_block_still_cleaned():
    """e) <mood> 未闭合（缺 </mood>）时：前缀行 + 标签被剥，正文仍保留。"""
    text = "<mood>\nVibe: 未闭合的内省\nReflections: bbb\n正文回复内容"
    cleaned, parsed = _both_clean(text)
    assert "Vibe" not in cleaned, f"未闭合 mood 内的 Vibe 行应被剥: {cleaned!r}"
    assert "Reflections" not in cleaned, f"未闭合 mood 内的 Reflections 行应被剥: {cleaned!r}"
    assert "正文回复内容" in cleaned, f"正文应保留: {cleaned!r}"
    assert "正文回复内容" in parsed, f"正文应保留: {parsed!r}"


def test_only_mood_block_returns_empty_safely():
    """f) 全文本都是 mood 块 → 返回空串（不抛异常；气泡/TTS 路径对空串安全）。"""
    text = "<mood>Vibe: 全部都是内省内容</mood>"
    cleaned, parsed = _both_clean(text)
    assert cleaned == "" or cleaned.isspace(), f"应清空: {cleaned!r}"
    assert parsed == "" or parsed.isspace(), f"应清空: {parsed!r}"


def test_bracket_vibe_block_still_stripped_after_plain_line_strip():
    """既有行为不回归：方括号 [ Vibe: ... ] 块仍被剥，正文保留。"""
    text = "[ Vibe: 好奇 ]\n[ Sparks: 想聊天 ]\n你好呀[emotion:cute]"
    from core.harness_adapter import HanakoPetAdapter

    cleaned, emotion = HanakoPetAdapter.parse_emotion(text)
    assert "Vibe" not in cleaned and "Sparks" not in cleaned
    assert "你好呀" in cleaned
    assert emotion == "cute"


# ── c) 心跳：chat 阻塞期间 push、返回后停止、不覆盖回复气泡 ─────────────────


class _FakeExecutor:
    """替身 TTS 线程池：只记录 submit，不真跑。"""

    def __init__(self):
        self.submitted = []

    def submit(self, fn, *a, **k):
        self.submitted.append((fn, a, k))
        return None

    def shutdown(self, wait=True):
        pass


class _FakeAdapter:
    """最小 adapter：chat 返回固定回复，记录调用参数。"""

    def __init__(self, reply: str = "你好呀～[emotion:happy]", emotion: str = "happy", block_seconds: float = 0.0):
        self._reply = reply
        self._emotion = emotion
        self.block_seconds = block_seconds
        self.chat_started = threading.Event()
        self.calls = 0

    def chat(self, message, inject_memory=True, extra_context="", tools=None, source="user"):
        self.calls += 1
        self.chat_started.set()
        if self.block_seconds > 0:
            time.sleep(self.block_seconds)
        from core.harness_adapter import HanakoPetAdapter
        cleaned, parsed = HanakoPetAdapter.parse_emotion(self._reply)
        return cleaned or "…", parsed or self._emotion


def _make_engine(adapter=None):
    """构造最小可用 ConversationEngine（跳过 __init__ 副作用），直接驱动 _process_message。"""
    from core.conversation_engine import ConversationEngine

    engine = object.__new__(ConversationEngine)
    engine._lock = threading.Lock()
    engine._generation = 0
    engine._queue = __import__("collections").deque(maxlen=200)
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
    engine.replies = []
    
    # P0: Poke 缓存（跳过 __init__ 副作用，需手动初始化）
    engine._poke_cache: list[dict] = []
    engine._poke_timer = None

    def _on_reply(reply, emotion, anim, audio_path):
        engine.replies.append((reply, emotion, anim, audio_path))

    engine.on_reply = _on_reply
    engine._original_on_reply = _on_reply
    engine.on_progress = lambda msg: None  # 心跳默认空实现（对象.__new__ 跳过 __init__）
    engine._original_on_progress = engine.on_progress
    return engine


def test_heartbeat_pushes_during_blocked_chat_and_stops_after_return():
    """c) 心跳：chat 阻塞 >6s 期间至少 push 一次「还在想...」；返回后停止；
    回复仍正常回调（不丢回复）。"""
    adapter = _FakeAdapter(block_seconds=6.4)
    engine = _make_engine(adapter=adapter)
    progress = []
    engine.on_progress = lambda msg: progress.append(msg)

    msg = {"text": "你好", "character": "miku", "source": "user", "gen": 0}
    engine._process_message(msg)

    # 阻塞期间至少推了一次心跳
    assert any("还在想" in p for p in progress), f"chat 阻塞期间应推送心跳, got={progress!r}"

    # 返回后心跳停止：快照后等待 1.2s（> 线程退出时间），不应新增
    n_after_return = len(progress)
    time.sleep(1.2)
    assert len(progress) == n_after_return, (
        f"chat 返回后不应再推送心跳, got={progress!r}"
    )

    # 回复正常上气泡（心跳不吞回复）
    assert len(engine.replies) == 1, f"回复必须正常回调, replies={engine.replies!r}"
    assert engine.replies[0][0] == "你好呀～"


def test_heartbeat_no_push_when_chat_fast():
    """c) 心跳：快速 chat（<6s）不推送「还在想...」——不给正常回复加噪声气泡。"""
    adapter = _FakeAdapter(block_seconds=0.0)  # 立即返回
    engine = _make_engine(adapter=adapter)
    progress = []
    engine.on_progress = lambda msg: progress.append(msg)

    msg = {"text": "你好", "character": "miku", "source": "user", "gen": 0}
    engine._process_message(msg)

    assert progress == [], f"快速 chat 不应推送心跳, got={progress!r}"
    assert len(engine.replies) == 1


# ── d) UI：基础 tab 无下拉框后 M5 切换保存 agents[].enabled 仍生效 ──────────


class _StubPetManager:
    def __init__(self):
        self.agents = []


def _build_dialog(config: dict):
    from PySide6.QtWidgets import QApplication
    from ui.settings_dialog import SettingsDialog

    app = QApplication.instance() or QApplication([])
    return SettingsDialog(config=config, pet_manager=_StubPetManager())


def test_m5_switch_persists_enabled_after_dropdown_removed(monkeypatch):
    """d) 基础 tab 下拉框已删除后，角色包管理 tab（M5）「切换选中桌宠」仍写
    agents[].enabled + character + character_package；保留的 _sync_pkg_select
    兼容方法为安全空操作（不因无 _pkg_select 崩溃）。"""
    config = {
        "agents": [
            {"id": "miku", "enabled": True, "position": {"x": 0, "y": 0}, "scale": 1.0, "builtin": True},
            {"id": "shizuku", "enabled": False, "position": {"x": 0, "y": 0}, "scale": 1.0, "builtin": False},
        ],
        "character": "miku",
    }
    dialog = _build_dialog(config)
    assert not hasattr(dialog, "_pkg_select"), "基础 tab 不应有角色包下拉框"

    saved = {}
    import ui.settings_dialog as sd
    import avatar.factory as fac
    # 资源预校验在资源齐备时放行（本测试聚焦切换写盘逻辑，不校验资源）
    monkeypatch.setattr(fac, "resource_available", lambda cid: (True, ""))
    monkeypatch.setattr(sd, "save_config", lambda cfg: saved.update(cfg))
    monkeypatch.setattr(sd.QMessageBox, "information", lambda *a, **k: None)
    monkeypatch.setattr(sd.QMessageBox, "warning", lambda *a, **k: None)

    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QListWidgetItem
    item = QListWidgetItem("Shizuku v1.0.0")
    item.setData(Qt.UserRole, "shizuku")
    dialog._pkg_list.addItem(item)
    dialog._pkg_list.setCurrentRow(0)

    dialog._switch_pet()

    by_id = {a["id"]: a for a in saved.get("agents", [])}
    assert by_id["shizuku"]["enabled"] is True, "M5 切换后 shizuku 必须启用"
    assert by_id["miku"]["enabled"] is False, "M5 切换后 miku 必须禁用"
    assert saved.get("character") == "shizuku"
    assert saved.get("character_package") == "shizuku", "展示字段应与真相源同步"

    # 保留的兼容方法：无 _pkg_select 时安全空操作（不得抛 AttributeError）
    dialog._sync_pkg_select("shizuku")
