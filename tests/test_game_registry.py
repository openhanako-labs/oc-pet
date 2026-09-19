# -*- coding: utf-8 -*-
"""游戏白名单与识别（陪玩 P0-1）单元测试。

纯逻辑，不碰 Windows API。
"""
from __future__ import annotations

import pytest

from core.game.registry import (
    DEFAULT_GAMES,
    TYPE_GACHA_OPENWORLD,
    GameEntry,
    generic_game,
    load_games,
    match_game,
)


# ── 内置表 ────────────────────────────────────────────────


def test_default_games_at_least_three():
    """PRD P0-1 验收：首发至少 3 款。"""
    assert len(DEFAULT_GAMES) >= 3


def test_default_games_have_matchers():
    """每条都得有判据，否则是死条目。"""
    for g in DEFAULT_GAMES:
        assert g.id and g.name
        assert g.process or g.title, f"{g.id} 没有任何匹配判据"


def test_default_games_ids_unique():
    ids = [g.id for g in DEFAULT_GAMES]
    assert len(ids) == len(set(ids))


def test_default_games_cover_three_scenario_types():
    """PRD §1.2 的三类 P0 场景都应有代表。"""
    types = {g.type for g in DEFAULT_GAMES}
    assert TYPE_GACHA_OPENWORLD in types
    assert "moba" in types
    assert "steam_single" in types


# ── 匹配 ──────────────────────────────────────────────────


def test_match_by_process_exact():
    g = match_game("StarRail.exe", "", DEFAULT_GAMES)
    assert g is not None and g.id == "star_rail"


def test_match_is_case_insensitive():
    g = match_game("STARRAIL.EXE", "", DEFAULT_GAMES)
    assert g is not None and g.id == "star_rail"


def test_match_by_wildcard_process():
    g = match_game("Wuthering Waves.exe", "", DEFAULT_GAMES)
    assert g is not None and g.id == "wuthering_waves"


def test_match_by_title_when_process_is_useless():
    """原版 Minecraft 的进程名没信息量，靠标题。"""
    g = match_game("javaw.exe", "Minecraft 1.21 - 单人游戏", DEFAULT_GAMES)
    assert g is not None and g.id == "minecraft"


def test_match_chinese_title():
    g = match_game("some.exe", "鸣潮", DEFAULT_GAMES)
    assert g is not None and g.id == "wuthering_waves"


def test_process_takes_precedence_over_title():
    """进程判据比标题硬：标题撞上别的条目也不能被抢走。"""
    mine = GameEntry(id="mine", name="M", process=["mine.exe"], title=["通用"])
    other = GameEntry(id="other", name="O", process=["other.exe"], title=["通用"])
    g = match_game("mine.exe", "通用 - 窗口", [mine, other])
    assert g is not None and g.id == "mine"


def test_match_unknown_returns_none():
    assert match_game("explorer.exe", "文件资源管理器", DEFAULT_GAMES) is None


def test_match_empty_inputs():
    assert match_game("", "", DEFAULT_GAMES) is None


# ── 通用兜底 ──────────────────────────────────────────────


def test_generic_game_uses_title():
    g = generic_game("鸣潮 - Steam", "")
    assert g.generic is True
    assert g.name == "鸣潮"


def test_generic_game_strips_platform_suffix():
    g = generic_game("Some Game - Steam", "some.exe")
    assert g.name == "Some Game"


def test_generic_game_falls_back_to_process_then_placeholders():
    assert generic_game("", "foo.exe").name == "foo.exe"
    assert generic_game("", "").name == "未知游戏"


def test_generic_game_truncates_long_title():
    g = generic_game("标" * 100, "")
    assert len(g.name) <= 40


# ── 配置合并 ──────────────────────────────────────────────


def test_load_games_none_gives_defaults():
    games = load_games(None)
    assert len(games) == len(DEFAULT_GAMES)


def test_load_games_override_same_id_keeps_position():
    before = load_games(None)
    pos = [g.id for g in before].index("terraria")
    after = load_games({"games": [{"id": "terraria", "name": "泰拉瑞亚（改）",
                                   "process": ["terraria.exe"]}]})
    assert len(after) == len(before)          # 不是追加
    assert after[pos].id == "terraria"
    assert after[pos].name == "泰拉瑞亚（改）"


def test_load_games_appends_new_id():
    after = load_games({"games": [{"id": "my_game", "name": "我的冷门游戏",
                                   "process": ["my.exe"]}]})
    assert after[-1].id == "my_game"
    assert len(after) == len(DEFAULT_GAMES) + 1


def test_load_games_ignores_entry_without_id():
    after = load_games({"games": [{"name": "没 id"}]})
    assert len(after) == len(DEFAULT_GAMES)


def test_load_games_accepts_string_process_and_title():
    after = load_games({"games": [{"id": "s", "process": "s.exe", "title": "代号S"}]})
    last = after[-1]
    assert last.process == ["s.exe"] and last.title == ["代号S"]


def test_load_games_disabled_removes_entries():
    after = load_games({"disabled": ["lol", "dota2"]})
    ids = [g.id for g in after]
    assert "lol" not in ids and "dota2" not in ids


def test_load_games_disabled_accepts_single_string():
    after = load_games({"disabled": "lol"})
    assert "lol" not in [g.id for g in after]


def test_load_games_does_not_mutate_defaults():
    """合并必须深拷；否则配置一改，内置表就被污染。"""
    load_games({"games": [{"id": "terraria", "name": "改了", "process": ["x.exe"]}]})
    g = next(g for g in DEFAULT_GAMES if g.id == "terraria")
    assert g.name == "泰拉瑞亚"
    assert "x.exe" not in g.process
