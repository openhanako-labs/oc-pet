# -*- coding: utf-8 -*-
"""P1-5/P1-6 反重复 + 屏幕/意图感知升级测试。

覆盖：
  - AntiRepeat 语义指纹：同文案跨会话拒绝 / 不同文案放行 / 时间窗过期放行
  - AntiRepeat 未回应长窗信号（跨小时高度相似主动搭话 → 拒绝）
  - AntiRepeat 持久化（写盘 → 新实例读回）
  - 屏幕场景分类规则（工作/娱乐/摸鱼/深夜/游戏/私密/学习/聊天/空闲）
  - LLM 语义增强失败降级（provider 失败/垃圾回复 → 保留规则结果）
  - LLM 语义增强成功合并（source=hybrid）
  - proactive 联动：screen provider（closed 硬跳过）+ anti_repeat 投递去重

运行: python -m pytest tests/test_p1_anti_repeat_screen.py -v
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from core.anti_repeat import (
    AntiRepeatCorpus,
    bm25_score,
    ANTI_REPEAT_FG_TTL_SECONDS,
    ANTI_REPEAT_MIN_DRAFT_TOKENS,
    ANTI_REPEAT_DROP_THRESHOLD,
)
from core.perception.screen_intent import (
    ScreenScene,
    classify_screen_scene,
    enrich_screen_scene,
    to_intent_scenario,
    focus_score_from_scene,
)
from core.perception.proactive import ProactiveScheduler


# ── 工具 ────────────────────────────────────────────────

def _corpus(tmp_path):
    return AntiRepeatCorpus(memory_dir=str(tmp_path))


def _long_text(topic: str) -> str:
    """构造超过 MIN_DRAFT_TOKENS 的文本（避免太短放行）。"""
    return f"我们今天聊聊{topic}吧，最近{topic}的事情你听说了吗？我觉得{topic}很有意思，想和你多说说{topic}的话题。"


# ── AntiRepeat BM25 语义指纹 ─────────────────────────────

def test_bm25_score_common_words_low_idf():
    """常见词（出现在所有 BG 文档）几乎不贡献分；话题词贡献高。"""
    fg = [["今天", "觉得", "老虎", "纳米机器"], ["今天", "觉得", "老虎", "bug"]]
    bg = [["今天", "觉得", "哈哈", "嗯", "老虎"], ["今天", "觉得", "哈哈", "嗯", "bug"], ["今天", "觉得", "哈哈", "嗯"]]
    total, terms = bm25_score(["老虎", "今天", "觉得"], fg, bg)
    assert total > 0
    assert "老虎" in terms
    # 常见词"今天/觉得"在 bg 高频 → 不贡献或贡献极小
    assert "今天" not in terms or terms["今天"] < terms["老虎"]


def test_anti_repeat_same_topic_cross_session_rejected(tmp_path):
    """同话题跨会话：先记录 → 后投递同话题 → 拒绝（语义指纹）。"""
    store = _corpus(tmp_path)
    now = time.time()
    text = _long_text("老虎")
    store.record_output("miku", text, is_proactive=True, now=now)
    assert store.is_repeat("miku", _long_text("老虎"), now=now + 60) is True


def test_anti_repeat_different_topic_allowed(tmp_path):
    """不同话题 → 放行（BM25 分数低）。"""
    store = _corpus(tmp_path)
    now = time.time()
    store.record_output("miku", _long_text("老虎"), is_proactive=True, now=now)
    assert store.is_repeat("miku", _long_text("量子物理"), now=now + 60) is False


def test_anti_repeat_time_window_expiry_allows(tmp_path):
    """时间窗过期：FG TTL 过后语义指纹分数归零 → 放行（防空闲死锁）。"""
    store = _corpus(tmp_path)
    now = time.time()
    text = _long_text("老虎")
    store.record_output("miku", text, is_proactive=True, now=now)
    # TTL 过后（10 分钟+），FG 清空 → 分数 0 → 放行
    later = now + ANTI_REPEAT_FG_TTL_SECONDS + 60
    total, _terms = store.score_draft("miku", _long_text("老虎"), now=later)
    assert total == 0.0
    assert store.is_repeat("miku", _long_text("老虎"), now=later) is False


def test_anti_repeat_short_draft_allowed(tmp_path):
    """太短草稿不评分直接放行（MIN_DRAFT_TOKENS 以下）。"""
    from core.memory_keywords import tokenize
    store = _corpus(tmp_path)
    now = time.time()
    store.record_output("miku", _long_text("老虎"), is_proactive=True, now=now)
    short = "嗯。"
    assert len(tokenize(short)) < ANTI_REPEAT_MIN_DRAFT_TOKENS
    total, _terms = store.score_draft("miku", short, now=now + 5)
    assert total == 0.0


def test_anti_repeat_unanswered_signal_cross_session(tmp_path):
    """未回应长窗：用户持续无互动时，同一主动搭话出现 ≥2 次 → 触发拒绝。"""
    store = _corpus(tmp_path)
    now = time.time()
    silence_since = now - 3600  # 用户 1 小时没说话
    text = "深夜了，记得早点休息哦，明天还有精神继续工作！"
    # 两次隔开的相同主动搭话（都投递在 silence_since 之后）
    store.record_output("miku", text, is_proactive=True, now=now - 1800)
    store.record_output("miku", text, is_proactive=True, now=now - 600)
    sig = store.score_unanswered_proactive_draft(
        "miku", text, silence_since=silence_since, now=now,
    )
    assert sig.triggered is True
    assert sig.match_count >= 2
    assert sig.repeated_terms  # 有重复话题词


def test_anti_repeat_unanswered_reset_by_user_message(tmp_path):
    """用户一旦真实互动，旧的未回应证据立即失效（silence_since 后移）。"""
    store = _corpus(tmp_path)
    now = time.time()
    text = "深夜了，记得早点休息哦，明天还有精神继续工作！"
    store.record_output("miku", text, is_proactive=True, now=now - 1800)
    store.record_output("miku", text, is_proactive=True, now=now - 600)
    # 用户刚回复（silence_since 移到最近）→ 之前两条都早于下限 → 不触发
    sig = store.score_unanswered_proactive_draft(
        "miku", text, silence_since=now - 300, now=now,
    )
    assert sig.triggered is False


def test_anti_repeat_persistence(tmp_path):
    """持久化：写盘后新实例能读回同一 corpus（跨会话）。"""
    dir_path = str(tmp_path)
    store1 = AntiRepeatCorpus(memory_dir=dir_path)
    now = time.time()
    text = _long_text("老虎")
    store1.record_output("miku", text, is_proactive=True, now=now)
    assert os.path.exists(os.path.join(dir_path, "anti_repeat.json"))
    # 新实例（模拟重启）读回
    store2 = AntiRepeatCorpus(memory_dir=dir_path)
    assert store2.is_repeat("miku", _long_text("老虎"), now=now + 60) is True
    # 空名归一化到 default
    store1.record_output("", _long_text("猫娘"), is_proactive=True, now=now)
    store3 = AntiRepeatCorpus(memory_dir=dir_path)
    assert store3.is_repeat("default", _long_text("猫娘"), now=now + 60) is True


def test_anti_repeat_clear(tmp_path):
    """clear 清空 corpus 后放行。"""
    store = _corpus(tmp_path)
    now = time.time()
    store.record_output("miku", _long_text("老虎"), is_proactive=True, now=now)
    store.clear("miku")
    assert store.is_repeat("miku", _long_text("老虎"), now=now + 60) is False


# ── 屏幕场景分类规则 ─────────────────────────────────────

def test_scene_gaming():
    scene = classify_screen_scene(category="gaming", activity="mouse", period="evening")
    assert scene.scene == "gaming"
    assert scene.intent == "gaming"
    assert scene.propensity == "restricted_screen_only"
    assert scene.confidence >= 0.8


def test_scene_work_focus():
    scene = classify_screen_scene(category="development", activity="typing", period="afternoon")
    assert scene.scene == "work_focus"
    assert scene.intent == "work"
    assert scene.confidence >= 0.6


def test_scene_late_night_work():
    scene = classify_screen_scene(
        category="development", activity="typing", period="late_night",
        fg_duration_min=45,
    )
    assert scene.scene == "late_night_work"
    assert scene.intent == "tense"
    assert scene.confidence >= 0.7


def test_scene_long_work_break():
    scene = classify_screen_scene(
        category="work", activity="mouse", period="afternoon", fg_duration_min=120,
    )
    assert scene.scene == "long_work_break"
    assert scene.intent == "tired"


def test_scene_slacking_work_hours():
    # 工作日 14 点 + 浏览（未达学习阈值）→ 摸鱼
    scene = classify_screen_scene(
        category="browsing", activity="mouse", period="afternoon",
        hour=14, weekday=2, is_weekend=False, fg_duration_min=3,
    )
    assert scene.scene == "slacking"
    assert scene.intent == "slacking"


def test_scene_not_slacking_on_weekend():
    scene = classify_screen_scene(
        category="browsing", activity="mouse", period="afternoon",
        hour=14, weekday=6, is_weekend=True, fg_duration_min=3,
    )
    assert scene.scene != "slacking"


def test_scene_private_keyword():
    scene = classify_screen_scene(
        category="browsing", activity="idle", period="afternoon",
        title="银行登录 - 密码管理器", app="chrome.exe",
    )
    assert scene.scene == "private"
    assert scene.propensity == "closed"


def test_scene_entertainment():
    scene = classify_screen_scene(category="entertainment", activity="mouse", period="evening")
    assert scene.scene == "video_watching"
    assert scene.propensity == "restricted_screen_only"


def test_scene_learning():
    scene = classify_screen_scene(
        category="browsing", activity="mouse", period="morning", fg_duration_min=20,
    )
    assert scene.scene == "learning"
    assert scene.intent == "learn"


def test_scene_communication():
    scene = classify_screen_scene(category="communication", activity="typing", period="afternoon")
    assert scene.scene == "chatting"
    assert scene.intent == "chatting"


def test_scene_title_ide():
    scene = classify_screen_scene(
        category="other", activity="typing", period="afternoon",
        app="Code.exe", title="main.py - Visual Studio Code",
    )
    assert scene.scene == "work_focus"


def test_to_intent_scenario_mapping():
    # 周末：weekend_play 类场景保持映射
    assert to_intent_scenario("late_night_work", is_weekend=True) == "late_night_work"
    assert to_intent_scenario("gaming", is_weekend=True) == "gaming"
    assert to_intent_scenario("video_watching", is_weekend=True) == "video_watching"
    assert to_intent_scenario("slacking", is_weekend=True) == "weekend_play"
    assert to_intent_scenario("music_listening", is_weekend=True) == "weekend_play"
    assert to_intent_scenario("unknown_scene", is_weekend=True) == "chat_idle"
    # 工作日：weekend_play 类场景（slacking / music_listening）降级为 chat_idle
    assert to_intent_scenario("slacking", is_weekend=False) == "chat_idle"
    assert to_intent_scenario("music_listening", is_weekend=False) == "chat_idle"


# ── LLM 语义增强：失败降级 / 成功合并 ─────────────────────

def _rule_scene():
    return classify_screen_scene(category="development", activity="typing", period="afternoon")


def test_enrich_failure_none_provider_keeps_rule():
    """未配置 provider（None）→ 直接返回规则结果。"""
    rule = _rule_scene()
    merged = enrich_screen_scene(rule, None)
    assert merged is rule
    assert merged.source == "rule"


def test_enrich_failure_bad_json_falls_back_to_rule():
    """provider 返回垃圾文本 → 解析失败 → 保留规则结果。"""
    rule = _rule_scene()
    merged = enrich_screen_scene(rule, lambda prompt: "今天天气不错啊哈哈")
    assert merged.source == "rule"
    assert merged.scene == rule.scene


def test_enrich_failure_exception_falls_back_to_rule():
    """provider 抛异常 → 失败降级 → 保留规则结果。"""
    rule = _rule_scene()

    def boom(prompt):
        raise RuntimeError("llm down")

    merged = enrich_screen_scene(rule, boom)
    assert merged.source == "rule"
    assert merged.scene == rule.scene


def test_enrich_failure_invalid_scene_falls_back_to_rule():
    """LLM 返回不在白名单的场景 → 拒绝 → 保留规则结果。"""
    rule = _rule_scene()
    bad = '{"scene": "watching_porn_weird_hallucination", "confidence": 0.9, "guess": "x"}'
    merged = enrich_screen_scene(rule, lambda prompt: bad)
    assert merged.source == "rule"


def test_enrich_success_merges_hybrid():
    """LLM 成功返回合法 JSON → 合并（source=hybrid），规则兜底保证非 None。"""
    rule = _rule_scene()
    ok = '{"scene": "slacking", "confidence": 0.8, "guess": "用户在上班时间刷视频摸鱼"}'
    merged = enrich_screen_scene(rule, lambda prompt: ok, app="chrome", title="bilibili")
    assert merged.source == "hybrid"
    assert merged.scene == "slacking"
    assert merged.confidence == pytest.approx(0.8)
    assert "摸鱼" in merged.llm_guess


def test_enrich_fence_json_parsed():
    """容忍 markdown ```json 围栏。"""
    rule = _rule_scene()
    fenced = '```json\n{"scene": "chatting", "confidence": 0.7, "guess": "在聊天"}\n```'
    merged = enrich_screen_scene(rule, lambda prompt: fenced)
    assert merged.source == "hybrid"
    assert merged.scene == "chatting"


# ── focus 联动（复用 P0 FocusScore 接口）─────────────────

def test_focus_score_from_scene_mapping():
    from core.perception.focus import FocusScore
    work = ScreenScene(scene="work_focus", intent="work", confidence=0.8)
    fs = focus_score_from_scene(work)
    assert isinstance(fs, FocusScore)
    assert fs.score > 0
    gaming = ScreenScene(scene="gaming", intent="gaming", confidence=0.9)
    fs2 = focus_score_from_scene(gaming)
    assert fs2.score < 0
    idle = ScreenScene(scene="idle", intent="idle", confidence=0.5)
    fs3 = focus_score_from_scene(idle)
    assert fs3.score == 0.0
    assert focus_score_from_scene(None) is None


# ── proactive 联动 ───────────────────────────────────────

def test_proactive_screen_provider_closed_skips_intent():
    """屏幕场景 propensity=closed（私密）→ 意图路径硬跳过。"""
    from unittest.mock import MagicMock
    fw = MagicMock()
    fw.last_category = "writing"
    fw.fg_duration_min = 2.0
    fw.window_switches_5min = 0
    at = MagicMock()
    at.state.state = "idle"
    sched = ProactiveScheduler(foreground_watcher=fw, on_proactive=lambda t: None, activity_tracker=at)
    sched._last_conversation = time.time() - 5 * 60
    sched._cooldown_until = 0.0
    sched._rules = []

    sched.set_screen_scene_provider(lambda: {"scene": "private", "intent": "private",
                                             "confidence": 0.98, "propensity": "closed"})
    # _try_intent 内部先走 classify_intent（可能命中 chat_idle），屏幕 closed 必须拦下
    result = sched._try_intent(time.time(), sched._collect_signals(time.time()))
    assert result is None


def test_proactive_screen_provider_feeds_signals():
    """屏幕场景 provider 并入 signals（screen_scene/intent/confidence）。"""
    from unittest.mock import MagicMock
    fw = MagicMock()
    fw.last_category = "gaming"
    fw.fg_duration_min = 2.0
    fw.window_switches_5min = 0
    at = MagicMock()
    at.state.state = "mouse"
    sched = ProactiveScheduler(foreground_watcher=fw, on_proactive=lambda t: None, activity_tracker=at)
    sched.set_screen_scene_provider(lambda: ScreenScene(
        scene="gaming", intent="gaming", confidence=0.92, propensity="restricted_screen_only",
    ))
    signals = sched._collect_signals(time.time())
    assert signals["screen_scene"] == "gaming"
    assert signals["screen_intent"] == "gaming"
    assert signals["screen_confidence"] == pytest.approx(0.92)
    assert signals["screen_propensity"] == "restricted_screen_only"


def test_proactive_anti_repeat_delivery_dedup(tmp_path):
    """proactive 接入 AntiRepeat：同文案第二次投递被语义去重拦截（跨会话拒绝）。

    说明：短文案（<DROP_THRESHOLD BM25 分）的精确复读由字符串相似 throttle
    拦截；语义去重补的是"较长同话题"的近重复——这里用长文案验证"同文案跨会话
    拒绝"（第二次 _deliver 直接不投递）。
    """
    from unittest.mock import MagicMock
    delivered = []
    fw = MagicMock()
    fw.last_category = "writing"
    fw.fg_duration_min = 2.0
    fw.window_switches_5min = 0
    at = MagicMock()
    at.state.state = "idle"
    sched = ProactiveScheduler(foreground_watcher=fw, on_proactive=delivered.append, activity_tracker=at)
    store = AntiRepeatCorpus(memory_dir=str(tmp_path))
    sched.set_anti_repeat(store, agent_name="miku")

    text = "我们今天聊聊老虎吧，最近老虎的事情你听说了吗？我觉得老虎很有意思，想和你多说说老虎的话题。"
    # 直接调用 _deliver（生成路径统一投递入口）
    sched._deliver(text, source_key="test")
    assert len(delivered) == 1
    assert store.is_repeat("miku", text) is True
    # 第二次同文案 → 语义去重拦截，不投递
    sched._deliver(text, source_key="test")
    assert len(delivered) == 1


def test_proactive_anti_repeat_different_text_delivered(tmp_path):
    """proactive 接入 AntiRepeat：不同文案第二次正常投递。"""
    from unittest.mock import MagicMock
    delivered = []
    fw = MagicMock()
    fw.last_category = "writing"
    fw.fg_duration_min = 2.0
    fw.window_switches_5min = 0
    at = MagicMock()
    at.state.state = "idle"
    sched = ProactiveScheduler(foreground_watcher=fw, on_proactive=delivered.append, activity_tracker=at)
    store = AntiRepeatCorpus(memory_dir=str(tmp_path))
    sched.set_anti_repeat(store, agent_name="miku")

    sched._deliver("写了这么久，休息一下吧？我陪你说说话。", source_key="a")
    sched._deliver("周末出去玩吗？我带你去吃好吃的！", source_key="b")
    assert len(delivered) == 2
