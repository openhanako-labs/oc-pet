# -*- coding: utf-8 -*-
"""🤝 派活设置页 UI 验收（用户 2026-09-19："这个填写不应该加到设置里面吗"）。

用户是对的：开关与白名单不该让他手改 JSON。这个文件就是那条要求的验收，
也是防止"页面上有、但保存不进去"的护栏。

⚠️ ``_save()`` 末尾会 ``save_config()`` **写真实 config.json**——
本文件一律把它 monkeypatch 成内存收集，绝不落盘。
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TAB_TITLE = "🤝 派活"


class _StubPetManager:
    def __init__(self):
        self.agents = []


def _build_dialog(monkeypatch, config):
    from PySide6.QtWidgets import QApplication

    import ui.settings_dialog as sd

    saved: dict = {}
    monkeypatch.setattr(sd, "save_config", lambda cfg: saved.update(cfg))
    monkeypatch.setattr(sd.QMessageBox, "information", lambda *a, **k: None)
    monkeypatch.setattr(sd.QMessageBox, "warning", lambda *a, **k: None)
    try:
        import avatar.factory as fac
        monkeypatch.setattr(fac, "resource_available", lambda cid: (True, ""))
    except Exception:
        pass

    app = QApplication.instance() or QApplication([])  # noqa: F841
    return sd.SettingsDialog(config=config, pet_manager=_StubPetManager()), saved


def _full_config(**a2a):
    from config import load_config

    cfg = load_config()
    cfg["a2a"] = a2a
    return cfg


def _tab_index(dialog):
    for i in range(dialog._main_tabs.count()):
        if dialog._main_tabs.tabText(i) == TAB_TITLE:
            return i
    return -1


# ── 页面存在 ──────────────────────────────────────────────


def test_a2a_tab_is_present(monkeypatch):
    d, _ = _build_dialog(monkeypatch, _full_config())
    assert _tab_index(d) >= 0, "设置面板里没有「🤝 派活」页"


def test_tab_page_has_real_widgets(monkeypatch):
    d, _ = _build_dialog(monkeypatch, _full_config(enabled=True, allowed_agents=["kurisu"]))
    page = d._main_tabs.widget(_tab_index(d))
    assert page is not None and page.layout() is not None
    assert hasattr(d, "a2a_enabled")
    assert set(d.a2a_agent_checks) >= {"kurisu", "alice", "glados"}


def test_tab_hint_no_longer_says_restart(monkeypatch):
    """提示不能再说"要重启"——已经热重载了，写错会让人白重启。"""
    from PySide6.QtWidgets import QLabel

    d, _ = _build_dialog(monkeypatch, _full_config())
    page = d._main_tabs.widget(_tab_index(d))
    texts = [w.text() for w in page.findChildren(QLabel)]
    joined = "\n".join(texts)
    assert "立即生效" in joined
    # 旧说法（已不成立）不能留着
    assert "重启桌宠才生效" not in joined
    assert "要重启" not in joined


# ── 读配置 ────────────────────────────────────────────────


def test_defaults_are_off_and_empty(monkeypatch):
    """没配过 → 关、白名单空、配额是保守值。"""
    d, _ = _build_dialog(monkeypatch, _full_config())
    assert d.a2a_enabled.isChecked() is False
    assert all(not cb.isChecked() for cb in d.a2a_agent_checks.values())
    assert d.a2a_max_hour.value() == 6
    assert d.a2a_max_day.value() == 30
    assert d.a2a_timeout.value() == 180


def test_existing_config_is_reflected(monkeypatch):
    d, _ = _build_dialog(monkeypatch, _full_config(
        enabled=True, allowed_agents=["kurisu", "alice"],
        max_per_hour=3, max_per_day=9, timeout_seconds=240,
    ))
    assert d.a2a_enabled.isChecked() is True
    assert d.a2a_agent_checks["kurisu"].isChecked() is True
    assert d.a2a_agent_checks["alice"].isChecked() is True
    assert d.a2a_agent_checks["rebecca"].isChecked() is False
    assert (d.a2a_max_hour.value(), d.a2a_max_day.value()) == (3, 9)
    assert d.a2a_timeout.value() == 240


def test_unknown_agent_goes_into_extra_line(monkeypatch):
    d, _ = _build_dialog(monkeypatch, _full_config(allowed_agents=["kurisu", "someone_new"]))
    assert "someone_new" in d.a2a_extra_agents.text()
    assert d.a2a_agent_checks["kurisu"].isChecked() is True


# ── 写配置 ────────────────────────────────────────────────


def test_save_writes_checked_agents(monkeypatch):
    d, saved = _build_dialog(monkeypatch, _full_config())
    d.a2a_enabled.setChecked(True)
    d.a2a_agent_checks["kurisu"].setChecked(True)
    d.a2a_agent_checks["alice"].setChecked(True)
    d.a2a_max_hour.setValue(4)
    d._save()

    assert saved["a2a"]["enabled"] is True
    # 顺序无所谓（按助手表排列），关键是两个都在
    assert sorted(saved["a2a"]["allowed_agents"]) == ["alice", "kurisu"]
    assert saved["a2a"]["max_per_hour"] == 4


def test_save_keeps_empty_whitelist_empty(monkeypatch):
    """不勾任何人 → 白名单必须是空的（一个都不许是护栏，不能被 UI 悄悄放宽）。"""
    d, saved = _build_dialog(monkeypatch, _full_config(enabled=True, allowed_agents=["kurisu"]))
    d.a2a_agent_checks["kurisu"].setChecked(False)
    d._save()
    assert saved["a2a"]["allowed_agents"] == []


def test_save_merges_and_dedupes_extra_ids(monkeypatch):
    d, saved = _build_dialog(monkeypatch, _full_config())
    d.a2a_agent_checks["kurisu"].setChecked(True)
    d.a2a_extra_agents.setText("kurisu, extra1，extra2")
    d._save()
    assert saved["a2a"]["allowed_agents"] == ["kurisu", "extra1", "extra2"]


def test_save_agrees_with_the_delegator(monkeypatch):
    """UI 存出来的配置，必须能被 core/a2a 直接吃下（表里不一就白做）。"""
    from core.a2a import build_from_config

    d, saved = _build_dialog(monkeypatch, _full_config())
    d.a2a_enabled.setChecked(True)
    d.a2a_agent_checks["kurisu"].setChecked(True)
    d._save()

    delegator = build_from_config(saved, lambda a: None, lambda s, t, to: "")
    assert delegator.enabled is True
    assert delegator.allowed_agents == ["kurisu"]
    assert delegator.check("kurisu")[0] is True
    assert delegator.check("alice")[0] is False
