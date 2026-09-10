# -*- coding: utf-8 -*-
"""QA 独立回归（fresh eyes）：BugFix #4 mood 内省块上屏 + 基础 tab 下拉框删除。

现象（2026-08-24 21:20:12）：工具静默恢复后桌宠气泡显示
  "Vibe: 晚上21:19，工作日尾声，用户在后台跑构建..."
+ 11 秒 TTS 朗读完整段——<mood>...</mood> 内省块被当成用户回复上屏。

根因：clean_bubble_text（core/hanako_monitor.py）只去 MOOD/mood/thinking/tool/
status 标签形式，没覆盖 "Vibe:" "Reflections:" "Will:" "Sparks:" 结构化前缀；
parse_emotion（core/harness_adapter.py）只剥 [ Vibe: ... ] 方括号形式，没剥
纯文本 "Vibe:" 行——LLM 输出或历史恢复里带这些前缀时被原样上屏。

修复：两处都加 <mood>...</mood> 整块剥离 + Vibe/Reflections/Will/Sparks 前缀行剥离。

验证：
  a) clean_bubble_text 剥离 <mood> 整块（含 Vibe/Reflections/Will/Sparks）
  b) clean_bubble_text 剥离纯文本 "Vibe:" 前缀行
  c) parse_emotion 剥离纯文本 "Vibe:" 前缀行（chat_direct/chat_via_hanako 共用）
  d) 基础 tab 不再有 _pkg_select 下拉框（UI 删除验收，settings_dialog）
  e) _save 不再写 config.character_package
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── a/b) clean_bubble_text：<mood> 整块 + 结构化前缀 ─────────────────────────


def test_clean_bubble_text_strips_mood_block_entirely():
    """<mood>...</mood> 内省块（Vibe/Reflections/Will/Sparks）必须整段剥掉，
    只保留真实回复文本。"""
    from core.hanako_monitor import clean_bubble_text

    text = (
        "<mood>\n"
        "Vibe: 晚上21:19，工作日尾声，用户在后台跑构建、开着游戏界面。\n"
        "Reflections: 用户在专注工作。\n"
        "Will: 保持安静。\n"
        "Sparks: 想提醒用户休息。\n"
        "</mood>\n"
        "你回来啦！今天辛苦啦～"
    )
    cleaned = clean_bubble_text(text)
    assert "Vibe" not in cleaned, f"不应残留 Vibe: {cleaned!r}"
    assert "Reflections" not in cleaned, f"不应残留 Reflections: {cleaned!r}"
    assert "Will" not in cleaned, f"不应残留 Will: {cleaned!r}"
    assert "Sparks" not in cleaned, f"不应残留 Sparks: {cleaned!r}"
    assert "晚上21:19" not in cleaned, f"不应残留 mood 内容: {cleaned!r}"
    assert "你回来啦" in cleaned, f"真实回复应保留: {cleaned!r}"


def test_clean_bubble_text_strips_plain_vibe_prefix_line():
    """无 <mood> 包裹的纯文本 "Vibe: ..." 前缀行（LLM 直出/历史恢复形态）
    也要剥掉，避免上屏。"""
    from core.hanako_monitor import clean_bubble_text

    text = "Vibe: 晚上21:19，工作日尾声，用户在后台跑构建、开着游戏界面。\nReflections: 有点困。\n你好呀"
    cleaned = clean_bubble_text(text)
    assert "Vibe" not in cleaned, f"不应残留 Vibe: {cleaned!r}"
    assert "Reflections" not in cleaned, f"不应残留 Reflections: {cleaned!r}"
    assert "你好呀" in cleaned, f"真实回复应保留: {cleaned!r}"


def test_clean_bubble_text_preserves_normal_reply():
    """正常回复不带 mood 前缀时不受影响。"""
    from core.hanako_monitor import clean_bubble_text

    text = "你回来啦！今天辛苦啦～[emotion:happy]"
    cleaned = clean_bubble_text(text)
    assert "你回来啦" in cleaned
    assert "[emotion" not in cleaned, "emotion 标签仍应被剥离"


# ── c) parse_emotion：纯文本 "Vibe:" 前缀行（chat_direct/chat_via_hanako 共用）──


def test_parse_emotion_strips_plain_vibe_prefix_line():
    """parse_emotion 必须剥纯文本（无方括号）"Vibe:" 行——工具静默恢复返回的
    历史权威文本正是这种形态（2026-08-24 21:20:12 复现）。"""
    from core.harness_adapter import HanakoPetAdapter

    text = (
        "Vibe: 晚上21:19，工作日尾声，用户在后台跑构建、开着游戏界面。\n"
        "Reflections: 有点困。\n"
        "Will: 保持安静。\n"
        "Sparks: 想提醒用户休息。\n"
        "你回来啦！[emotion:happy]"
    )
    cleaned, emotion = HanakoPetAdapter.parse_emotion(text)
    assert "Vibe" not in cleaned, f"不应残留 Vibe: {cleaned!r}"
    assert "Reflections" not in cleaned, f"不应残留 Reflections: {cleaned!r}"
    assert "Will" not in cleaned, f"不应残留 Will: {cleaned!r}"
    assert "Sparks" not in cleaned, f"不应残留 Sparks: {cleaned!r}"
    assert emotion == "happy"
    assert "你回来啦" in cleaned, f"真实回复应保留: {cleaned!r}"


def test_parse_emotion_strips_bracketed_vibe_block():
    """方括号形式 [ Vibe: ... ] 仍应被剥（既有行为不回归）。"""
    from core.harness_adapter import HanakoPetAdapter

    text = "[ Vibe: 好奇 ]\n[ Sparks: 想聊天 ]\n你好呀[emotion:cute]"
    cleaned, emotion = HanakoPetAdapter.parse_emotion(text)
    assert "Vibe" not in cleaned
    assert "Sparks" not in cleaned
    assert "你好呀" in cleaned
    assert emotion == "cute"


def test_parse_emotion_strips_mood_block_entirely():
    """parse_emotion 也要剥 <mood>...</mood> 整块。"""
    from core.harness_adapter import HanakoPetAdapter

    text = "<mood>Vibe: 开心\nReflections: 无</mood>\n哈喽～[emotion:happy]"
    cleaned, emotion = HanakoPetAdapter.parse_emotion(text)
    assert "Vibe" not in cleaned
    assert "Reflections" not in cleaned
    assert "哈喽" in cleaned
    assert emotion == "happy"


# ── d/e) 基础 tab 下拉框删除（UI 验收）───────────────────────────────────────


class _StubPetManager:
    """仅用于让 SettingsDialog 构建（pet_manager 传入后不应再触发下拉框）。"""

    def __init__(self):
        self.agents = []


def _build_dialog(config: dict):
    from PySide6.QtWidgets import QApplication
    from ui.settings_dialog import SettingsDialog

    app = QApplication.instance() or QApplication([])
    return SettingsDialog(config=config, pet_manager=_StubPetManager())


def test_basic_tab_has_no_pkg_select_dropdown():
    """BugFix #4 UI 验收：基础 tab 不再有"角色包"下拉框（用户截图的卡片删除）。"""
    config = {
        "agents": [
            {"id": "miku", "enabled": True, "position": {"x": 0, "y": 0}, "scale": 1.0, "builtin": True},
        ],
        "character": "miku",
    }
    dialog = _build_dialog(config)
    assert not hasattr(dialog, "_pkg_select"), "基础 tab 不应再构建角色包下拉框"


def test_save_does_not_write_character_package(monkeypatch):
    """BugFix #4 保存验收：_save 不再写 config.character_package。"""
    config = {
        "agents": [
            {"id": "miku", "enabled": True, "position": {"x": 0, "y": 0}, "scale": 1.0, "builtin": True},
        ],
        "character": "miku",
    }
    dialog = _build_dialog(config)

    saved = {}
    import ui.settings_dialog as sd
    monkeypatch.setattr(sd, "save_config", lambda cfg: saved.update(cfg))
    monkeypatch.setattr(dialog, "_save_env", lambda: None)

    dialog._save()

    assert "character_package" not in saved, "保存路径不应再写 character_package 死字段"
    assert "behavior" in saved, "其他基础设置仍应保存"


def test_m5_switch_pet_still_works(monkeypatch):
    """BugFix #4：角色包管理 tab「切换选中桌宠」功能保留（M5）。"""
    config = {
        "agents": [
            {"id": "miku", "enabled": True, "position": {"x": 0, "y": 0}, "scale": 1.0, "builtin": True},
        ],
        "character": "miku",
    }
    dialog = _build_dialog(config)

    saved = {}
    import ui.settings_dialog as sd
    import avatar.factory as fac
    # 资源预校验在资源齐备时放行（本测试聚焦切换写盘逻辑，不校验资源）
    monkeypatch.setattr(fac, "resource_available", lambda cid: (True, ""))
    monkeypatch.setattr(sd, "save_config", lambda cfg: saved.update(cfg))

    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QListWidgetItem
    item = QListWidgetItem("Shizuku v1.0.0")
    item.setData(Qt.UserRole, "shizuku")
    dialog._pkg_list.addItem(item)
    dialog._pkg_list.setCurrentRow(0)

    monkeypatch.setattr(sd.QMessageBox, "information", lambda *a, **k: None)
    monkeypatch.setattr(sd.QMessageBox, "warning", lambda *a, **k: None)
    dialog._switch_pet()

    by_id = {a["id"]: a for a in saved.get("agents", [])}
    assert by_id["shizuku"]["enabled"] is True, "M5 切换仍应启用目标角色"
    assert by_id["miku"]["enabled"] is False
    assert saved.get("character") == "shizuku"
