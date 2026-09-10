# -*- coding: utf-8 -*-
"""P2 互动层测试 — 小游戏 / 音乐推荐 / 休息提醒 / 卡片 / 接线。

覆盖 P2-3/P2-4/P2-5 验收：
  - 小游戏纯逻辑（猜数字 win/lose/invalid、石头剪刀布计分、快速反应抢跑/计时）
  - 音乐推荐（场景/情绪/时段匹配、排除最近、播放目标解析）
  - 休息提醒状态机（累积/衰减/阈值/深夜降频/确认重置/稍后提醒）
  - 交互意图检测（游戏/音乐/休息关键词）
  - 卡片构建（game/music/rest）+ ChatPanel 卡片支持（离屏）
  - PlayMixin 接线（聊天分发、卡片动作路由）

运行: python -m pytest tests/test_p2_interaction_layer.py -v
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from core.play.break_reminder import (
    BREAK_SUGGESTIONS,
    DEFAULT_WORK_REMINDER_CONFIG,
    WorkReminderTracker,
    is_late_night,
    load_work_reminder_config,
)
from core.play.games import (
    GAME_CATALOG,
    GuessNumberGame,
    ReactionGame,
    RockPaperScissorsGame,
    create_game,
)
from core.play.interaction_dispatcher import (
    InteractionIntentDetector,
    build_game_invite_card,
    build_music_card,
    build_rest_card,
)
from core.play.music_recommender import (
    MusicRecommender,
    MusicTrack,
    resolve_play_target,
)


# ── P2-3 小游戏纯逻辑 ────────────────────────────────────

def test_guess_number_win():
    g = GuessNumberGame(seed=1)
    target = g.target
    out = g.guess(target)
    assert out["status"] == "win"
    assert out["hint"] == "correct"
    assert g.finished is True
    result = g.result()
    assert result.won is True
    assert "猜中" in result.detail


def test_guess_number_lose_when_attempts_exhausted():
    g = GuessNumberGame(seed=1, max_attempts=3)
    # 一直猜 0（范围外无效不计数），改用与 target 不同的数
    for _ in range(3):
        g.guess(g.target + 1 if g.target < g.high else g.target - 1)
    assert g.finished is True
    assert g.result().won is False


def test_guess_number_invalid_input():
    g = GuessNumberGame(seed=1)
    assert g.guess(0)["status"] == "invalid"       # 低于范围
    assert g.guess(101)["status"] == "invalid"     # 高于范围
    assert g.guess("abc")["status"] == "invalid"   # 非数字
    assert g.finished is False


def test_guess_number_reset():
    g = GuessNumberGame(seed=1)
    old = g.target
    g.reset()
    assert g.finished is False
    assert g.attempts == 0


def test_rps_score_and_finish():
    g = RockPaperScissorsGame(seed=2, best_of=3)
    assert g.wins_needed == 2
    # 玩家一直出石头，直到有一方先赢 2 局
    while not g.finished:
        out = g.play("rock")
    assert out["finished"] is True
    total = g.player_score + g.pet_score
    assert total >= 2  # 至少一方到 2 分
    result = g.result()
    assert result.game == "rps"
    assert result.won == (g.player_score >= 2)


def test_rps_draw_and_invalid():
    g = RockPaperScissorsGame(seed=3)
    out = g.play("bomb")
    assert out["result"] == "invalid"
    out = g.play("rock")
    assert out["result"] in ("win", "lose", "draw")
    assert out["pet_emoji"] in ("✊", "✋", "✌️")


def test_reaction_ok_and_too_soon():
    g = ReactionGame()
    g.arm(1000.0, delay_sec=1.0)
    # 抢跑（信号还没出现）
    out = g.record(1000.3)
    assert out["status"] == "too_soon"
    assert out["finished"] is True
    assert g.result().won is False

    g.reset()
    g.arm(2000.0, delay_sec=1.0)  # go_at = 2001.0
    out = g.record(2001.25)
    assert out["status"] == "ok"
    assert 240 <= out["ms"] <= 260  # 250ms
    assert g.result().won is True


def test_create_game_factory():
    assert isinstance(create_game("guess_number"), GuessNumberGame)
    assert isinstance(create_game("rps"), RockPaperScissorsGame)
    assert isinstance(create_game("reaction"), ReactionGame)
    assert create_game("unknown") is None
    assert set(GAME_CATALOG.keys()) == {"guess_number", "rps", "reaction"}


# ── P2-4 音乐推荐 ────────────────────────────────────────

def test_music_recommend_scene_match():
    rec = MusicRecommender(seed=1)
    track = rec.recommend(scene="deep_work", emotion="working", hour=14)
    assert track is not None
    # 工作场景应命中工作/专注标签曲目
    assert track.tags & {"工作", "专注"}


def test_music_recommend_emotion_and_hour():
    rec = MusicRecommender(seed=2)
    track = rec.recommend(scene="", emotion="sad", hour=23)
    assert track is not None
    assert track.tags & {"治愈", "安静", "深夜"}


def test_music_recommend_exclude():
    rec = MusicRecommender(seed=3)
    t1 = rec.recommend(scene="deep_work", hour=14)
    t2 = rec.recommend(scene="deep_work", hour=14, exclude_ids=[t1.id])
    assert t2 is not None
    assert t2.id != t1.id


def test_music_get_and_card():
    rec = MusicRecommender()
    track = rec.get("rain_piano")
    assert track is not None and track.id == "rain_piano"
    card = build_music_card(track)
    assert card["kind"] == "music"
    assert "雨声钢琴曲" in card["title"]
    assert card["actions"][0][0].startswith("▶")
    assert card["actions"][0][1] == "music_play:rain_piano"


def test_music_resolve_play_target_fallback_folder(tmp_path):
    track = MusicTrack(id="x", title="测试", tags=set())
    kind, target = resolve_play_target(track, music_dir=tmp_path)
    assert kind == "folder"
    assert str(tmp_path) in str(target)


def test_music_resolve_url_and_file(tmp_path):
    track = MusicTrack(id="u", title="URL", play_target="https://example.com/a.mp3")
    kind, target = resolve_play_target(track, music_dir=tmp_path)
    assert kind == "url"
    assert target.startswith("http")

    local = tmp_path / "song.mp3"
    local.write_bytes(b"fake")
    track2 = MusicTrack(id="f", title="File", play_target=str(local))
    kind2, target2 = resolve_play_target(track2, music_dir=tmp_path)
    assert kind2 == "file"
    assert target2 == str(local)


# ── P2-5 休息提醒状态机 ─────────────────────────────────

def _fixed_config(**overrides):
    cfg = dict(DEFAULT_WORK_REMINDER_CONFIG)
    cfg.update(overrides)
    return cfg


def test_break_reminder_accumulate_and_due():
    tr = WorkReminderTracker(_fixed_config(after_minutes=1.0, cooldown_minutes=0))
    t0 = 1700000000.0
    tr.update(True, t0)
    tr.update(True, t0 + 30)
    assert tr.should_remind(t0 + 30)["due"] is False  # 30s < 60s
    tr.update(True, t0 + 70)
    assert tr.should_remind(t0 + 70)["due"] is True
    assert tr.accumulated() >= 60.0


def test_break_reminder_decay_when_not_working():
    tr = WorkReminderTracker(_fixed_config(after_minutes=1.0, cooldown_minutes=0))
    t0 = 1700000000.0
    tr.update(True, t0)
    tr.update(True, t0 + 70)   # 累计 70s ≥ 60s → due
    assert tr.should_remind(t0 + 70)["due"] is True
    tr.update(False, t0 + 100)  # 停止工作 30s → 累计回到 40s
    assert tr.should_remind(t0 + 100)["due"] is False


def test_break_reminder_acknowledge_resets_and_cooldown():
    tr = WorkReminderTracker(_fixed_config(after_minutes=1.0, cooldown_minutes=30))
    t0 = 1700000000.0
    tr.update(True, t0)
    tr.update(True, t0 + 70)
    assert tr.should_remind(t0 + 70)["due"] is True
    tr.acknowledge(t0 + 71)
    assert tr.should_remind(t0 + 71)["due"] is False
    assert tr.accumulated() == 0.0
    # 冷却期内即使再工作也不提醒
    tr.update(True, t0 + 100)
    assert tr.should_remind(t0 + 100)["due"] is False
    # 冷却结束后重新累计才提醒
    tr.update(True, t0 + 2000)
    assert tr.should_remind(t0 + 2000)["due"] is True


def test_break_reminder_snooze_keeps_accumulated():
    tr = WorkReminderTracker(_fixed_config(after_minutes=1.0, cooldown_minutes=0))
    t0 = 1700000000.0
    tr.update(True, t0)
    tr.update(True, t0 + 70)
    assert tr.should_remind(t0 + 70)["due"] is True
    tr.snooze(minutes=10, now=t0 + 71)
    assert tr.accumulated() > 0.0
    assert tr.should_remind(t0 + 71)["due"] is False  # 进入冷却


def test_break_reminder_late_night_multiplier():
    tr = WorkReminderTracker(_fixed_config(
        after_minutes=1.0, late_night_hour=22, late_night_end_hour=6,
        late_night_multiplier=3.0, cooldown_minutes=0,
    ))
    t_late = 1700000000.0 + 23 * 3600  # 23:00 深夜
    assert is_late_night(t_late) is True
    tr.update(True, t_late)
    tr.update(True, t_late + 70)        # 累计 70s
    assert tr.should_remind(t_late + 70)["due"] is False  # 阈值 180s
    tr.update(True, t_late + 200)
    assert tr.should_remind(t_late + 200)["due"] is True  # 累计 200s ≥ 180s


def test_break_reminder_daytime_not_late_night():
    t_day = 1700000000.0 + 9 * 3600  # 9:00
    assert is_late_night(t_day) is False


def test_break_reminder_disabled_zero_behavior():
    tr = WorkReminderTracker(_fixed_config(enabled=False, after_minutes=1.0))
    t0 = 1700000000.0
    tr.update(True, t0)
    tr.update(True, t0 + 500)
    assert tr.should_remind(t0 + 500)["due"] is False
    assert tr.accumulated() == 0.0


def test_break_suggestion_rotation():
    tr = WorkReminderTracker(_fixed_config())
    s1 = tr.pick_suggestion()
    s2 = tr.pick_suggestion()
    assert s1.id != s2.id
    assert len(BREAK_SUGGESTIONS) >= 4


def test_build_rest_card():
    tr = WorkReminderTracker(_fixed_config(after_minutes=1.5))
    s = tr.pick_suggestion()
    card = build_rest_card(s, threshold_min=90, late_night=True)
    assert card["kind"] == "rest"
    assert "90" in card["body"]
    assert card["actions"][0][1] == "rest_ack"
    assert card["actions"][1][1] == "rest_snooze"


def test_load_work_reminder_config_defaults():
    cfg = load_work_reminder_config()
    assert cfg["after_minutes"] == 90
    assert cfg["enabled"] is True


# ── 交互意图检测 ─────────────────────────────────────────

def test_detector_game_music_rest():
    det = InteractionIntentDetector()
    assert det.detect("陪我玩个游戏吧").kind == "game"
    assert det.detect("来一局石头剪刀布").kind == "game"
    assert det.detect("推荐首歌呗").kind == "music"
    assert det.detect("放点音乐").kind == "music"
    assert det.detect("我想休息一下").kind == "rest"
    assert det.detect("今天天气不错") is None
    assert det.detect("") is None
    assert det.detect("   ") is None


def test_build_game_invite_card():
    card = build_game_invite_card()
    assert card["kind"] == "game"
    assert len(card["actions"]) == 3
    assert card["actions"][0][1] == "open_game:guess_number"


# ── ChatPanel 卡片支持（离屏）────────────────────────────

def test_chat_panel_add_card_and_action():
    from PySide6.QtWidgets import QApplication
    from ui.chat_panel import ChatPanel
    app = QApplication.instance() or QApplication([])

    panel = ChatPanel(theme="light")
    seen = []
    panel.card_action.connect(lambda k, a: seen.append((k, a)))
    card = panel.add_card("game", "🎮 小游戏", "来玩吗？",
                          [("开始", "open_game:rps")])
    assert panel.card_count == 1
    assert card.kind == "game"
    # 触发按钮点击 → card_action 信号
    card._action_buttons[0].click()
    assert seen == [("game", "open_game:rps")]
    # 主题切换同步卡片
    panel.set_theme("dark")
    assert card.theme == "dark"
    # 关闭卡片移除
    card.dismiss_requested.emit("game")
    assert panel.card_count == 0
    panel.clear()
    assert panel.card_count == 0


def test_interaction_card_standalone_theme():
    from PySide6.QtWidgets import QApplication
    from ui.interaction_card import InteractionCard
    app = QApplication.instance() or QApplication([])
    card = InteractionCard(kind="music", title="🎵 测试", body="理由",
                           actions=[("播放", "music_play:x")])
    assert card.property("data-theme") == "light"
    card.set_theme("dark")
    assert card.property("data-theme") == "dark"
    assert card.title_text == "🎵 测试"


# ── 小游戏窗口（离屏构造）────────────────────────────────

def test_mini_game_windows_constructible():
    from PySide6.QtWidgets import QApplication
    from ui.mini_game_window import (
        GuessNumberWindow,
        ReactionWindow,
        RockPaperScissorsWindow,
        create_game_window,
    )
    app = QApplication.instance() or QApplication([])
    assert GuessNumberWindow(theme="light").game_id == "guess_number"
    assert RockPaperScissorsWindow(theme="light").game_id == "rps"
    assert ReactionWindow(theme="light").game_id == "reaction"
    assert create_game_window("rps").game_id == "rps"
    assert create_game_window("unknown") is None


def test_guess_number_window_bad_input_no_crash():
    from PySide6.QtWidgets import QApplication
    from ui.mini_game_window import GuessNumberWindow
    app = QApplication.instance() or QApplication([])
    w = GuessNumberWindow(theme="light")
    w._input.setText("abc")
    w._on_guess()
    assert "数字" in w._result_label.text()


# ── PlayMixin 接线（stub pet）────────────────────────────

from pet_mixins.play_mixin import PlayMixin


class _FakePlayPet(PlayMixin):
    """最小 stub：继承 PlayMixin（拿到 _offer_* 等真实方法），
    只覆写 PetWindow 提供的鸭子类型外部依赖。"""

    def __init__(self):
        self._interaction_detector = InteractionIntentDetector()
        self._music_recommender = MusicRecommender(seed=7)
        self._work_reminder = WorkReminderTracker(
            _fixed_config(after_minutes=1.0, cooldown_minutes=0)
        )
        self._music_exclude = []
        self._music_dir = None
        self._interaction_card = None
        self._mini_game_windows = {}
        self._foreground_watcher = None
        self._focus_manager = None
        self._companion_memory = None
        self._agent_id = "test"
        self._current_char = "test"
        self._current_emotion = "neutral"
        self._engine = None
        self._chat_panel = None
        self.calls = []

    def _show_bubble(self, text, emotion="neutral", priority=0):
        self.calls.append(("bubble", text, emotion))

    def _show_interaction_card(self, card_data):
        self.calls.append(("card", card_data.get("kind")))

    def _hide_interaction_card(self):
        self.calls.append(("hide_card",))

    def _record_interaction_event(self, **kw):
        self.calls.append(("event", kw.get("intent", "")))

    def _try_speak_reminder(self, text):
        self.calls.append(("tts", text))

    def _open_mini_game(self, kind):
        self.calls.append(("open_game", kind))

    def _play_music(self, track_id):
        self.calls.append(("play_music", track_id))

    def _open_music_folder_action(self):
        self.calls.append(("open_folder",))

    def _acknowledge_rest(self):
        self.calls.append(("ack_rest",))

    def _snooze_rest(self):
        self.calls.append(("snooze_rest",))

    def _is_working_context(self):
        return True


def test_play_mixin_dispatch_game():
    from pet_mixins.play_mixin import PlayMixin
    pet = _FakePlayPet()
    PlayMixin._dispatch_chat_interaction(pet, "陪我玩个游戏")
    kinds = [c[0] for c in pet.calls]
    assert "card" in kinds
    assert "bubble" in kinds
    assert "event" in kinds


def test_play_mixin_dispatch_music():
    from pet_mixins.play_mixin import PlayMixin
    pet = _FakePlayPet()
    PlayMixin._dispatch_chat_interaction(pet, "推荐首歌")
    assert any(c[0] == "card" and c[1] == "music" for c in pet.calls)
    assert any(c[0] == "bubble" for c in pet.calls)


def test_play_mixin_dispatch_rest():
    from pet_mixins.play_mixin import PlayMixin
    pet = _FakePlayPet()
    PlayMixin._dispatch_chat_interaction(pet, "我想休息一下")
    assert any(c[0] == "card" and c[1] == "rest" for c in pet.calls)


def test_play_mixin_no_dispatch_unrelated():
    from pet_mixins.play_mixin import PlayMixin
    pet = _FakePlayPet()
    PlayMixin._dispatch_chat_interaction(pet, "今天天气不错")
    assert pet.calls == []


def test_play_mixin_card_action_routing():
    from pet_mixins.play_mixin import PlayMixin
    pet = _FakePlayPet()
    PlayMixin._on_interaction_card_action(pet, "game", "open_game:rps")
    assert ("open_game", "rps") in pet.calls
    assert ("hide_card",) in pet.calls

    pet2 = _FakePlayPet()
    PlayMixin._on_interaction_card_action(pet2, "music", "music_play:rain_piano")
    assert ("play_music", "rain_piano") in pet2.calls

    pet3 = _FakePlayPet()
    PlayMixin._on_interaction_card_action(pet3, "rest", "rest_ack")
    assert ("ack_rest",) in pet3.calls


def test_play_mixin_work_reminder_tick_due():
    from pet_mixins.play_mixin import PlayMixin
    pet = _FakePlayPet()
    t0 = 1700000000.0
    pet._work_reminder.update(True, t0)
    pet._work_reminder.update(True, t0 + 70)  # 累计 70s ≥ 60s
    # 直接调用 tick（内部用真实 now，因累计已达标且冷却 0 → due）
    PlayMixin._work_reminder_tick(pet)
    assert any(c[0] == "card" and c[1] == "rest" for c in pet.calls)
    # mark_reminded 后不再重复弹
    assert pet._work_reminder.is_reminded() is True
