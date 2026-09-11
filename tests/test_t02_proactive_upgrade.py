# -*- coding: utf-8 -*-
"""T02 主动搭话升级测试 — P0-1 LLM 生成 + P0-2 半衰期节流。

覆盖 T02 验收：
  - _half_life_for / _source_skip_probability 数值（对照 N.E.K.O.：硬跳过窗口内
    1.0，之后 0.5^(age/half_life)）
  - ProactiveThrottle 每日预算（达到即静默 / 跨天重置）
  - ProactiveThrottle source 半衰期节流 + 近期文案去重
  - P0-1 LLM 生成成功投递 / 失败回退模板池 / 同会话不重复触发（dedup）

运行: python -m pytest tests/test_t02_proactive_upgrade.py -v
"""
import os
import sys
import time
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from core.perception.proactive_state import (
    ProactiveThrottle,
    _half_life_for,
    _source_skip_probability,
    PROACTIVE_SOURCE_HARD_SKIP_SECONDS,
    PROACTIVE_SOURCE_HALF_LIFE_BY_KIND,
    PROACTIVE_SOURCE_HALF_LIFE_DEFAULT,
)
from core.perception.proactive_generation import (
    ProactiveGenerator,
    clean_generated,
    build_proactive_prompt,
)
from core.perception.proactive import ProactiveScheduler, DEFAULT_LLM_GENERATION


# ── 工具 ────────────────────────────────────────────────

class _FakeAdapter:
    """模拟 HanakoPetAdapter.chat() 的最小对象。"""

    def __init__(self, reply: str, exc: Exception | None = None):
        self._reply = reply
        self._exc = exc
        self.calls = []

    def chat(self, message, inject_memory=True, extra_context="", tools=None, source="user"):
        self.calls.append({"message": message, "source": source})
        if self._exc is not None:
            raise self._exc
        return self._reply, "neutral"


def _make_scheduler(foreground_category="writing", activity="idle", conv_idle_min=10.0, rules=None):
    fw = MagicMock()
    fw.last_category = foreground_category
    fw.fg_duration_min = 2.0
    fw.window_switches_5min = 0
    at = MagicMock()
    at.state.state = activity
    sched = ProactiveScheduler(foreground_watcher=fw, on_proactive=lambda t: None, activity_tracker=at)
    # 中和两个“按时钟小时生效”的门（2026-09-11 修）。
    #
    # 截图：本文件的生成类用例在 00:00-08:00 **必挂**，白天全绿。
    # 原因是 tick() 里有两道夜间门：
    #   1) `_get_period()` 在 0-8 点返回 "night"，而 night 的干扰预算是 0；
    #   2) `_is_dnd_active()` 在 0-8 点返回 True。
    # 两者都会让 tick() 直接 return None，而它们**本身是故意的产品行为**
    # （深夜不主动搭话），不是 bug。
    #
    # 这组用例验的是“生成链路能否投递”，不是时间门，故在此中和——
    # 否则测试结果会随墙钟变，凌晨重跑回归会把真失败埋进噪声里。
    sched._get_period = lambda: "evening"
    sched._is_dnd_active = lambda: False
    sched._last_conversation = time.time() - conv_idle_min * 60
    sched._cooldown_until = 0.0
    if rules is not None:
        sched._rules = rules
    return sched, fw, at


def _force_rule_fallback(sched, monkeypatch, prompt="写了这么久，休息一下吧？", weight=1.0):
    sched._rules = [{"idle_min": 0, "foreground": ["*"], "prompt": prompt, "weight": weight}]
    monkeypatch.setattr(
        "core.perception.proactive.classify_intent",
        lambda **kw: {"intent": "work", "scenario": "chat_idle", "confidence": 0.0, "reason": "test"},
    )
    sched._is_fullscreen = lambda: False


# ── P0-2 半衰期数值（对照 N.E.K.O. 算法）────────────────

def test_half_life_for_known_and_default():
    """_half_life_for：web/image 查表，未知 kind 用默认半衰期。"""
    assert _half_life_for("web") == PROACTIVE_SOURCE_HALF_LIFE_BY_KIND["web"]
    assert _half_life_for("image") == PROACTIVE_SOURCE_HALF_LIFE_BY_KIND["image"]
    assert _half_life_for("music") == PROACTIVE_SOURCE_HALF_LIFE_BY_KIND["music"]
    assert _half_life_for("unknown-kind") == PROACTIVE_SOURCE_HALF_LIFE_DEFAULT


def test_source_skip_probability_hard_window_is_1():
    """硬跳过窗口内（age < 5h）skip_probability = 1.0。"""
    age_in_window = PROACTIVE_SOURCE_HARD_SKIP_SECONDS - 1
    assert _source_skip_probability(age_in_window, 3 * 86400.0) == 1.0
    assert _source_skip_probability(0.0, 3 * 86400.0) == 1.0


def test_source_skip_probability_after_window_decays():
    """窗口外按 0.5^(age/half_life)：age=half_life → 0.5。"""
    half_life = 86400.0
    age = PROACTIVE_SOURCE_HARD_SKIP_SECONDS + half_life
    assert _source_skip_probability(age, half_life) == pytest.approx(0.5)
    # age=2*half_life → 0.25
    age2 = PROACTIVE_SOURCE_HARD_SKIP_SECONDS + 2 * half_life
    assert _source_skip_probability(age2, half_life) == pytest.approx(0.25)
    # age=0（刚好等于窗口）→ 窗口外 0.5^0 = 1.0（age 严格小于窗口才返回 1.0）
    assert _source_skip_probability(PROACTIVE_SOURCE_HARD_SKIP_SECONDS, half_life) == pytest.approx(1.0)


def test_source_skip_probability_never_negative():
    """p_skip 永远在 [0,1]：age 极大时趋近 0。"""
    assert _source_skip_probability(10**9, 86400.0) == pytest.approx(0.0, abs=1e-9)
    assert 0.0 <= _source_skip_probability(0, 1) <= 1.0


# ── P0-2 每日预算 ───────────────────────────────────────

def test_daily_budget_silences_after_limit():
    """每日预算：达到上限后 can_trigger_today()=False（达到即静默）。"""
    throttle = ProactiveThrottle(daily_limit=3)
    assert throttle.can_trigger_today()
    throttle.record_trigger()
    throttle.record_trigger()
    assert throttle.can_trigger_today()
    throttle.record_trigger()
    assert not throttle.can_trigger_today()


def test_daily_budget_resets_cross_day():
    """每日预算：跨天自动重置。"""
    throttle = ProactiveThrottle(daily_limit=2)
    # 用固定时间戳模拟"昨天"
    yesterday = time.time() - 86400
    throttle.record_trigger(now=yesterday)
    throttle.record_trigger(now=yesterday)
    assert throttle.daily_count() == 2
    # 今天再触发 → 自动重置
    assert throttle.can_trigger_today()
    assert throttle.daily_count() == 0


# ── P0-2 source 半衰期节流 + 去重 ──────────────────────

def test_throttle_should_skip_within_hard_window(monkeypatch):
    """同一 source 在硬跳过窗口内 should_skip=True（不重复触发）。"""
    throttle = ProactiveThrottle()
    now = time.time()
    throttle.record_used("late_night_work", kind="chat", now=now)
    # 5 小时内 → 必跳
    assert throttle.should_skip("late_night_work", kind="chat", now=now + 60)
    # 完全不同的 source → 不跳
    assert not throttle.should_skip("gaming", kind="chat", now=now + 60)


def test_throttle_should_skip_after_half_life():
    """超过硬窗口 + 多个半衰期后不再跳过（p_skip 趋近 0）。"""
    throttle = ProactiveThrottle()
    now = time.time()
    throttle.record_used("web_topic", kind="web", now=now)
    # 2026-09-11 修：原来用 20 天，p_skip = 0.5^((20d-5h)/3d) ≈ 1.03e-2 ——
    # 也就意味着**每次全量跑有约 1% 概率假失败**（实测确实偶挂）。
    # 断言 should_skip 为 False 是概率性断言，必须把概率压到实际为零。
    # 200 天 → p_skip ≈ 9e-21。
    far_future = now + 200 * 86400
    skip = throttle.should_skip("web_topic", kind="web", now=far_future)
    assert skip is False, "200 天后 p_skip≈9e-21，不该再跳过"


def test_throttle_dedup_recent_chat():
    """同会话不重复触发：与近期文案高度相似 → is_duplicate=True。"""
    throttle = ProactiveThrottle()
    throttle.record_chat("写了这么久，休息一下吧？我陪你说说话。")
    assert throttle.is_duplicate("写了这么久，休息一下吧？我陪你说说话。") is True
    # 完全不同 → 不重复
    assert throttle.is_duplicate("今天天气真不错，想出去走走吗？") is False


def test_throttle_dedup_ignores_old_entries():
    """超过 1 小时窗口的旧文案不再参与去重。"""
    throttle = ProactiveThrottle()
    old_ts = time.time() - 7200  # 2 小时前
    throttle.record_chat("写了这么久，休息一下吧？", now=old_ts)
    assert throttle.is_duplicate("写了这么久，休息一下吧？") is False


# ── P0-1 LLM 生成 ───────────────────────────────────────

def test_clean_generated_strips_labels_and_quotes():
    """clean_generated：去 [proactive]/[emotion:xxx] 前缀、引号、限长。"""
    assert clean_generated('[proactive] 想你了') == '想你了'
    assert clean_generated('[emotion:happy] 休息一下吧') == '休息一下吧'
    assert clean_generated('"带引号的话"') == '带引号的话'
    long_text = '啊' * 100
    cleaned = clean_generated(long_text)
    assert len(cleaned) <= 60


def test_build_proactive_prompt_contains_scenario():
    """prompt 构造：包含场景与参考方向，让 LLM 不照抄模板。"""
    prompt = build_proactive_prompt({
        "scenario": "late_night_work",
        "signals": {"category": "development", "period": "night", "conversation_idle_min": 30},
        "fallback_prompt": "都这么晚了还在忙呀…",
    })
    assert "late_night_work" in prompt
    assert "参考方向" in prompt
    assert "不要照抄" in prompt


def test_generator_success_delivers_generated_text():
    """LLM 生成成功 → on_generated 收到生成文案（非模板）。"""
    adapter = _FakeAdapter(reply="这么晚还在写代码呀，注意休息哦。[emotion:thinking]")
    gen = ProactiveGenerator(adapter=adapter, use_qt_bridge=False)
    results = []
    gen.set_callbacks(on_generated=lambda t: results.append(("gen", t)), on_fallback=lambda t: results.append(("fb", t)))
    gen.generate({"scenario": "late_night_work", "signals": {}}, "模板文案")
    assert results, "应触发回调"
    kind, text = results[0]
    assert kind == "gen"
    assert "这么晚还在写代码呀" in text
    # 必须走 source=proactive（不污染 Hanako 会话历史）
    assert adapter.calls[0]["source"] == "proactive"


def test_generator_failure_falls_back_to_template():
    """LLM 调用抛异常 → 回退模板池文案（经 on_generated 投递兜底，不静默）。"""
    adapter = _FakeAdapter(reply="", exc=RuntimeError("llm down"))
    gen = ProactiveGenerator(adapter=adapter, use_qt_bridge=False)
    results = []
    gen.set_callbacks(on_generated=lambda t: results.append(("gen", t)), on_fallback=lambda t: results.append(("fb", t)))
    gen.generate({"scenario": "late_night_work", "template_text": "先歇会儿吧~"}, "先歇会儿吧~")
    # 生成失败 → 回退模板池（注入的兜底文案经 on_generated 投递）
    assert results, "生成失败应回退模板池（不静默）"
    assert results[0] == ("gen", "先歇会儿吧~"), "应投递注入的兜底模板文案"


def test_generator_empty_output_falls_back_to_template():
    """LLM 返回空串 → 回退模板池文案（不静默）。"""
    adapter = _FakeAdapter(reply="   ")
    gen = ProactiveGenerator(adapter=adapter, use_qt_bridge=False)
    results = []
    gen.set_callbacks(on_generated=lambda t: results.append(("gen", t)), on_fallback=lambda t: results.append(("fb", t)))
    gen.generate({"scenario": "long_work_break", "template_text": "喝口水吧~"}, "喝口水吧~")
    assert results, "生成空结果应回退模板池（不静默）"
    assert results[0] == ("gen", "喝口水吧~"), "应投递注入的兜底模板文案"


def test_generator_no_adapter_not_available():
    """无适配器且无 llm_fn → is_available()=False（调度器不会启用生成）。"""
    gen = ProactiveGenerator(adapter=None, use_qt_bridge=False)
    assert gen.is_available() is False


# ── P0-1 调度器集成：生成 / 回退 / 不重复 ────────────────

def test_scheduler_generation_started_on_rule(monkeypatch):
    """规则命中且生成可用 → tick 启动生成（日志 generated via llm）。"""
    sched, _, _ = _make_scheduler(rules=[{"idle_min": 0, "foreground": ["*"], "prompt": "模板A", "weight": 1.0}])
    sched._is_fullscreen = lambda: False
    monkeypatch.setattr(
        "core.perception.proactive.classify_intent",
        lambda **kw: {"intent": "work", "scenario": "chat_idle", "confidence": 0.0, "reason": "test"},
    )
    delivered = []
    sched.on_proactive = delivered.append

    adapter = _FakeAdapter(reply="工作辛苦了，喝口水吧。[emotion:happy]")
    gen = ProactiveGenerator(adapter=adapter, use_qt_bridge=False)
    sched.set_generator(gen)

    result = sched.tick()
    # 同步路径下回调立即触发
    assert delivered, "应已投递生成文案"
    assert "喝口水吧" in delivered[0]
    assert sched._daily_count == 1


def test_scheduler_generation_failure_falls_back_to_template(monkeypatch):
    """LLM 失败 → 回退模板池投递（不静默、计触发）。"""
    sched, _, _ = _make_scheduler(rules=[{"idle_min": 0, "foreground": ["*"], "prompt": "写了这么久，休息一下吧？", "weight": 1.0}])
    sched._is_fullscreen = lambda: False
    monkeypatch.setattr(
        "core.perception.proactive.classify_intent",
        lambda **kw: {"intent": "work", "scenario": "chat_idle", "confidence": 0.0, "reason": "test"},
    )
    delivered = []
    sched.on_proactive = delivered.append

    adapter = _FakeAdapter(reply="", exc=TimeoutError("timeout"))
    gen = ProactiveGenerator(adapter=adapter, use_qt_bridge=False)
    sched.set_generator(gen)
    # 让模板兜底返回确定文案，断言可稳定
    monkeypatch.setattr(
        "core.template_library.generate_template_fallback",
        lambda ctx: "兜底：先歇会儿",
    )

    sched.tick()
    # 生成失败 → 回退模板池投递（不静默）
    assert len(delivered) == 1, "生成失败应回退模板池投递"
    assert delivered[0] == "兜底：先歇会儿", "应投递模板兜底文案"
    assert sched._daily_count == 1


def test_scheduler_no_double_trigger_same_tick(monkeypatch):
    """生成在途时 tick 不重复触发其他路径（_generation_in_flight 守卫）。"""
    sched, _, _ = _make_scheduler(rules=[{"idle_min": 0, "foreground": ["*"], "prompt": "模板A", "weight": 1.0}])
    sched._is_fullscreen = lambda: False
    monkeypatch.setattr(
        "core.perception.proactive.classify_intent",
        lambda **kw: {"intent": "work", "scenario": "chat_idle", "confidence": 0.0, "reason": "test"},
    )
    delivered = []
    sched.on_proactive = delivered.append

    adapter = _FakeAdapter(reply="生成文案A")
    gen = ProactiveGenerator(adapter=adapter, use_qt_bridge=False)
    sched.set_generator(gen)

    # 第一次 tick：启动生成并立即投递（同步路径）
    sched.tick()
    assert len(delivered) == 1
    # 立即再 tick：冷却未到 + 无生成在途 → 由于同规则节流（hard-skip），不应再次投递
    sched.tick()
    assert len(delivered) == 1, "同会话/同规则不应重复触发"


def test_intent_generation_blocks_recall_and_rules_same_tick(monkeypatch):
    """意图命中且启动生成后，同一 tick 不得再走回忆/规则（防双投递）。"""
    sched, _, _ = _make_scheduler(rules=[{"idle_min": 0, "foreground": ["*"], "prompt": "规则文案", "weight": 1.0}])
    sched._is_fullscreen = lambda: False
    # 意图高置信命中（会走生成路径）
    monkeypatch.setattr(
        "core.perception.proactive.classify_intent",
        lambda **kw: {"intent": "work", "scenario": "late_night_work", "confidence": 0.9, "reason": "test"},
    )
    delivered = []
    sched.on_proactive = delivered.append
    # 注入 scene_memory 让 recall 有机会触发（若未被 in-flight 守卫挡住则会产生第二条）
    sched._scene_memory = MagicMock()
    sched._scene_memory.find_matching.return_value = []

    adapter = _FakeAdapter(reply="夜深了，早点休息吧[emotion:sad]")
    gen = ProactiveGenerator(adapter=adapter, use_qt_bridge=False)
    sched.set_generator(gen)

    sched.tick()
    assert len(delivered) == 1, "同一 tick 只能投递一条（生成文案），回忆/规则不得再触发"
    assert "早点休息吧" in delivered[0]


def test_scheduler_dedup_same_session(monkeypatch):
    """同会话不重复触发：LLM 生成与近期文案高度相似 → dedup 不投递。"""
    sched, _, _ = _make_scheduler(rules=[{"idle_min": 0, "foreground": ["*"], "prompt": "重复文案", "weight": 1.0}])
    sched._is_fullscreen = lambda: False
    monkeypatch.setattr(
        "core.perception.proactive.classify_intent",
        lambda **kw: {"intent": "work", "scenario": "chat_idle", "confidence": 0.0, "reason": "test"},
    )
    delivered = []
    sched.on_proactive = delivered.append

    # 注入一条近期文案，让后续生成/模板与它高度相似 → dedup
    sched._throttle.record_chat("写了这么久，休息一下吧？")
    adapter = _FakeAdapter(reply="写了这么久，休息一下吧？")
    gen = ProactiveGenerator(adapter=adapter, use_qt_bridge=False)
    sched.set_generator(gen)

    sched.tick()
    # 生成文案与近期重复 → 不投递
    assert delivered == []


def test_scheduler_llm_generation_disabled_uses_template(monkeypatch):
    """config proactive.llm_generation=False → 不启动生成，直接模板池。"""
    sched, _, _ = _make_scheduler()
    sched._is_fullscreen = lambda: False
    sched.set_generator(ProactiveGenerator(adapter=_FakeAdapter(reply="不应出现"), use_qt_bridge=False))
    # load_config 会重置 rules，因此把自定义规则一并传入
    sched.load_config({
        "llm_generation": False,
        "rules": [{"idle_min": 0, "foreground": ["*"], "prompt": "模板B", "weight": 1.0}],
    })
    monkeypatch.setattr(
        "core.perception.proactive.classify_intent",
        lambda **kw: {"intent": "work", "scenario": "chat_idle", "confidence": 0.0, "reason": "test"},
    )
    delivered = []
    sched.on_proactive = delivered.append

    sched.tick()
    assert delivered == ["模板B"]


def test_scheduler_default_llm_generation_flag():
    """默认 llm_generation=True（与 T01 config 骨架一致）。"""
    sched, _, _ = _make_scheduler()
    assert sched._llm_generation == DEFAULT_LLM_GENERATION is True


def test_generation_available_false_without_generator():
    """未注入生成器 → generation_available()=False（行为与旧版一致）。"""
    sched, _, _ = _make_scheduler()
    assert sched.generation_available() is False


def test_screen_slacking_not_mapped_to_weekend_on_weekday(monkeypatch):
    """屏幕感知为摸鱼(slacking)时，工作日不应覆盖到 weekend_play，避免说出"周末"。"""
    sched, _, _ = _make_scheduler(foreground_category="browsing", activity="idle", conv_idle_min=10.0)
    sched._is_fullscreen = lambda: False
    monkeypatch.setattr(
        "core.perception.proactive.classify_intent",
        lambda **kw: {"intent": "work", "scenario": "chat_idle", "confidence": 0.5, "reason": "test"},
    )
    monkeypatch.setattr(
        "core.perception.proactive.get_reaction",
        lambda scenario, intensity=1.0: {"text": "工作日摸鱼测试", "emotion": "neutral"},
    )
    signals = {
        "period": "afternoon",
        "category": "browsing",
        "activity": "idle",
        "fg_duration_min": 2.0,
        "conversation_idle_min": 10.0,
        "window_switches_5min": 0,
        "is_weekend": False,
        "weekday": 2,
        "screen_scene": "slacking",
        "screen_intent": "slacking",
        "screen_confidence": 0.8,
        "screen_propensity": "open",
    }
    result = sched._try_intent(time.time(), signals)
    assert result is not None, "工作日摸鱼场景应触发非周末场景"
    assert "周末" not in result, f"不应在工作日说出周末文案：{result}"


def test_screen_slacking_maps_to_weekend_on_weekend(monkeypatch):
    """屏幕感知为摸鱼(slacking)时，周末可正常映射到 weekend_play。"""
    sched, _, _ = _make_scheduler(foreground_category="browsing", activity="idle", conv_idle_min=10.0)
    sched._is_fullscreen = lambda: False
    monkeypatch.setattr(
        "core.perception.proactive.classify_intent",
        lambda **kw: {"intent": "work", "scenario": "chat_idle", "confidence": 0.5, "reason": "test"},
    )
    monkeypatch.setattr(
        "core.perception.proactive.get_reaction",
        lambda scenario, intensity=1.0: {"text": "周末测试文案", "emotion": "happy"},
    )
    signals = {
        "period": "afternoon",
        "category": "browsing",
        "activity": "idle",
        "fg_duration_min": 2.0,
        "conversation_idle_min": 10.0,
        "window_switches_5min": 0,
        "is_weekend": True,
        "weekday": 5,
        "screen_scene": "slacking",
        "screen_intent": "slacking",
        "screen_confidence": 0.8,
        "screen_propensity": "open",
    }
    result = sched._try_intent(time.time(), signals)
    assert result is not None, "周末摸鱼场景应触发 weekend_play"
    assert "周末" in result, f"周末应使用周末文案：{result}"
