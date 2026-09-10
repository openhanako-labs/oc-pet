"""T04 P0-5 专注模式 — FocusScorer 三信号 + hysteresis 状态机单测。

无 GUI 依赖（纯 Python），pytest 直接运行：
    python -m pytest tests/test_focus.py -v
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.perception.focus import (
    DEFAULT_FOCUS_SETTINGS,
    EmotionReading,
    FocusScore,
    FocusScorer,
    FocusStateMachine,
    emotion_reading_from_state,
    load_focus_settings,
    scan_vulnerability_keywords,
)


def make_settings(**overrides):
    """默认配置 + 覆盖项（默认 enabled=False，测试里显式开启）。"""
    s = dict(DEFAULT_FOCUS_SETTINGS)
    s["signal_weights"] = dict(DEFAULT_FOCUS_SETTINGS["signal_weights"])
    s.update(overrides)
    return s


class FakeEmotion:
    """duck-typed 情绪读数：valence/arousal/complexity。"""

    def __init__(self, valence=0.0, arousal=0.0, complexity=None):
        self.valence = valence
        self.arousal = arousal
        self.complexity = complexity


# ── 关键词信号 ──

def test_scan_vulnerability_keywords_hits():
    assert scan_vulnerability_keywords("我好累好烦") >= 2
    assert scan_vulnerability_keywords("最近压力很大，快崩溃了") >= 2
    assert scan_vulnerability_keywords("I am so tired and stressed") >= 2


def test_scan_vulnerability_keywords_miss():
    assert scan_vulnerability_keywords("今天天气不错") == 0
    assert scan_vulnerability_keywords("") == 0


def test_keyword_signal_saturates():
    scorer = FocusScorer(make_settings(keyword_saturation=2))
    result = scorer.score(user_text="累 累 累 累")
    assert result.signals["keyword"] is not None
    assert result.signals["keyword"] == 1.0  # 饱和到 1.0
    assert result.score > 0.0


def test_keyword_absent_returns_none():
    scorer = FocusScorer(make_settings())
    result = scorer.score(user_text="今天阳光真好")
    assert result.signals["keyword"] is None


# ── cadence 信号 ──

def test_cadence_signal_after_baseline():
    scorer = FocusScorer(make_settings(cadence_min_samples=3))
    # 先积累 3 条长消息作为基线
    for _ in range(3):
        scorer.score(user_text="这是一条比较长的用户消息用来建立基线")
    result = scorer.score(user_text="累")  # 骤降 + 关键词在场
    assert result.signals["cadence"] is not None
    assert result.signals["cadence"] > 0.5


def test_cadence_gated_without_distress():
    scorer = FocusScorer(make_settings(cadence_min_samples=1))
    scorer.score(user_text="这是一条很长的普通消息")
    result = scorer.score(user_text="嗯")  # 短但无脆弱性证据 → cadence 被门控
    assert result.signals["keyword"] is None
    assert result.signals["cadence"] is None


# ── 情绪信号 ──

def test_emotion_negative_is_positive_evidence():
    scorer = FocusScorer(make_settings())
    result = scorer.score(user_text="", emotion_reading=FakeEmotion(valence=-0.8, arousal=0.6))
    assert result.signals["emotion"] is not None
    assert result.signals["emotion"] > 0.0


def test_emotion_positive_pulls_down():
    scorer = FocusScorer(make_settings())
    result = scorer.score(user_text="", emotion_reading=FakeEmotion(valence=0.8, arousal=0.6))
    assert result.signals["emotion"] is not None
    assert result.signals["emotion"] < 0.0


def test_emotion_neutral_is_absent():
    scorer = FocusScorer(make_settings())
    result = scorer.score(user_text="", emotion_reading=FakeEmotion(valence=0.0, arousal=0.0))
    assert result.signals["emotion"] is None


# ── question（认知负荷）信号 ──

def test_question_signal():
    scorer = FocusScorer(make_settings())
    result = scorer.score(user_text="", emotion_reading=FakeEmotion(valence=0.0, arousal=0.0, complexity=0.8))
    assert result.signals["question"] == 0.8
    assert abs(result.score - 0.6 * 0.8) < 1e-6


def test_no_signals_zero_score():
    scorer = FocusScorer(make_settings())
    result = scorer.score(user_text="", emotion_reading=None)
    assert result.score == 0.0
    assert result.signals["keyword"] is None


# ── 加权和（直接搬算法） ──

def test_weighted_sum_matches_reference():
    scorer = FocusScorer(make_settings(signal_weights={"keyword": 1.0, "cadence": 0.8,
                                                       "emotion": 1.0, "question": 0.6},
                                       keyword_saturation=3))
    # 3 个关键词命中 → keyword 饱和 1.0；负效价强 → emotion 1.0；总分 = 2.0
    result = scorer.score(user_text="累 累 累", emotion_reading=FakeEmotion(valence=-1.0, arousal=1.0))
    assert result.signals["keyword"] == 1.0
    assert result.signals["emotion"] == 1.0
    assert result.score > 1.5
    assert result.score <= 2.0 + 1e-6


# ── 状态机：默认关零行为 ──

def test_disabled_zero_behavior():
    sm = FocusStateMachine(settings=make_settings(enabled=False))
    high = FocusScore(score=2.0, signals={"keyword": 1.0})
    calls = []
    sm.add_listener(lambda *a: calls.append(a))
    assert sm.update(high) is False
    assert sm.active is False
    assert sm.charge == 0.0
    assert calls == []  # 不回调
    assert sm.update(None) is False


def test_load_focus_settings_default_disabled():
    settings = load_focus_settings()
    assert settings.get("enabled", False) is False  # 默认关（config focus.enabled=false）


# ── 状态机：hysteresis 进出阈值 ──

def test_hysteresis_enter_and_exit():
    sm = FocusStateMachine(settings=make_settings(
        enabled=True, charge_enter=0.6, charge_exit=0.3,
        charge_gain=0.35, charge_cap=1.0, charge_decay=0.08,
    ))
    # 单次高分（score=2.0 → charge += 0.7）→ 进入专注
    assert sm.update(FocusScore(score=2.0, signals={"keyword": 1.0})) is True
    assert sm.active is True
    assert sm.charge >= 0.6
    # 之后空转冷却，charge 逐步衰减到 < 0.3 才退出（hysteresis 带）
    exited = False
    for _ in range(200):
        if sm.update(None) is True:
            exited = True
            break
    assert exited is True
    assert sm.active is False
    assert sm.charge < 0.3


def test_hysteresis_stays_active_in_band():
    sm = FocusStateMachine(settings=make_settings(
        enabled=True, charge_enter=0.6, charge_exit=0.3, charge_gain=0.35,
    ))
    sm.update(FocusScore(score=2.0, signals={"keyword": 1.0}))
    assert sm.active is True
    # 中等分保持在 [0.3, 0.6) 带内不退出（差一点不够触发，但也不退出）
    # charge 当前 ≈ 0.7；喂 0 分几轮，若电荷仍在带内则不退出
    for _ in range(3):
        sm.update(None)
    assert sm.active is True  # 0.7 只衰减几次仍在 0.3 以上


def test_state_listener_notified():
    sm = FocusStateMachine(settings=make_settings(enabled=True))
    seen = []
    sm.add_listener(lambda active, charge, signals: seen.append((active, charge)))
    sm.update(FocusScore(score=2.0, signals={"keyword": 1.0}))
    assert seen and seen[0][0] is True


# ── 情绪适配 ──

def test_emotion_reading_from_state():
    class State:
        current = "sad"
        intensity = 0.8

    reading = emotion_reading_from_state(State())
    assert reading is not None
    assert reading.valence < 0.0

    class Neutral:
        current = "neutral"
        intensity = 0.0

    assert emotion_reading_from_state(Neutral()) is None


# ── 行为层专注参数（motion/behavior.py 联动） ──

def test_behavior_focus_params_quiet():
    from motion.behavior import BEHAVIOR_MODES, MOUSE_REACTIONS
    assert "focus" in BEHAVIOR_MODES
    assert "focus" in MOUSE_REACTIONS
    bp = BEHAVIOR_MODES["focus"]
    assert bp.walk_chance <= 0.1          # 几乎不走动
    assert bp.speed_mul < 1.0             # 移动慢
    assert bp.min_pause >= 4000           # 休息长
    mr = MOUSE_REACTIONS["focus"]
    assert not mr.gaze_enabled and not mr.react_nearby
    assert not mr.chase_enabled and not mr.react_startle
