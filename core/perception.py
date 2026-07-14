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
    """情绪状态机 - 连续感知，强度衰减（线程安全）"""

    DECAY_RATE = 0.08       # 每分钟衰减 8%
    THRESHOLD_HIGH = 0.5
    THRESHOLD_LOW = 0.15

    def __init__(self):
        self._current: str = "neutral"
        self._intensity: float = 0.0
        self._last_trigger: float = 0.0
        self._history: list[dict] = []
        self._lock = threading.Lock()

    def trigger(self, emotion: str, intensity: float = 1.0):
        if not emotion or emotion == "neutral":
            return
        with self._lock:
            self._current = emotion
            self._intensity = min(1.0, max(0.0, intensity))
            self._last_trigger = time.time()
            self._history.append({"emotion": emotion, "intensity": self._intensity, "time": datetime.now().isoformat()})
            if len(self._history) > 10:
                self._history.pop(0)

    def tick(self):
        with self._lock:
            if self._current == "neutral":
                return
            elapsed = time.time() - self._last_trigger
            decay = self.DECAY_RATE * (elapsed / 60.0)
            self._intensity = max(0.0, self._intensity - decay)
            if self._intensity <= self.THRESHOLD_LOW:
                self._current = "neutral"
                self._intensity = 0.0

    def reset(self):
        with self._lock:
            self._current = "neutral"
            self._intensity = 0.0
            self._last_trigger = time.time()

    @property
    def current(self) -> str:
        with self._lock:
            return self._current

    @property
    def intensity(self) -> float:
        with self._lock:
            return self._intensity

    def should_show_emotion(self) -> bool:
        with self._lock:
            return self._intensity > self.THRESHOLD_LOW

    def format_for_prompt(self) -> str:
        with self._lock:
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
VISION_PROMPT = """用一句话简短描述用户当前在屏幕上做什么（不超过20字）。

注意隐私保护：
- 不要读取或提及任何密码、验证码、密钥、token
- 不要读取或提及银行账户、信用卡号、身份证号等敏感信息
- 如果屏幕包含敏感信息，请描述为'用户正在处理私密信息'"""

# 屏幕内容→情绪映射
SCREEN_EMOTION_MAP = {
    # 关键词 → (情绪, 强度)
    "游戏": ("happy", 0.6),
    "gaming": ("happy", 0.6),
    "视频": ("happy", 0.4),
    "电影": ("happy", 0.4),
    "音乐": ("happy", 0.3),
    "代码": ("thinking", 0.5),
    "编程": ("thinking", 0.5),
    "开发": ("thinking", 0.5),
    "terminal": ("thinking", 0.5),
    "终端": ("thinking", 0.5),
    "写作": ("thinking", 0.4),
    "文档": ("thinking", 0.3),
    "阅读": ("thinking", 0.3),
    "聊天": ("happy", 0.3),
    "社交": ("happy", 0.3),
    "购物": ("happy", 0.3),
    "错误": ("surprised", 0.7),
    "error": ("surprised", 0.7),
    "崩溃": ("surprised", 0.8),
    "crash": ("surprised", 0.8),
}


class ScreenPerception:
    """屏幕感知 - 后台定时截屏 + 视觉模型分析

    优化：
    - 变化检测：对比上一帧 hash，相同则跳过 API 调用
    - 失败退避：连续失败时拉长间隔
    """

    MAX_CONSECUTIVE_FAILURES = 3

    def __init__(self, interval: int = 120):
        self._interval = interval
        self._base_interval = interval
        self._enabled = True  # 可通过配置禁用
        self._running = False
        self._thread = None
        self._last_description: str = ""
        self._last_frame_hash: str = ""
        self._consecutive_failures: int = 0
        self._lock = threading.Lock()
        self.on_update: callable = lambda desc: None
        self.on_emotion: callable = lambda emotion, intensity: None

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
        if not self._enabled:
            logger.info("ScreenPerception disabled by config")
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logger.info("ScreenPerception started | interval=%ds", self._interval)
    
    def disable(self):
        """禁用屏幕感知"""
        self._enabled = False
        self.stop()
    
    def enable(self):
        """启用屏幕感知"""
        self._enabled = True

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
        import hashlib as _hashlib
        from .hanako_context import HanakoContext

        img = ImageGrab.grab()
        new_size = (img.width // SCREENSHOT_SCALE, img.height // SCREENSHOT_SCALE)
        img = img.resize(new_size)
        
        # 隐私保护：对截图进行模糊处理（降低敏感信息可读性）
        try:
            from PIL import ImageFilter
            img = img.filter(ImageFilter.GaussianBlur(radius=2))
        except Exception:
            pass  # 模糊失败不影响正常流程

        # 变化检测：对比上一帧 hash
        frame_hash = _hashlib.md5(img.tobytes()).hexdigest()
        if frame_hash == self._last_frame_hash:
            logger.debug("Screen unchanged, skipping API call")
            return
        self._last_frame_hash = frame_hash

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=JPEG_QUALITY)
        b64 = base64.b64encode(buf.getvalue()).decode()
        logger.info("Screenshot: %s, %dKB base64", new_size, len(b64) // 1024)

        ctx = HanakoContext()
        
        # 优先使用视觉专用模型配置
        from env_config import get_vision_config, get_llm_config
        vision_cfg = get_vision_config()
        
        if vision_cfg:
            # 使用视觉专用配置
            base_url = vision_cfg["base_url"].rstrip("/")
            # 如果 base_url 已经包含 /v1，则不再添加
            if base_url.endswith("/v1"):
                api_url = base_url + "/chat/completions"
            else:
                api_url = base_url + "/v1/chat/completions"
            api_key = vision_cfg["api_key"]
            model = vision_cfg["model"]
            logger.debug("Using vision-specific model: %s", model)
        else:
            # 回退到 LLM 配置
            env_llm = get_llm_config()
            if env_llm:
                api_url = env_llm["base_url"] + "/v1/chat/completions"
                api_key = env_llm["api_key"]
                model = env_llm["model"]
            else:
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
                    self._consecutive_failures = 0
                    self._interval = self._base_interval  # 恢复正常间隔
                    logger.info("Screen analysis: %s", content[:50])
                    self.on_update(content)
                    # 触发屏幕情绪
                    self._detect_screen_emotion(content)
                else:
                    logger.warning("Vision API returned empty content")
                    self._consecutive_failures += 1
            else:
                logger.warning("Vision API error: %d", resp.status_code)
                self._consecutive_failures += 1
        except requests.exceptions.Timeout:
            logger.warning("Vision API timeout")
            self._consecutive_failures += 1
        except Exception as e:
            logger.warning("Vision analysis failed: %s", e)
            self._consecutive_failures += 1

        # 失败退避：连续失败时拉长间隔
        if self._consecutive_failures >= self.MAX_CONSECUTIVE_FAILURES:
            self._interval = self._base_interval * 3
            logger.warning("ScreenPerception backoff: interval=%ds (failures=%d)",
                         self._interval, self._consecutive_failures)

    def _detect_screen_emotion(self, description: str):
        """根据屏幕内容触发情绪"""
        desc_lower = description.lower()
        for keyword, (emotion, intensity) in SCREEN_EMOTION_MAP.items():
            if keyword in desc_lower:
                logger.info("Screen emotion triggered: %s (%.1f) from '%s'", emotion, intensity, description[:30])
                self.on_emotion(emotion, intensity)
                return


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
        self._last_conversation: float = time.time()  # 上次对话时间
        self.on_proactive: callable = on_proactive or (lambda text: None)

    def load_config(self, config: dict):
        self._enabled = config.get("enabled", True)
        self._cooldown_minutes = config.get("cooldown_minutes", 10)
        self._rules = config.get("rules", list(DEFAULT_RULES))

    def mark_conversation(self):
        """标记用户刚和桌宠对话过"""
        self._last_conversation = time.time()

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

        # 对话空闲时间（上次对话到现在）
        conversation_idle = now - self._last_conversation

        category = "other"
        if self._foreground_watcher:
            category = self._foreground_watcher.last_category or "other"

        sorted_rules = sorted(self._rules, key=lambda r: r.get("idle_min", 0), reverse=True)
        for rule in sorted_rules:
            required_idle = rule.get("idle_min", 0) * 60
            if conversation_idle < required_idle:
                continue
            fg_match = rule.get("foreground", ["*"])
            if "*" in fg_match or category in fg_match:
                weight = rule.get("weight", 0.5)
                if random.random() < weight:
                    prompt = rule.get("prompt", "")
                    if prompt:
                        self._cooldown_until = now + self._cooldown_minutes * 60
                        logger.info("Proactive triggered: idle=%ds fg=%s rule='%s'", int(conversation_idle), category, prompt)
                        self.on_proactive(prompt)
                        return prompt
        return None


# ════════════════════════════════════════════════════════════
#  统一感知控制器
# ════════════════════════════════════════════════════════════

class PerceptionController:
    """统一感知控制器 - 整合时间/情绪/日程/屏幕/主动对话 + M2 增强环境扫描

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

        # ── M2: 增强环境扫描器 ──
        self._env_scanner = None
        self._env_scanner_enabled = True
        try:
            from core.enhanced_environment import EnhancedEnvironmentScanner
            self._env_scanner = EnhancedEnvironmentScanner()
            logger.info("EnhancedEnvironmentScanner initialized for %s", character_id)
        except Exception as e:
            logger.warning("Failed to init EnhancedEnvironmentScanner: %s", e)

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

    @property
    def env_scanner(self):
        """M2: 暴露环境扫描器引用"""
        return self._env_scanner

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
        """每 30 秒调用，驱动情绪衰减 + 主动对话检查 + 日程刷新 + M2 环境扫描"""
        self._emotion.tick()
        if self._proactive:
            self._proactive.tick()
        now = time.time()
        if now - self._last_schedule_refresh > 600:
            self._schedule.refresh()
            self._last_schedule_refresh = now

        # ── M2: 定期刷新环境扫描快照 ──
        if self._env_scanner and self._env_scanner_enabled:
            try:
                self._scan_environment()
            except Exception as e:
                logger.debug("M2 env scan tick failed: %s", e)

    def _scan_environment(self):
        """M2: 扫描当前环境并更新上下文
        
        从 ForegroundWatcher 获取窗口标题，通过 EnhancedEnvironmentScanner
        解析为结构化快照，注入到 ScreenPerception 的 on_update 回调中。
        """
        try:
            # 尝试从前景窗口检测器获取最新标题
            fg_title = ""
            if hasattr(self, '_foreground_watcher') and self._foreground_watcher:
                fg_title = getattr(self._foreground_watcher, 'last_title', '') or ''
            elif hasattr(self._screen, '_foreground_watcher'):
                fw = self._screen._foreground_watcher
                if fw:
                    fg_title = getattr(fw, 'last_title', '') or ''
        except Exception:
            fg_title = ""

        # 时间上下文
        time_ctx = self._time.get_context()

        # 屏幕描述
        screen_desc = self._screen.last_description if self._screen else ""

        # 执行扫描
        snapshot = self._env_scanner.scan(
            window_title=fg_title,
            screen_description=screen_desc,
            time_context=time_ctx,
        )
        logger.debug("M2 env scan: app=%s cat=%s files=%s",
                     snapshot.foreground_app, snapshot.category, snapshot.detected_files)
        return snapshot

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

        # ── M2: 注入环境扫描观察 ──
        if self._env_scanner and self._env_scanner_enabled:
            try:
                # 从 ScreenPerception 获取最新的窗口标题
                fg_title = ""
                if hasattr(self._screen, '_foreground_watcher'):
                    fw = self._screen._foreground_watcher
                    if fw:
                        fg_title = getattr(fw, 'last_title', '') or ''
                if fg_title:
                    snapshot = self._env_scanner.scan(
                        window_title=fg_title,
                        screen_description=self._screen.last_description,
                        time_context=self._time.get_context(),
                    )
                    obs = self._env_scanner.get_observation(snapshot)
                    if obs:
                        parts.append(f"[环境观察] {obs}")
            except Exception as e:
                logger.debug("M2 build_context observation failed: %s", e)

        return "\n".join(parts) if parts else ""
