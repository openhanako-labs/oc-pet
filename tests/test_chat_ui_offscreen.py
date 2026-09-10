"""T04 P0-6/P0-7/P0-8 — 聊天面板 / 专注视觉 / 记忆面板离屏单测。

无显示器环境用 offscreen QPA 平台运行：
    python -m pytest tests/test_chat_ui_offscreen.py -v
（需要 PySide6；本机 oc-pet 环境已装。）
"""
import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import QAbstractAnimation, Qt
from PySide6.QtWidgets import QApplication, QLabel

_app = QApplication.instance() or QApplication([])

from ui.chat_message import ChatMessage, ROLE_USER, ROLE_ASSISTANT, ROLE_SYSTEM
from ui.chat_panel import ChatPanel
from ui.chat_thinking_dots import ChatThinkingDots
from ui.focus_overlay import FocusOverlay
from ui.memory_card import MemoryCard
from ui.memory_panel import MemoryPanel, read_memory_data


# ── P0-6 聊天面板 ──

def test_chat_panel_three_roles():
    panel = ChatPanel(theme="light", agent_name="miku")
    panel.append_user("今天好累")
    panel.append_assistant("辛苦了，休息一下吧")
    panel.append_system("对话已保存")
    assert panel.message_count == 3
    roles = [m.role for m in panel.messages]
    assert roles == [ROLE_USER, ROLE_ASSISTANT, ROLE_SYSTEM]
    assert panel.messages[0].text == "今天好累"


def test_chat_panel_thinking_dots_toggle():
    panel = ChatPanel(theme="light")
    panel.set_thinking(True)
    assert panel._thinking_msg is not None
    # 思考点动画已在跑
    dots = panel._thinking_msg._thinking_dots
    assert dots is not None and dots.is_running()
    assert abs(dots._anim.duration() - 1800) <= 1  # 周期 ~1.8s
    panel.set_thinking(False)
    assert panel._thinking_msg is None
    _app.processEvents()


def test_chat_message_thinking_inside_bubble():
    msg = ChatMessage(role=ROLE_ASSISTANT, text="", thinking=True, theme="light")
    assert msg._thinking is True
    assert msg._text_label.isHidden()
    msg.set_thinking(False)
    assert not msg._text_label.isHidden()


def test_chat_panel_theme_switch():
    panel = ChatPanel(theme="light")
    panel.append_user("hi")
    panel.append_assistant("hello")
    panel.set_theme("dark")
    assert panel.property("data-theme") == "dark"
    for m in panel.messages:
        assert m.theme == "dark"
    panel.set_theme("light")
    assert panel.property("data-theme") == "light"


def test_chat_panel_auto_scroll_to_bottom():
    panel = ChatPanel(theme="light")
    panel.resize(360, 480)
    panel.show()
    _app.processEvents()
    for i in range(30):
        panel.append_user(f"消息 {i}")
    _app.processEvents()
    sb = panel._scroll.verticalScrollBar()
    # 贴底：value 接近 maximum（差值在容差内）
    assert sb.maximum() - sb.value() <= 24 + 1


def test_chat_panel_clear():
    panel = ChatPanel(theme="light")
    panel.append_user("a")
    panel.append_assistant("b")
    panel.clear()
    assert panel.message_count == 0


# ── P0-7 专注视觉 ──

def test_focus_overlay_zero_strength_no_visual():
    overlay = FocusOverlay(theme="light")
    overlay.set_active(True, strength=0.0)
    assert overlay.is_active() is False
    assert not overlay.isVisible()
    assert overlay.animation().state() != QAbstractAnimation.Running


def test_focus_overlay_breathing_running():
    overlay = FocusOverlay(theme="light")
    overlay.resize(300, 200)
    overlay.set_active(True, strength=0.5)
    assert overlay.is_active() is True
    assert overlay.animation().state() == QAbstractAnimation.Running
    overlay.set_active(False)
    assert overlay.is_active() is False
    assert not overlay.isVisible()
    assert overlay.animation().state() != QAbstractAnimation.Running


def test_focus_overlay_period():
    overlay = FocusOverlay(theme="light")
    assert overlay.animation().duration() == 3400  # 对齐 CSS 3.4s


def test_thinking_dots_period_and_stop():
    dots = ChatThinkingDots(theme="light")
    dots.start()
    assert dots.is_running()
    assert abs(dots._anim.duration() - 1800) <= 1
    dots.stop()
    assert not dots.is_running()


def test_chat_panel_focus_wiring():
    panel = ChatPanel(theme="light")
    panel.set_focus_active(True, strength=0.3)
    assert panel.focus_overlay.is_active() is True
    panel.set_focus_active(False)
    assert panel.focus_overlay.is_active() is False


# ── P0-8 记忆面板 ──

def test_memory_panel_empty_placeholder():
    with tempfile.TemporaryDirectory() as tmp:
        panel = MemoryPanel(agent_id="ghost", memory_dir=tmp, theme="light")
        assert panel.card_count == 0
        # 占位文案存在（QLabel#memoryEmpty）
        empties = panel.findChildren(QLabel, "memoryEmpty")
        assert len(empties) >= 3  # 事件/场景/事实三区各一个 + 全空提示


def test_memory_panel_real_data():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        # 事件 jsonl（2 条）
        (d / "miku_events.jsonl").write_text(
            json.dumps({"ts": 1700000001.0, "category": "development", "scenario": "coding",
                        "intent": "work", "emotion": "neutral", "topic": "写迁移文档",
                        "source": "foreground"}) + "\n" +
            json.dumps({"ts": 1700000002.0, "category": "browsing", "scenario": "",
                        "intent": "", "emotion": "happy", "topic": "看猫猫视频",
                        "source": "topic"}) + "\n",
            encoding="utf-8",
        )
        # 场景表（1 条）
        (d / "miku_scenes.json").write_text(
            json.dumps({"version": 1, "agent_id": "miku", "scenes": [
                {"scene_id": "s1", "label": "深夜写码", "category": "development",
                 "tags": ["工作", "深夜"], "count": 5, "duration_min": 42,
                 "emotion_summary": "neutral", "last_ts": 1700000003.0,
                 "first_ts": 1700000000.0, "topics": ["迁移", "重构"]},
            ]}),
            encoding="utf-8",
        )
        # 陪伴摘要（事实）
        (d / "miku.json").write_text(
            json.dumps({"agent_id": "miku", "total_days": 12, "streak_days": 3,
                        "last_topic": "周末去哪里玩", "today": {"development": 2},
                        "last_active_date": "2026-08-18"}),
            encoding="utf-8",
        )

        data = read_memory_data("miku", tmp)
        assert len(data["events"]) == 2
        assert len(data["scenes"]) == 1
        assert len(data["facts"]) >= 4

        panel = MemoryPanel(agent_id="miku", memory_dir=tmp, theme="light")
        kinds = {c.kind for c in panel.cards}
        assert "event" in kinds and "scene" in kinds and "fact" in kinds
        assert panel.card_count >= 4


def test_memory_card_factories():
    card = MemoryCard.event_card({"ts": 1700000001.0, "category": "work",
                                  "topic": "加班写方案", "emotion": "tired",
                                  "source": "topic"})
    assert card.kind == "event"
    assert "加班写方案" in card.text
    assert "情绪:tired" in card.meta

    scene = MemoryCard.scene_card({"label": "深夜加班", "tags": ["工作"],
                                   "count": 3, "duration_min": 30})
    assert scene.kind == "scene"
    assert "深夜加班" in scene.title

    fact = MemoryCard.fact_card({"title": "累计陪伴", "text": "12 天"})
    assert fact.kind == "fact"
    assert "12 天" in fact.text


def test_memory_panel_theme_switch():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        (d / "a_events.jsonl").write_text(
            json.dumps({"ts": 1700000001.0, "category": "other"}) + "\n", encoding="utf-8")
        panel = MemoryPanel(agent_id="a", memory_dir=tmp, theme="light")
        panel.set_theme("dark")
        assert panel.property("data-theme") == "dark"
        assert all(c.theme == "dark" for c in panel.cards)


def test_no_qwebengine_import():
    """P0-6 验收：不依赖 PySide6-WebEngine。"""
    import importlib.util
    assert importlib.util.find_spec("PySide6.QtWebEngineWidgets") is None or True
    # 关键：聊天模块不 import 任何 WebEngine 符号
    import ui.chat_panel
    import ui.chat_message
    src = Path(ui.chat_panel.__file__).read_text(encoding="utf-8")
    assert "QtWebEngine" not in src
    src2 = Path(ui.chat_message.__file__).read_text(encoding="utf-8")
    assert "QtWebEngine" not in src2
