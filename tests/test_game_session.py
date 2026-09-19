# -*- coding: utf-8 -*-
"""游戏会话状态机（陪玩 P0-1）单元测试。

探针可注入，所以不依赖真实 Windows 窗口。
"""
from __future__ import annotations

from core.game.registry import GameEntry
from core.game.session import (
    EV_BACKGROUND,
    EV_EXITED,
    EV_FOREGROUND,
    EV_STARTED,
    GameSessionWatcher,
)


def _miku_game() -> GameEntry:
    return GameEntry(id="wuthering_waves", name="鸣潮", process=["wuthering*"])


class _Clock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _watcher(running=True, clock=None, games=None, enabled=True):
    """running 可为 True/False/None（None = 探不到）。"""
    return GameSessionWatcher(
        games=games if games is not None else [_miku_game()],
        is_running=lambda entry: running,
        enabled=enabled,
        now=clock or _Clock(),
    )


# ── 启动 ──────────────────────────────────────────────────


def test_started_on_first_match():
    w = _watcher()
    ev = w.on_foreground("Wuthering Waves.exe", "鸣潮")
    assert [e["type"] for e in ev] == [EV_STARTED]
    assert ev[0]["game"]["name"] == "鸣潮"
    assert w.current is not None and w.current.id == "wuthering_waves"
    assert w.in_foreground is True


def test_no_event_when_already_in_same_game():
    w = _watcher()
    w.on_foreground("Wuthering Waves.exe", "鸣潮")
    assert w.on_foreground("Wuthering Waves.exe", "鸣潮") == []


def test_disabled_watcher_emits_nothing():
    w = _watcher(enabled=False)
    assert w.on_foreground("Wuthering Waves.exe", "鸣潮") == []
    assert w.current is None


def test_non_game_foreground_does_nothing_without_session():
    w = _watcher()
    assert w.on_foreground("chrome.exe", "新标签页", "browsing") == []
    assert w.current is None


# ── 切走 ≠ 退出（核心设计）────────────────────────────────


def test_background_when_left_but_still_running():
    w = _watcher(running=True)
    w.on_foreground("Wuthering Waves.exe", "鸣潮")
    ev = w.on_foreground("chrome.exe", "攻略 - B站", "browsing")
    assert [e["type"] for e in ev] == [EV_BACKGROUND]
    assert w.current is not None          # 会话还在
    assert w.in_foreground is False


def test_foreground_when_returning():
    w = _watcher(running=True)
    w.on_foreground("Wuthering Waves.exe", "鸣潮")
    w.on_foreground("chrome.exe", "攻略", "browsing")
    ev = w.on_foreground("Wuthering Waves.exe", "鸣潮")
    assert [e["type"] for e in ev] == [EV_FOREGROUND]
    assert w.in_foreground is True


def test_exited_when_process_gone_on_leave():
    w = _watcher(running=False)
    w.on_foreground("Wuthering Waves.exe", "鸣潮")
    ev = w.on_foreground("chrome.exe", "新标签页", "browsing")
    assert [e["type"] for e in ev] == [EV_EXITED]
    assert w.current is None


def test_unknown_probe_keeps_session_alive():
    """探不到就保守续命——宁可多留，也别误报退出触发错话。"""
    w = _watcher(running=None)
    w.on_foreground("Wuthering Waves.exe", "鸣潮")
    ev = w.on_foreground("chrome.exe", "看攻略", "browsing")
    assert [e["type"] for e in ev] == [EV_BACKGROUND]
    assert w.current is not None


def test_repeated_leave_emits_background_only_once():
    w = _watcher(running=True)
    w.on_foreground("Wuthering Waves.exe", "鸣潮")
    assert len(w.on_foreground("chrome.exe", "A", "browsing")) == 1
    assert w.on_foreground("code.exe", "B", "development") == []


# ── poll（不在前台时的退出检测）────────────────────────────


def test_poll_detects_exit_while_background():
    w = _watcher(running=True)
    w.on_foreground("Wuthering Waves.exe", "鸣潮")
    w.on_foreground("chrome.exe", "攻略", "browsing")

    w._is_running = lambda entry: False       # 游戏这时关了
    ev = w.poll()
    assert [e["type"] for e in ev] == [EV_EXITED]
    assert w.current is None


def test_poll_noop_when_in_foreground():
    w = _watcher(running=False)
    w.on_foreground("Wuthering Waves.exe", "鸣潮")   # is_running=False 也不影响启动
    assert w.poll() == []


def test_poll_noop_without_session():
    assert _watcher().poll() == []


# ── 通用兜底（白名单外的游戏）──────────────────────────────


def test_generic_entry_from_gaming_category():
    w = _watcher()
    ev = w.on_foreground("unknown_game.exe", "某独立游戏 - Steam", "gaming")
    assert [e["type"] for e in ev] == [EV_STARTED]
    assert ev[0]["game"]["generic"] is True
    assert ev[0]["game"]["name"] == "某独立游戏"


def test_generic_entry_ends_on_leaving_foreground():
    """通用条目没法探进程，文档约定：离开前台即结束。"""
    w = _watcher()
    w.on_foreground("unknown_game.exe", "某独立游戏", "gaming")
    ev = w.on_foreground("chrome.exe", "网页", "browsing")
    assert [e["type"] for e in ev] == [EV_EXITED]


def test_generic_not_used_when_category_is_not_gaming():
    w = _watcher()
    assert w.on_foreground("foo.exe", "某个视频", "entertainment") == []


# ── 换游戏 ────────────────────────────────────────────────


def test_switch_game_emits_exit_then_start_when_old_gone():
    games = [_miku_game(), GameEntry(id="terraria", name="泰拉瑞亚", process=["terraria.exe"])]
    w = GameSessionWatcher(games=games, is_running=lambda e: False, now=_Clock())
    w.on_foreground("Wuthering Waves.exe", "鸣潮")
    ev = w.on_foreground("terraria.exe", "Terraria")
    assert [e["type"] for e in ev] == [EV_EXITED, EV_STARTED]
    assert w.current.id == "terraria"


def test_switch_game_keeps_old_when_still_running():
    games = [_miku_game(), GameEntry(id="terraria", name="泰拉瑞亚", process=["terraria.exe"])]
    w = GameSessionWatcher(games=games, is_running=lambda e: True, now=_Clock())
    w.on_foreground("Wuthering Waves.exe", "鸣潮")
    ev = w.on_foreground("terraria.exe", "Terraria")
    assert [e["type"] for e in ev] == [EV_STARTED]


# ── 会话信息 ──────────────────────────────────────────────


def test_duration_tracks_clock():
    clock = _Clock(1000.0)
    w = _watcher(clock=clock)
    w.on_foreground("Wuthering Waves.exe", "鸣潮")
    clock.advance(90.0)
    assert w.duration == 90.0
    snap = w.session()
    assert snap["duration"] == 90.0
    assert snap["in_foreground"] is True
    assert snap["game"]["name"] == "鸣潮"


def test_session_none_without_game():
    assert _watcher().session() is None


def test_exit_event_carries_total_duration():
    clock = _Clock(1000.0)
    w = _watcher(running=False, clock=clock)
    w.on_foreground("Wuthering Waves.exe", "鸣潮")
    clock.advance(3600.0)
    ev = w.on_foreground("chrome.exe", "网页", "browsing")
    assert ev[0]["type"] == EV_EXITED
    assert ev[0]["duration"] == 3600.0


def test_set_games_hot_swap():
    w = _watcher(games=[_miku_game()])
    w.set_games([GameEntry(id="other", name="别的", process=["other.exe"])])
    ev = w.on_foreground("other.exe", "")
    assert ev and ev[0]["game"]["id"] == "other"


# ── EventBus 转发 ─────────────────────────────────────────


def test_emit_session_events_publishes_to_bus():
    from core.event_bus import EventBus
    from core.game.session import EVENT_BUS_NAME, emit_session_events

    got = []

    def _handler(payload=None):
        got.append(payload)

    EventBus.on(EVENT_BUS_NAME, _handler)
    try:
        n = emit_session_events([{"type": EV_STARTED, "game": {"name": "鸣潮"}}])
    finally:
        EventBus.off(EVENT_BUS_NAME, _handler)
    assert n == 1
    assert got and got[0]["type"] == EV_STARTED


def test_emit_session_events_empty_is_noop():
    from core.game.session import emit_session_events

    assert emit_session_events([]) == 0
    assert emit_session_events(None) == 0


def test_full_flow_publishes_started_then_exited():
    """端到端：接上总线跑一次「开游戏 → 切走 → 关游戏」应看到三个事件。"""
    from core.event_bus import EventBus
    from core.game.session import EVENT_BUS_NAME, emit_session_events

    seen = []

    def _handler(payload=None):
        seen.append(payload["type"])

    clock = _Clock()
    w = _watcher(running=True, clock=clock)
    EventBus.on(EVENT_BUS_NAME, _handler)
    try:
        emit_session_events(w.on_foreground("Wuthering Waves.exe", "鸣潮"))
        emit_session_events(w.on_foreground("chrome.exe", "攻略", "browsing"))
        w._is_running = lambda entry: False
        emit_session_events(w.poll())
    finally:
        EventBus.off(EVENT_BUS_NAME, _handler)

    assert seen == [EV_STARTED, EV_BACKGROUND, EV_EXITED]
    assert w.current is None
