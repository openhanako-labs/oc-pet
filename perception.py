"""感知系统 - 整合时间/情绪/日程/屏幕视觉/主动对话

统一入口 PerceptionController，对外暴露：
  - build_context()  -> 注入 LLM prompt 的感知上下文
  - tick()           -> 每 30 秒调用，驱动情绪衰减 + 屏幕分析 + 主动对话
  - trigger_emotion() -> 触发情绪状态
  - get_screen_context() -> 屏幕感知结果
  - check_proactive()   -> 主动对话触发检查

原 screen_watcher.py + perception.py + proactive_scheduler.py 合并于此。
"""
from __future__ import annotations

import base64
import io
import json
import logging
import random
import threading
import time
from datetime import datetime
from pathlib import Path

import requests
from PIL import ImageGrab

logger = logging.getLogger(__name__)

HANAKO_HOME = Path.home() / ".hanako"


# ════════════════════════════════════════════════════════════
#  时间感知
# ════════════════════════════════════════════════════════════

class TimePerception:
    """时间感知 - 区分工作时段/休息时段/深夜"""

    PERIODS = {
        (6, 12): ("morning", "早上"),
        (12, 14): ("noon", "中午"),
        (14, 18): ("afternoon", "下午"),
        (18, 22): ("evening", "晚上"),
        (22, 24): ("late_night", "深夜"),
        (0, 6): ("midnight", "凌晨"),
    }

    def get_context(self) -> dict:
        now = datetime.now()
        hour = now.hour
        period, label = "other", "未知"
        for (start, end), (pid, plabel) in self.PERIODS.items():
            if start <= hour < end:
                period, label = pid, plabel
                break
        return {
            "period": period, "label": label, "hour": hour,
            "weekday": now.weekday(), "is_weekend": now.weekday() >= 5,
            "date": now.strftime("%Y-%m-%d"), "time": now.strftime("%H:%M"),
        }

    def format_for_prompt(self) -> str:
        ctx = self.get_context()
        weekend = "周末" if ctx["is_weekend"] else "工作日"
        return f"[当前时间：{ctx['label']} {ctx['time']}，{weekend}]"


# ════════════════════════════════════════════════════════════
#  情绪状态机
# ════════════════════════════════════════════════════════════

class EmotionStateMachine:
    """情绪状态机 - 连续感知，强度衰减"""

    DECAY_RATE = 0.08       # 每分钟衰减 8%
    THRESHOLD_HIGH = 0.5
    THRESHOLD_LOW = 0.15

    def __init__(self):
        self._current: str = "neutral"
        self._intensity: float = 0.0
        self._last_trigger: float = 0.0
        self._history: list[dict] = []

    def trigger(self, emotion: str, intensity: float = 1.0):
        if not emotion or emotion == "neutral":
            return
        self._current = emotion
        self._intensity = min(1.0, max(0.0, intensity))
        self._last_trigger = time.time()
        self._history.append({"emotion": emotion, "intensity": self._intensity, "time": datetime.now().isoformat()})
        if len(self._history) > 10:
            self._history.pop(0)

    def tick(self):
        if self._current == "neutral":
            return
        elapsed = time.time() - self._last_trigger
        decay = self.DECAY_RATE * (elapsed / 60.0)
        self._intensity = max(0.0, self._intensity - decay)
        if self._intensity <= self.THRESHOLD_LOW:
            self._current = "neutral"
            self._intensity = 0.0

    def reset(self):
        self._current = "neutral"
        self._intensity = 0.0
        self._last_trigger = time.time()

    @property
    def current(self) -> str:
        return self._current

    @property
    def intensity(self) -> float:
        return self._intensity

    def should_show_emotion(self) -> bool:
        return self._intensity > self.THRESHOLD_LOW

    def format_for_prompt(self) -> str:
        if self._current == "neutral":
            return ""
        return f"[当前情绪：{self._current}（强度 {self._intensity:.0%}）]"


# ════════════════════════════════════════════════════════════
#  日程感知
# ════════════════════════════════════════════════════════════

class SchedulePerception:
    """日程感知 - 读取 Hanako 自动化任务"""

    def __init__(self):
        self._automations: list[dict] = []

    def refresh(self):
        self._automations = []
        try:
            auto_dir = HANAKO_HOME / ".ephemeral"
            if auto_dir.exists():
                for f in auto_dir.glob("automation*.json"):
                    try:
                        data = json.loads(f.read_text("utf-8"))
                        if isinstance(data, list):
                            self._automations.extend(data)
                        elif isinstance(data, dict):
                            self._automations.append(data)
                    except Exception:
                        pass
        except Exception as e:
            logger.debug("Schedule refresh failed: %s", e)

    def get_upcoming(self, max_items: int = 3) -> list[dict]:
        return self._automations[:max_items]

    def format_for_prompt(self) -> str:
        items = self.get_upcoming()
        if not items:
            return ""
        lines = ["[即将到来的定时任务]"]
        for item in items:
            label = item.get("label", item.get("name", "未知"))
            schedule = item.get("schedule", "")
            lines.append(f"- {label}（{schedule}）")
        return "\n".join(lines)


# ════════════════════════════════════════════════════════════
#  屏幕视觉感知
# ════════════════════════════════════════════════════════════

SCREENSHOT_SCALE = 4
JPEG_QUALITY = 50
VISION_PROMPT = "用一句话简短描述用户当前在屏幕上做什么（不超过20字）"


class ScreenPerception:
    """屏幕感知 - 后台定时截屏 + 视觉模型分析"""

    def __init__(self, interval: int = 120):
        self._interval = interval
        self._running = False
        self._thread = None
        self._last_description: str = ""
        self._lock = threading.Lock()
        self.on_update: callable = lambda desc: None

    @property
    def last_description(self) -> str:
        with self._lock:
            return self._last_description

    def get_context(self) -> str:
        with self._lock:
            if self._last_description:
                return f"[屏幕画面：{self._last_description}]"
        return ""

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logger.info("ScreenPerception started | interval=%ds", self._interval)

    def stop(self):
        self._running = False

    def _run(self):
        time.sleep(10)  # 首次延迟
        while self._running:
            try:
                self._capture_and_analyze()
            except Exception as e:
                logger.warning("ScreenPerception error: %s", e)
            for _ in range(self._interval):
                if not self._running:
                    return
                time.sleep(1)

    def _capture_and_analyze(self):
        from hanako_context import HanakoContext

        img = ImageGrab.grab()
        new_size = (img.width // SCREENSHOT_SCALE, img.height // SCREENSHOT_SCALE)
        img = img.resize(new_size)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=JPEG_QUALITY)
        b64 = base64.b64encode(buf.getvalue()).decode()
        logger.info("Screenshot: %s, %dKB base64", new_size, len(b64) // 1024)

        ctx = HanakoContext()
        cfg = ctx.read_model_config()
        api_url = cfg.get("base_url", "") + "/chat/completions"
        api_key = cfg.get("api_key", "")
        model = cfg.get("model", "")
        if not api_url or not api_key:
            return

        try:
            resp = requests.post(
                api_url,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": [
                        {"type": "text", "text": VISION_PROMPT},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                    ]}],
                    "max_tokens": 1000,
                    "temperature": 0.3,
                },
                timeout=30,
            )
            if resp.status_code == 200:
                content = resp.json()["choices"][0]["message"].get("content", "").strip()
                if content:
                    with self._lock:
                        self._last_description = content
                    logger.info("Screen analysis: %s", content[:50])
                    self.on_update(content)
                else:
                    logger.warning("Vision API returned empty content")
            else:
                logger.warning("Vision API error: %d", resp.status_code)
        except requests.exceptions.Timeout:
            logger.warning("Vision API timeout")
        except Exception as e:
            logger.warning("Vision analysis failed: %s", e)


# ════════════════════════════════════════════════════════════
#  主动对话调度
# ════════════════════════════════════════════════════════════

DEFAULT_RULES = [
    {"idle_min": 5,  "foreground": ["writing", "development", "browsing"], "prompt": "写了这么久，休息一下吧？", "weight": 0.7},
    {"idle_min": 15, "foreground": ["gaming", "entertainment"],             "prompt": "带我一起玩嘛～",          "weight": 0.5},
    {"idle_min": 30, "foreground": ["communication"],                       "prompt": "还在忙吗？想和你说说话～", "weight": 0.3},
    {"idle_min": 60, "foreground": ["*"],                                    "prompt": "好安静啊……你在做什么呢？",  "weight": 0.3},
]


class ProactiveScheduler:
    """主动对话调度器 - 规则引擎 + 空闲检测 + 前台分类"""

    def __init__(self, foreground_watcher=None, on_proactive: callable = None):
        self._foreground_watcher = foreground_watcher
        self._enabled = True
        self._cooldown_minutes = 10
        self._rules: list[dict] = list(DEFAULT_RULES)
        self._cooldown_until: float = 0.0
        self.on_proactive: callable = on_proactive or (lambda text: None)

    def load_config(self, config: dict):
        self._enabled = config.get("enabled", True)
        self._cooldown_minutes = config.get("cooldown_minutes", 10)
        self._rules = config.get("rules", list(DEFAULT_RULES))

    @property
    def enabled(self) -> bool:
        return self._enabled

    def enable(self):
        self._enabled = True

    def disable(self):
        self._enabled = False

    def reset(self):
        self._cooldown_until = time.time() + self._cooldown_minutes * 60

    def tick(self) -> str | None:
        if not self._enabled or not self._rules:
            return None
        now = time.time()
        if now < self._cooldown_until:
            return None

        # 系统空闲时间检测（Windows GetLastInputInfo）
        import ctypes
        class LASTINPUTINFO(ctypes.Structure):
            fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]
        lii = LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
        ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii))
        millis = ctypes.windll.kernel32.GetTickCount() - lii.dwTime
        idle_sec = millis / 1000.0
        if idle_sec < 180:
            return None

        category = "other"
        if self._foreground_watcher:
            category = self._foreground_watcher.last_category or "other"

        sorted_rules = sorted(self._rules, key=lambda r: r.get("idle_min", 0), reverse=True)
        for rule in sorted_rules:
            required_idle = rule.get("idle_min", 0) * 60
            if idle_sec < required_idle:
                continue
            fg_match = rule.get("foreground", ["*"])
            if "*" in fg_match or category in fg_match:
                weight = rule.get("weight", 0.5)
                if random.random() < weight:
                    prompt = rule.get("prompt", "")
                    if prompt:
                        self._cooldown_until = now + self._cooldown_minutes * 60
                        logger.info("Proactive triggered: idle=%ds fg=%s rule='%s'", int(idle_sec), category, prompt)
                        self.on_proactive(prompt)
                        return prompt
        return None


# ════════════════════════════════════════════════════════════
#  统一感知控制器
# ════════════════════════════════════════════════════════════

class PerceptionController:
    """统一感知控制器 - 整合时间/情绪/日程/屏幕/主动对话

    用法:
        ctrl = PerceptionController(character_id="ophelia")
        ctrl.start_screen(interval=120)
        ctrl.set_proactive(foreground_watcher=watcher, on_proactive=callback)
        ctrl.load_proactive_config(config)

        # 每 30 秒
        ctrl.tick()

        # 注入 LLM prompt
        context = ctrl.build_context()

        # 触发情绪
        ctrl.trigger_emotion("happy")
    """

    def __init__(self, character_id: str = "ophelia"):
        self._character_id = character_id
        self._time = TimePerception()
        self._emotion = EmotionStateMachine()
        self._schedule = SchedulePerception()
        self._screen = ScreenPerception()
        self._proactive: ProactiveScheduler | None = None
        self._last_schedule_refresh = 0.0

    @property
    def time(self) -> TimePerception:
        return self._time

    @property
    def emotion(self) -> EmotionStateMachine:
        return self._emotion

    @property
    def schedule(self) -> SchedulePerception:
        return self._schedule

    @property
    def screen(self) -> ScreenPerception:
        return self._screen

    @property
    def proactive(self) -> ProactiveScheduler | None:
        return self._proactive

    # ── 屏幕 ──

    def start_screen(self, interval: int = 120):
        self._screen._interval = interval
        self._screen.start()

    def stop_screen(self):
        self._screen.stop()

    def get_screen_context(self) -> str:
        return self._screen.get_context()

    # ── 情绪 ──

    def trigger_emotion(self, emotion: str, intensity: float = 1.0):
        self._emotion.trigger(emotion, intensity)

    def reset_emotion(self):
        self._emotion.reset()

    # ── 主动对话 ──

    def set_proactive(self, foreground_watcher=None, on_proactive: callable = None):
        self._proactive = ProactiveScheduler(foreground_watcher=foreground_watcher, on_proactive=on_proactive)

    def load_proactive_config(self, config: dict):
        if self._proactive:
            self._proactive.load_config(config)
        else:
            self.set_proactive()
            self._proactive.load_config(config)

    # ── 统一 tick（每 30 秒）──

    def tick(self):
        """每 30 秒调用，驱动情绪衰减 + 主动对话检查 + 日程刷新"""
        self._emotion.tick()
        if self._proactive:
            self._proactive.tick()
        now = time.time()
        if now - self._last_schedule_refresh > 600:
            self._schedule.refresh()
            self._last_schedule_refresh = now

    # ── 构建 LLM 上下文 ──

    def build_context(self) -> str:
        """组合所有感知信息为 prompt 上下文"""
        parts = []
        time_ctx = self._time.format_for_prompt()
        if time_ctx:
            parts.append(time_ctx)
        emotion_ctx = self._emotion.format_for_prompt()
        if emotion_ctx:
            parts.append(emotion_ctx)
        schedule_ctx = self._schedule.format_for_prompt()
        if schedule_ctx:
            parts.append(schedule_ctx)
        screen_ctx = self._screen.get_context()
        if screen_ctx:
            parts.append(screen_ctx)
        return "\n".join(parts) if parts else ""
