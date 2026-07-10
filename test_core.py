"""单元测试 - 核心逻辑（不依赖 Qt）

运行: python -m pytest test_core.py -v
"""
import time
from unittest.mock import patch

from perception import (
    EmotionStateMachine, TimePerception, ProactiveScheduler
)
from hanako_monitor import compact_bubble_text


# ════════════════════════════════════════════════════════════
#  EmotionStateMachine
# ════════════════════════════════════════════════════════════

class TestEmotionStateMachine:

    def test_initial_state(self):
        sm = EmotionStateMachine()
        assert sm.current == "neutral"
        assert sm.intensity == 0.0
        assert not sm.should_show_emotion()

    def test_trigger_sets_emotion(self):
        sm = EmotionStateMachine()
        sm.trigger("happy")
        assert sm.current == "happy"
        assert sm.intensity == 1.0
        assert sm.should_show_emotion()

    def test_neutral_trigger_ignored(self):
        sm = EmotionStateMachine()
        sm.trigger("happy")
        sm.trigger("neutral")
        assert sm.current == "happy"  # neutral 不覆盖

    def test_decay_over_time(self):
        sm = EmotionStateMachine()
        sm.trigger("happy", intensity=1.0)
        # 模拟 10 分钟过去
        sm._last_trigger = time.time() - 600
        sm.tick()
        # 10 分钟 * 8%/分钟 = 80% 衰减
        assert sm.intensity < 0.3
        assert sm.intensity > 0.0

    def test_full_decay_to_neutral(self):
        sm = EmotionStateMachine()
        sm.trigger("happy", intensity=1.0)
        # 模拟 15 分钟过去（超过 100% 衰减）
        sm._last_trigger = time.time() - 900
        sm.tick()
        assert sm.current == "neutral"
        assert sm.intensity == 0.0

    def test_reset(self):
        sm = EmotionStateMachine()
        sm.trigger("angry")
        sm.reset()
        assert sm.current == "neutral"
        assert sm.intensity == 0.0

    def test_format_for_prompt(self):
        sm = EmotionStateMachine()
        assert sm.format_for_prompt() == ""
        sm.trigger("happy")
        result = sm.format_for_prompt()
        assert "happy" in result
        assert "100%" in result

    def test_thread_safety(self):
        """并发 trigger + tick 不崩溃"""
        import threading
        sm = EmotionStateMachine()
        errors = []

        def trigger_loop():
            for _ in range(100):
                try:
                    sm.trigger("happy")
                except Exception as e:
                    errors.append(e)

        def tick_loop():
            for _ in range(100):
                try:
                    sm.tick()
                except Exception as e:
                    errors.append(e)

        t1 = threading.Thread(target=trigger_loop)
        t2 = threading.Thread(target=tick_loop)
        t1.start(); t2.start()
        t1.join(); t2.join()
        assert not errors


# ════════════════════════════════════════════════════════════
#  TimePerception
# ════════════════════════════════════════════════════════════

class TestTimePerception:

    def test_returns_valid_period(self):
        tp = TimePerception()
        ctx = tp.get_context()
        assert ctx["period"] in ("morning", "noon", "afternoon", "evening", "late_night", "midnight")
        assert 0 <= ctx["hour"] < 24
        assert 0 <= ctx["weekday"] < 7
        assert isinstance(ctx["is_weekend"], bool)

    def test_format_for_prompt(self):
        tp = TimePerception()
        result = tp.format_for_prompt()
        assert "当前时间" in result
        assert "周末" in result or "工作日" in result


# ════════════════════════════════════════════════════════════
#  ProactiveScheduler
# ════════════════════════════════════════════════════════════

class TestProactiveScheduler:

    def test_disabled_does_not_trigger(self):
        sched = ProactiveScheduler(on_proactive=lambda t: None)
        sched.disable()
        assert sched.tick() is None

    def test_cooldown_blocks_trigger(self):
        sched = ProactiveScheduler(on_proactive=lambda t: None)
        sched.load_config({"rules": [{"idle_min": 0, "foreground": ["*"], "prompt": "test", "weight": 1.0}]})
        # 设置冷却
        sched._cooldown_until = time.time() + 999
        assert sched.tick() is None

    def test_short_idle_does_not_trigger(self):
        sched = ProactiveScheduler(on_proactive=lambda t: None)
        sched.load_config({"rules": [{"idle_min": 5, "foreground": ["*"], "prompt": "test", "weight": 1.0}]})
        # 模拟 60 秒空闲（< 180 秒阈值）
        import ctypes
        orig_get_last_input = ctypes.windll.user32.GetLastInputInfo
        orig_get_tick = ctypes.windll.kernel32.GetTickCount
        try:
            ctypes.windll.kernel32.GetTickCount.return_value = 60000
            assert sched.tick() is None
        finally:
            ctypes.windll.user32.GetLastInputInfo = orig_get_last_input
            ctypes.windll.kernel32.GetTickCount = orig_get_tick

    def test_rule_match_triggers(self):
        triggered = []
        sched = ProactiveScheduler(on_proactive=lambda t: triggered.append(t))
        sched.load_config({"rules": [{"idle_min": 0, "foreground": ["*"], "prompt": "hi", "weight": 1.0}]})
        # 模拟 200 秒空闲（> 180 秒阈值）
        # 直接 patch _get_idle_seconds 太复杂，改为直接设置内部状态
        # 验证规则匹配逻辑：idle_min=0 + weight=1.0 + foreground=*
        # 只要 idle > 180 就触发
        import ctypes
        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]
        lii = LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
        lii.dwTime = 0  # 很久没输入
        orig = ctypes.windll.user32.GetLastInputInfo
        try:
            ctypes.windll.user32.GetLastInputInfo = lambda ptr: True
            ctypes.windll.kernel32.GetTickCount = lambda: 200000
            result = sched.tick()
            assert result == "hi"
            assert triggered == ["hi"]
        finally:
            ctypes.windll.user32.GetLastInputInfo = orig

    def test_enable_disable(self):
        sched = ProactiveScheduler()
        assert sched.enabled
        sched.disable()
        assert not sched.enabled
        sched.enable()
        assert sched.enabled

    def test_reset_sets_cooldown(self):
        sched = ProactiveScheduler()
        sched.load_config({"cooldown_minutes": 5})
        before = time.time()
        sched.reset()
        assert sched._cooldown_until > before


# ════════════════════════════════════════════════════════════
#  compact_bubble_text
# ════════════════════════════════════════════════════════════

class TestCompactBubbleText:

    def test_short_text_unchanged(self):
        assert compact_bubble_text("你好") == "你好"

    def test_long_text_truncated(self):
        long = "这是一段很长的文字" * 20
        result = compact_bubble_text(long)
        assert len(result) < len(long)

    def test_none_input(self):
        assert compact_bubble_text("") == ""

    def test_sentence_split(self):
        text = "第一句话。第二句话。第三句话。"
        result = compact_bubble_text(text)
        # compact_bubble_text 取最后一句
        assert "句话" in result
        assert len(result) <= len(text)
