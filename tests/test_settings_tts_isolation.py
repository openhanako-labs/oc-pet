"""设置面板：全局 TTS 与「桌宠独立配置」的写盘隔离。

## 起因（用户实测反馈）

> 我修改了 ophelia 的 TTS 为 qwen 后，全局也变成了 qwen

## 根因

`_save()` **无条件**把「功能 → 语音」页那个全局 TTS 下拉的当前值
写回 `config.tts.provider`。而那个下拉的初始值来自 config 本身
（不是用户意图）。于是：

- 用户在「基础 → 桌宠独立配置」改某行、点保存
- 全局下拉的值（可能来自上次会话遗留）也被一起落盘
- 用户看不到那个下拉，只看到结果 → 感知为「互相覆盖」

## 修复

给全局下拉加**脏标记**：只有用户真的动过它，保存时才写全局。
独立配置行（`agents[].tts`）的写入逻辑不变——它本来就只写自己那一份。
"""
from __future__ import annotations

import copy

import pytest


class _StubPM:
    def list_pets(self):
        return []

    def get_pet(self, *a, **k):
        return None


def _make_dialog(config: dict):
    from PySide6.QtWidgets import QApplication
    from ui.settings_dialog import SettingsDialog

    app = QApplication.instance() or QApplication([])
    return SettingsDialog(config=copy.deepcopy(config), pet_manager=_StubPM())


def _base(provider: str = "edge") -> dict:
    return {
        "tts": {
            "enabled": True,
            "provider": provider,
            "volume": 0.8,
            "edge_voice": "zh-CN-XiaoxiaoNeural",
        },
        "dialog": {"agent_id": "ophelia"},
        "agents": [
            {"id": "miku", "enabled": True, "position": {"x": 0, "y": 0},
             "scale": 1.0, "builtin": True},
        ],
        "character": "miku",
    }


def _provider_of(out: dict) -> str:
    return out.get("tts", {}).get("provider")


def _agent_tts(out: dict, aid: str = "miku"):
    entry = next((a for a in out.get("agents", []) if a.get("id") == aid), None)
    return (entry or {}).get("tts")


# ══════════════════════════════════════════════════════════════
#  核心：改独立配置不得动全局
# ══════════════════════════════════════════════════════════════

def test_per_pet_edit_does_not_touch_global():
    """★ 核心断言：只改独立配置行，全局 TTS 必须原样不动。

    这是用户报的那个 bug 的直接回归测试。
    """
    dlg = _make_dialog(_base(provider="qwen"))
    eng = dlg._per_pet_rows["miku"][0]
    eng.setCurrentIndex(eng.findText("微软 Edge (免费)"))
    dlg._save()

    out = dlg.get_config()
    assert _provider_of(out) == "qwen", "全局不该被独立配置的保存动作改写"
    assert _agent_tts(out) == {"provider": "edge"}, "独立配置应写进 agents[].tts"


def test_no_edit_keeps_global_unchanged():
    """什么都不改，全局保持原值。"""
    dlg = _make_dialog(_base(provider="edge"))
    dlg._save()
    assert _provider_of(dlg.get_config()) == "edge"


@pytest.mark.parametrize("initial", ["cosyvoice", "edge", "qwen"])
def test_global_preserved_for_every_initial_provider(initial):
    """无论全局原本是什么，只改独立配置都不应改变它。"""
    dlg = _make_dialog(_base(provider=initial))
    eng = dlg._per_pet_rows["miku"][0]
    eng.setCurrentIndex(eng.findText("MIMO TTS"))
    dlg._save()
    assert _provider_of(dlg.get_config()) == initial


# ══════════════════════════════════════════════════════════════
#  反向：用户真改了全局，就要写进去
# ══════════════════════════════════════════════════════════════

def test_explicit_global_change_is_persisted():
    """用户真的动了全局下拉 → 必须写进全局（修复不能把功能改没）。"""
    dlg = _make_dialog(_base(provider="qwen"))
    dlg.tts_provider.setCurrentIndex(dlg.tts_provider.findText("微软 Edge (免费)"))
    dlg._save()
    assert _provider_of(dlg.get_config()) == "edge"


def test_global_change_does_not_write_agent_tts():
    """改全局不该给 agents[] 塞 tts（独立配置保持“沿用全局”）。"""
    dlg = _make_dialog(_base(provider="edge"))
    dlg.tts_provider.setCurrentIndex(dlg.tts_provider.findText("Qwen3-TTS 本地"))
    dlg._save()
    assert _agent_tts(dlg.get_config()) is None


# ══════════════════════════════════════════════════════════════
#  Edge 音色：同样只在动过时才写
# ══════════════════════════════════════════════════════════════

def test_edge_voice_not_written_when_untouched():
    """没碰 Edge 音色下拉，就不该重写 edge_voice。"""
    dlg = _make_dialog(_base())
    dlg._save()
    assert dlg.get_config()["tts"]["edge_voice"] == "zh-CN-XiaoxiaoNeural"


def test_edge_voice_written_when_changed():
    dlg = _make_dialog(_base())
    voices = dlg.tts_edge_voice
    if voices.count() < 2:
        pytest.skip("只有一个 Edge 音色，无法测试切换")
    voices.setCurrentIndex(1)
    dlg._save()
    assert dlg.get_config()["tts"]["edge_voice"] == voices.currentText()


# ══════════════════════════════════════════════════════════════
#  独立配置行的完整语义
# ══════════════════════════════════════════════════════════════

def test_per_pet_engine_and_voice_written_together():
    dlg = _make_dialog(_base())
    eng, voice, _ag = dlg._per_pet_rows["miku"]
    eng.setCurrentIndex(eng.findText("Qwen3-TTS 本地"))
    idx = voice.findText("qwen|ophelia")
    if idx >= 0:
        voice.setCurrentIndex(idx)
    dlg._save()
    tts = _agent_tts(dlg.get_config())
    assert tts["provider"] == "qwen"
    if idx >= 0:
        assert tts.get("voice") == "ophelia"


def test_per_pet_revert_to_global_clears_override():
    """把行切回「沿用全局」→ agents[].tts 里的托管键应被清掉。"""
    dlg = _make_dialog(_base())
    eng = dlg._per_pet_rows["miku"][0]
    eng.setCurrentIndex(eng.findText("Qwen3-TTS 本地"))
    eng.setCurrentIndex(0)  # 沿用全局
    dlg._save()
    assert _agent_tts(dlg.get_config()) is None


def test_agent_binding_written_to_agents_dialog():
    """独立配置的「助手」列写进 agents[].dialog.agent_id。"""
    dlg = _make_dialog(_base())
    ag = dlg._per_pet_rows["miku"][2]
    target = ag.findData("ophelia-pet")
    if target < 0:
        pytest.skip("该环境未发现 ophelia-pet agent")
    ag.setCurrentIndex(target)
    dlg._save()
    entry = next(a for a in dlg.get_config()["agents"] if a["id"] == "miku")
    assert entry.get("dialog", {}).get("agent_id") == "ophelia-pet"
    # 全局 dialog 不受影响
    assert dlg.get_config()["dialog"]["agent_id"] == "ophelia"


def test_dirty_flags_default_false():
    """构造后脏标记必须是 False——否则修复等于没做。"""
    dlg = _make_dialog(_base())
    assert dlg._tts_provider_dirty is False
    assert dlg._tts_edge_voice_dirty is False


def test_provider_always_present_in_output():
    """即使没动过，输出里也必须有 provider（下游读它，不能缺）。"""
    cfg = _base()
    cfg["tts"].pop("provider")
    dlg = _make_dialog(cfg)
    dlg._save()
    assert _provider_of(dlg.get_config()), "provider 缺省时也要有兜底值"
