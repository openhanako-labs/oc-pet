"""P1-7/P1-8 — 角色卡 + HUD 风格统一离屏单测。

无显示器环境用 offscreen QPA 平台运行：
    python -m pytest tests/test_character_card_hud_offscreen.py -v
（需要 PySide6；本机 oc-pet 环境已装。）

覆盖：
- 角色卡：三类信息渲染（档案/标签/记忆统计）、空角色占位、双主题
- HUD：取色正确（颜色来自 neko_palette 而非硬编码，抽查关键控件）
- 无回归：StatusHUD / AmadeusHUD 既有功能（set_stats/set_emotion/set_counts）不变
"""
import json
import os
import re
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication, QLabel

_app = QApplication.instance() or QApplication([])

from ui.character_card import (
    CharacterCard, read_character_profile, read_memory_stats, _parse_identity,
)
from ui.status_hud import StatusHUD, _bar_color
from ui.amadeus_hud import AmadeusHUD
from ui.theme.neko_palette import NEKO_LAYOUT, NEKO_PALETTE, neko_qcolor

# QColor(r, g, b) 字面量（硬编码色值）检测
_HARDCODED_QCOLOR_RE = re.compile(r"QColor\(\s*\d+\s*,")


# ── 测试夹具 ─────────────────────────────────────────────

def _make_character(tmp: str, agent_id: str, with_memory: bool = True) -> None:
    """构造临时角色目录 + 记忆文件。"""
    d = Path(tmp) / "characters" / agent_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "pet.json").write_text(json.dumps({
        "id": agent_id,
        "name": "测试喵",
        "description": "来自 pet.json 的简介",
        "personality": ["勤劳", "乐观"],
    }, ensure_ascii=False), encoding="utf-8")
    (d / "identity.md").write_text(
        "# 测试喵\n\n你是一只测试用的小猫咪。\n\n"
        "## 性格\n- 爱睡觉\n- 爱撒娇\n",
        encoding="utf-8",
    )
    if with_memory:
        md = Path(tmp) / "memory"
        md.mkdir(parents=True, exist_ok=True)
        (md / f"{agent_id}_events.jsonl").write_text(
            json.dumps({"ts": 1.0}) + "\n" + json.dumps({"ts": 2.0}) + "\n",
            encoding="utf-8",
        )
        (md / f"{agent_id}_scenes.json").write_text(
            json.dumps({"scenes": [{"scene_id": "s1"}, {"scene_id": "s2"}]}),
            encoding="utf-8",
        )
        (md / f"{agent_id}_facts.json").write_text(
            json.dumps([{"text": "f1"}, {"text": "f2"}, {"text": "f3"}]),
            encoding="utf-8",
        )
        (md / f"{agent_id}_reflections.json").write_text(
            json.dumps({"reflections": [{"id": "r1"}]}),
            encoding="utf-8",
        )


# ── P1-7 角色卡：数据读取（纯函数） ─────────────────────

def test_parse_identity():
    text = ("# 月薪喵\n\n你是一只白色的小办公室猫咪。\n\n"
            "## 性格\n- 勤劳但有点迷糊\n- 喜欢踩键盘\n\n"
            "## 规则\n- 不适用\n")
    name, intro, personality = _parse_identity(text)
    assert name == "月薪喵"
    assert "小办公室猫咪" in intro
    assert personality == ["勤劳但有点迷糊", "喜欢踩键盘"]


def test_read_character_profile_builtin():
    with tempfile.TemporaryDirectory() as tmp:
        _make_character(tmp, "t1")
        prof = read_character_profile("t1", characters_dir=Path(tmp) / "characters")
        assert prof["name"] == "测试喵"
        assert "测试用的小猫咪" in prof["intro"]   # identity 首段优先
        assert "爱睡觉" in prof["personality"] or "勤劳" in prof["personality"]
        assert prof["source"] == "builtin"


def test_read_character_profile_empty():
    with tempfile.TemporaryDirectory() as tmp:
        prof = read_character_profile("ghost", characters_dir=Path(tmp) / "characters")
        assert prof["name"] == "ghost"       # 名字回退 agent_id
        assert prof["intro"] == ""
        assert prof["personality"] == []
        assert prof["avatar_path"] == ""
        assert prof["source"] == "none"


def test_read_memory_stats_counts():
    with tempfile.TemporaryDirectory() as tmp:
        _make_character(tmp, "t4")
        stats = read_memory_stats("t4", memory_dir=Path(tmp) / "memory")
        assert stats == {"events": 2, "scenes": 2, "facts": 3, "reflections": 1}


def test_read_memory_stats_missing_files_zero():
    with tempfile.TemporaryDirectory() as tmp:
        stats = read_memory_stats("nobody", memory_dir=Path(tmp) / "memory")
        assert stats == {"events": 0, "scenes": 0, "facts": 0, "reflections": 0}


# ── P1-7 角色卡：面板渲染 ───────────────────────────────

def test_character_card_renders_info():
    with tempfile.TemporaryDirectory() as tmp:
        _make_character(tmp, "t2")
        card = CharacterCard(
            agent_id="t2", character_id="t2",
            characters_dir=Path(tmp) / "characters",
            memory_dir=Path(tmp) / "memory", theme="light",
        )
        # 名字 / 简介
        assert card._name_label.text() == "测试喵"
        assert "测试用的小猫咪" in card._intro_label.text()
        # 性格标签 chips
        tags = [t.text() for t in card.findChildren(QLabel, "cardTag")]
        assert len(tags) >= 2
        # 记忆统计数字
        nums = {k: v[0].text() for k, v in card._stat_boxes.items()}
        assert nums == {"events": "2", "scenes": "2", "facts": "3", "reflections": "1"}
        assert card._empty_label.isHidden()


def test_character_card_empty_placeholder():
    with tempfile.TemporaryDirectory() as tmp:
        card = CharacterCard(
            agent_id="ghost", character_id="ghost",
            characters_dir=Path(tmp) / "characters",
            memory_dir=Path(tmp) / "memory", theme="light",
        )
        assert card._name_label.text() == "ghost"
        assert not card._empty_label.isHidden()       # 占位可见
        assert card._intro_label.isHidden()
        assert all(v[0].text() == "0" for v in card._stat_boxes.values())


def test_character_card_theme_switch():
    with tempfile.TemporaryDirectory() as tmp:
        _make_character(tmp, "t3")
        card = CharacterCard(
            agent_id="t3", character_id="t3",
            characters_dir=Path(tmp) / "characters",
            memory_dir=Path(tmp) / "memory", theme="light",
        )
        assert card.theme == "light"
        card.set_theme("dark")
        assert card.property("data-theme") == "dark"
        assert card.theme == "dark"
        card.set_theme("light")
        assert card.theme == "light"
        # 渲染不崩溃
        pix = card.grab()
        assert not pix.isNull()


def test_character_card_set_agent_refresh():
    with tempfile.TemporaryDirectory() as tmp:
        _make_character(tmp, "t5")
        card = CharacterCard(
            agent_id="other", character_id="other",
            characters_dir=Path(tmp) / "characters",
            memory_dir=Path(tmp) / "memory", theme="light",
        )
        assert card._name_label.text() == "other"
        card.set_agent("t5", "t5")
        assert card._name_label.text() == "测试喵"
        assert card._stat_boxes["events"][0].text() == "2"


# ── P1-8 HUD：取色正确 ─────────────────────────────────

def test_neko_palette_hud_tokens_exist():
    """palette 双主题都含 HUD 所需全部 token。"""
    for theme in ("light", "dark"):
        p = NEKO_PALETTE[theme]
        for key in ("hud_panel_bg", "hud_panel_border", "hud_title", "hud_text",
                    "hud_text_secondary", "hud_track", "hud_bar_good",
                    "hud_bar_warn", "hud_bar_bad", "hud_dot_running",
                    "hud_dot_needyou", "hud_dot_active", "hud_dot_idle",
                    "hud_glow_text", "hud_border_glow"):
            assert key in p, f"{theme} 缺 token {key}"
        for emo in ("happy", "sad", "thinking", "surprised", "angry", "neutral"):
            assert f"hud_emotion_{emo}" in p, f"{theme} 缺情绪 token {emo}"
    assert NEKO_LAYOUT["hud_radius"] == 16
    assert NEKO_LAYOUT["amadeus_radius"] == 9


def test_status_hud_bar_color_from_palette():
    """数值条颜色来自 palette token，而非硬编码。"""
    assert _bar_color(0.8, True) == neko_qcolor("dark", "hud_bar_good")
    assert _bar_color(0.5, True) == neko_qcolor("dark", "hud_bar_warn")
    assert _bar_color(0.1, True) == neko_qcolor("dark", "hud_bar_bad")
    assert _bar_color(0.8, False) == neko_qcolor("light", "hud_bar_good")
    assert _bar_color(0.5, False) == neko_qcolor("light", "hud_bar_warn")
    assert _bar_color(0.1, False) == neko_qcolor("light", "hud_bar_bad")


def test_amadeus_hud_colors_from_palette():
    hud = AmadeusHUD()
    for theme in ("light", "dark"):
        cols = hud._colors(theme)
        assert cols["dot_running"] == neko_qcolor(theme, "hud_dot_running")
        assert cols["dot_needyou"] == neko_qcolor(theme, "hud_dot_needyou")
        assert cols["dot_active"] == neko_qcolor(theme, "hud_dot_active")
        assert cols["dot_idle"] == neko_qcolor(theme, "hud_dot_idle")
        assert cols["text_bright"] == neko_qcolor(theme, "hud_glow_text")
        assert cols["bg"] == neko_qcolor(theme, "hud_panel_bg")


def test_hud_modules_use_palette_not_hardcoded():
    """HUD 源码不残留 QColor(r, g, b) 硬编码字面量。"""
    import ui.status_hud as sh
    import ui.amadeus_hud as ah
    for mod in (sh, ah):
        src = Path(mod.__file__).read_text(encoding="utf-8")
        assert "neko_qcolor" in src, f"{mod.__name__} 未引用 palette 取色"
        assert not _HARDCODED_QCOLOR_RE.search(src), (
            f"{mod.__name__} 残留硬编码 QColor 字面量"
        )


# ── P1-8 HUD：无回归（功能不变） ────────────────────────

def test_status_hud_functionality_unchanged():
    class FakeSave:
        hunger = 80
        thirst = 50
        mood = 70
        mood_max = 100
        energy = 40
        health = 90
        health_max = 100

    hud = StatusHUD()
    hud.set_stats(FakeSave())
    hud.set_emotion("happy")
    assert hud._stats["hunger"] == (80.0, 100.0)
    assert hud._emotion == "happy"
    # 尺寸与旧实现一致（不回归）
    assert hud.width() == 188
    assert hud.height() == 172
    # 渲染不崩溃
    pix = hud.grab()
    assert not pix.isNull()


def test_amadeus_hud_functionality_unchanged():
    hud = AmadeusHUD()
    hud.set_counts(1, 2, 3)
    assert (hud._running, hud._need_you, hud._active) == (1, 2, 3)
    # 呼吸脉冲 tick 不崩溃
    hud._tick_pulse()
    # 渲染不崩溃
    pix = hud.grab()
    assert not pix.isNull()
    # 主题切换不崩溃
    hud._on_theme("light")
    assert hud.theme == "light"
    hud._on_theme("dark")
    assert hud.theme == "dark"
