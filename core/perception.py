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
import os
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
VISION_PROMPT = """分析用户当前屏幕内容，以 JSON 格式返回。

返回格式：
{
  "activity": "具体活动描述（英文，如 writing code / watching video / reading docs）",
  "category": "分类（work/learn/entertainment/communication/other）",
  "summary": "一句话中文摘要（20字以内）",
  "confidence": 0.0到1.0的置信度
}

规则：
- category 必须是 work / learn / entertainment / communication / other 之一
- confidence 反映你对判断的确信程度（看到明确内容=0.8+，模糊不清=0.3-0.5）
- 不要提及任何密码、验证码、密钥、token、银行账户等敏感信息
- 如果屏幕包含敏感信息，返回 {"activity": "private", "category": "other", "summary": "处理私密信息", "confidence": 0.9}
- 只返回 JSON，不要其他文字"""

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


class PetPermissions:
    """桌宠权限开关 — 控制各感知模块的启用/禁用

    所有开关默认开启，用户可通过设置面板关闭。
    关闭后对应模块降级或跳过。
    """

    def __init__(self):
        self.screenshot_enabled: bool = True       # 截图总开关
        self.diary_enabled: bool = True            # 日报总开关
        self.session_read_enabled: bool = True     # Session 读取总开关
        self.cross_session_enabled: bool = True    # 跨 Session 总开关
        self.tool_call_enabled: bool = True        # 插件工具调用总开关
        self.active_hours: tuple[int, int] = (6, 23)  # 活跃时段（默认 6:00-23:00）

    def is_in_active_hours(self) -> bool:
        """是否在活跃时段内"""
        from datetime import datetime
        hour = datetime.now().hour
        return self.active_hours[0] <= hour < self.active_hours[1]

    def to_dict(self) -> dict:
        return {
            "screenshot_enabled": self.screenshot_enabled,
            "diary_enabled": self.diary_enabled,
            "session_read_enabled": self.session_read_enabled,
            "cross_session_enabled": self.cross_session_enabled,
            "tool_call_enabled": self.tool_call_enabled,
            "active_hours": list(self.active_hours),
        }

    def load_from_dict(self, data: dict):
        """从配置加载"""
        for key in ('screenshot_enabled', 'diary_enabled', 'session_read_enabled',
                     'cross_session_enabled', 'tool_call_enabled'):
            if key in data:
                setattr(self, key, bool(data[key]))
        if 'active_hours' in data:
            ah = data['active_hours']
            if isinstance(ah, (list, tuple)) and len(ah) == 2:
                self.active_hours = (int(ah[0]), int(ah[1]))

    def get_status_text(self) -> str:
        """当前感知状态文本（展示给用户）"""
        parts = []
        parts.append("截图: " + ("✅" if self.screenshot_enabled else "❌"))
        parts.append("日报: " + ("✅" if self.diary_enabled else "❌"))
        parts.append("Session: " + ("✅" if self.session_read_enabled else "❌"))
        parts.append("跨Session: " + ("✅" if self.cross_session_enabled else "❌"))
        parts.append("工具调用: " + ("✅" if self.tool_call_enabled else "❌"))
        hour = "活跃时段" if self.is_in_active_hours() else "休息时段"
        parts.append(f"{hour} ({self.active_hours[0]}:00-{self.active_hours[1]}:00)")
        return " | ".join(parts)


# ── 隐私黑名单 ──────────────────────────────────────────

# 进程名黑名单（永不截图）
SCREENSHOT_PROCESS_BLACKLIST: set[str] = {
    # 密码管理器
    "1Password.exe", "KeePass.exe", "KeePassXC.exe", "Bitwarden.exe",
    "LastPass.exe", "Dashlane.exe",
    # 系统锁屏
    "LogonUI.exe",
}

# 窗口标题关键词黑名单（模糊匹配，命中则跳过）
SCREENSHOT_TITLE_BLACKLIST: list[str] = [
    "密码", "password", "密钥", "private key",
    "无痕", "incognito", "InPrivate",
    "登录", "login", "验证", "verification",
    "支付", "payment", "银行", "bank",
]


def _is_screen_blacklisted(app: str, title: str) -> bool:
    """检查前台窗口是否在截图黑名单中"""
    if app in SCREENSHOT_PROCESS_BLACKLIST:
        return True
    title_lower = title.lower()
    for keyword in SCREENSHOT_TITLE_BLACKLIST:
        if keyword.lower() in title_lower:
            return True
    return False


class ScreenEvent:
    """一次屏幕感知的结构化数据"""
    __slots__ = ('app', 'title', 'timestamp', 'mode', 'description')

    def __init__(self, app: str = "", title: str = "", timestamp: float = 0.0,
                 mode: str = "timer", description: str = ""):
        self.app = app              # 进程名（如 Obsidian.exe）
        self.title = title          # 窗口标题
        self.timestamp = timestamp  # time.time()
        self.mode = mode            # "timer" / "event" / "manual"
        self.description = description  # 视觉模型描述

    def to_dict(self) -> dict:
        return {
            "app": self.app, "title": self.title,
            "timestamp": self.timestamp, "mode": self.mode,
            "description": self.description,
        }


class ActivityEvent:
    """结构化活动事件（从视觉分析 JSON 提取）"""
    __slots__ = ('app', 'activity', 'category', 'summary', 'confidence',
                 'source', 'start_time', 'end_time')

    def __init__(self, app: str = "", activity: str = "", category: str = "other",
                 summary: str = "", confidence: float = 0.5, source: str = "vision",
                 start_time: float = 0.0, end_time: float = 0.0):
        self.app = app                # 应用名
        self.activity = activity      # 具体活动（如 "writing code", "watching video"）
        self.category = category      # 分类：work/learn/entertainment/communication/other
        self.summary = summary        # 一句话摘要
        self.confidence = confidence  # 置信度 0~1
        self.source = source          # "vision"（模型推断）/ "foreground"（窗口标题直接判断）
        self.start_time = start_time  # 开始时间
        self.end_time = end_time      # 结束时间（0 = 进行中）

    @property
    def duration_minutes(self) -> float:
        if self.start_time and self.end_time:
            return (self.end_time - self.start_time) / 60.0
        return 0.0

    def is_same_activity(self, other: 'ActivityEvent') -> bool:
        """判断两个事件是否是同一活动（用于合并）"""
        return (self.app == other.app and self.activity == other.activity
                and self.category == other.category)

    def to_dict(self) -> dict:
        return {
            "app": self.app, "activity": self.activity,
            "category": self.category, "summary": self.summary,
            "confidence": self.confidence, "source": self.source,
            "start_time": self.start_time, "end_time": self.end_time,
            "duration_minutes": round(self.duration_minutes, 1),
        }


class ScreenPerception:
    """屏幕感知 - 后台定时截屏 + 视觉模型分析

    优化：
    - 变化检测：对比上一帧 hash，相同则跳过 API 调用
    - 失败退避：连续失败时拉长间隔
    """

    MAX_CONSECUTIVE_FAILURES = 3
    BASE_BACKOFF_SECONDS = 60  # 基础退避时间
    MAX_BACKOFF_SECONDS = 600  # 最大退避时间（10分钟）

    def __init__(self, interval: int = 120):
        self._interval = interval
        self._base_interval = interval
        self._enabled = True  # 可通过配置禁用
        self._blur_enabled = True  # 截图模糊（隐私保护），可通过配置关闭
        self._running = False
        self._thread = None
        self._last_description: str = ""
        self._last_event: ScreenEvent | None = None  # 结构化元数据
        self._last_activity: ActivityEvent | None = None  # 结构化活动事件
        self._activity_history: list[ActivityEvent] = []  # 最近 50 个活动事件
        self._last_frame_hash: str = ""
        self._consecutive_failures: int = 0
        self._lock = threading.Lock()
        self.on_update: callable = lambda desc: None
        self.on_emotion: callable = lambda emotion, intensity: None
        self.on_screen_proactive: callable = lambda prompt: None  # 屏幕内容触发主动对话

    @property
    def last_description(self) -> str:
        with self._lock:
            return self._last_description

    def get_context(self) -> str:
        with self._lock:
            if self._last_description:
                return f"[屏幕画面：{self._last_description}]"
        return ""

    @property
    def last_event(self) -> ScreenEvent | None:
        """最近一次屏幕感知的结构化数据"""
        with self._lock:
            return self._last_event

    def capture_now(self, mode: str = "manual") -> ScreenEvent | None:
        """主动截图（不等待定时器）

        Args:
            mode: "manual"（用户主动） 或 "event"（前台切换触发）

        Returns:
            ScreenEvent 或 None（黑名单/失败时）
        """
        if not self._enabled:
            return None
        return self._capture_and_analyze(mode=mode)

    def on_foreground_change(self, app: str, category: str, title: str):
        """前台窗口切换时调用（由 ForegroundWatcher 触发）

        黑名单内 → 跳过
        变化不大 → 跳过（hash 检测）
        其他 → 触发一次截图
        """
        if _is_screen_blacklisted(app, title):
            logger.debug("Screenshot skipped (blacklisted): %s - %s", app, title[:30])
            return
        # 前台切换本身就是变化信号，直接触发截图
        self._capture_and_analyze(mode="event", app=app, title=title)

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
                # 定时截图时获取当前前台窗口信息（用于黑名单检查）
                try:
                    from motion.foreground_watcher import _get_foreground_process_name, _get_foreground_window_title
                    app = _get_foreground_process_name()
                    title = _get_foreground_window_title()
                except Exception:
                    app, title = "", ""
                self._capture_and_analyze(mode="timer", app=app, title=title)
            except Exception as e:
                logger.warning("ScreenPerception error: %s", e)
            for _ in range(self._interval):
                if not self._running:
                    return
                time.sleep(1)

    def _capture_and_analyze(self, mode: str = "timer", app: str = "", title: str = "") -> ScreenEvent | None:
        import hashlib as _hashlib
        from .hanako_context import HanakoContext

        # 黑名单检查（定时模式需要检查，事件模式已在 on_foreground_change 检查过）
        if mode == "timer":
            if app and title and _is_screen_blacklisted(app, title):
                logger.debug("Screenshot skipped (blacklisted): %s", app)
                return None

        img = ImageGrab.grab()
        new_size = (img.width // SCREENSHOT_SCALE, img.height // SCREENSHOT_SCALE)
        img = img.resize(new_size)
        
        # 隐私保护：对截图进行模糊处理（降低敏感信息可读性）
        if self._blur_enabled:
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
                raw = resp.json()["choices"][0]["message"].get("content", "").strip()
                if raw:
                    # 尝试解析 JSON（新版提示词返回结构化数据）
                    activity = self._parse_activity_json(raw, app or "")
                    # 保留自然语言描述用于兼容
                    description = activity.summary if activity else raw

                    event = ScreenEvent(
                        app=app or "",
                        title=title or "",
                        timestamp=time.time(),
                        mode=mode,
                        description=description,
                    )
                    with self._lock:
                        self._last_description = description
                        self._last_event = event
                        if activity:
                            self._last_activity = activity
                            self._activity_history.append(activity)
                            if len(self._activity_history) > 50:
                                self._activity_history.pop(0)
                    self._consecutive_failures = 0
                    self._interval = self._base_interval  # 恢复正常间隔
                    logger.info("Screen analysis [%s]: %s", mode, description[:50])
                    self.on_update(description)
                    # 触发屏幕情绪
                    self._detect_screen_emotion(description)
                    # 触发屏幕内容主动对话
                    self._check_screen_proactive(description)
                    return event
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

        # 失败退避：指数退避（连续失败时拉长间隔）
        if self._consecutive_failures >= self.MAX_CONSECUTIVE_FAILURES:
            backoff = min(self.BASE_BACKOFF_SECONDS * (2 ** (self._consecutive_failures - self.MAX_CONSECUTIVE_FAILURES)),
                         self.MAX_BACKOFF_SECONDS)
            self._interval = self._base_interval + backoff
            logger.warning("ScreenPerception backoff: interval=%ds (failures=%d, backoff=%ds)",
                         self._interval, self._consecutive_failures, backoff)
        return None

    def _parse_activity_json(self, raw: str, app: str) -> ActivityEvent | None:
        """解析视觉模型返回的 JSON，生成 ActivityEvent"""
        try:
            # 尝试提取 JSON（模型可能在 JSON 前后加文字）
            import re
            json_match = re.search(r'\{[^{}]+\}', raw)
            if not json_match:
                return None
            data = json.loads(json_match.group())

            valid_categories = {'work', 'learn', 'entertainment', 'communication', 'other'}
            category = data.get('category', 'other')
            if category not in valid_categories:
                category = 'other'

            return ActivityEvent(
                app=app,
                activity=data.get('activity', ''),
                category=category,
                summary=data.get('summary', ''),
                confidence=max(0.0, min(1.0, float(data.get('confidence', 0.5)))),
                source='vision',
                start_time=time.time(),
            )
        except (json.JSONDecodeError, ValueError, KeyError) as e:
            logger.debug("Failed to parse activity JSON: %s", e)
            return None

    def get_recent_activities(self, minutes: int = 60) -> list[dict]:
        """获取最近 N 分钟的活动事件（用于日报生成）"""
        cutoff = time.time() - minutes * 60
        with self._lock:
            return [e.to_dict() for e in self._activity_history if e.start_time >= cutoff]

    def get_activity_summary(self, minutes: int = 60) -> str:
        """获取活动摘要（注入 LLM prompt 用）"""
        activities = self.get_recent_activities(minutes)
        if not activities:
            return ""
        parts = []
        for a in activities[-5:]:  # 最近 5 个
            parts.append(f"{a['category']}: {a['summary']}")
        return "[近期活动：" + "；".join(parts) + "]"

    def _detect_screen_emotion(self, description: str):
        """根据屏幕内容触发情绪"""
        desc_lower = description.lower()
        for keyword, (emotion, intensity) in SCREEN_EMOTION_MAP.items():
            if keyword in desc_lower:
                logger.info("Screen emotion triggered: %s (%.1f) from '%s'", emotion, intensity, description[:30])
                self.on_emotion(emotion, intensity)
                return

    def _check_screen_proactive(self, description: str):
        """根据屏幕内容触发主动对话（使用 LLM 生成个性化回复）"""
        import random
        
        # 冷却检查（避免频繁触发）
        if not hasattr(self, '_last_screen_proactive'):
            self._last_screen_proactive = 0
        if time.time() - self._last_screen_proactive < 300:  # 5分钟冷却
            return
        
        # 随机触发（30%概率，不每次都打扰）
        if random.random() > 0.3:
            return
        
        # 构造带桌宠人格的提示词
        prompt = f"""你是一只可爱的桌宠，名叫月薪喵。你看到用户正在屏幕上做以下事情：

{description}

请用你的个性表达你的想法和感受，要求：
- 不超过两句话
- 语气活泼可爱
- 可以表达关心、好奇或鼓励
- 不要太啰嗦"""
        
        logger.info("Screen proactive prompt: %s", prompt[:100])
        self._last_screen_proactive = time.time()
        self.on_screen_proactive(prompt)


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
        ctrl = PerceptionController(character_id="yuexinmiao")
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

    def __init__(self, character_id: str = "yuexinmiao"):
        self._character_id = character_id
        self._time = TimePerception()
        self._emotion = EmotionStateMachine()
        self._schedule = SchedulePerception()
        self._screen = ScreenPerception()
        self._proactive: ProactiveScheduler | None = None
        self._last_schedule_refresh = 0.0
        self._permissions = PetPermissions()  # 权限开关

        # ── M2: 增强环境扫描器 ──
        self._env_scanner = None
        self._env_scanner_enabled = True
        try:
            from core.enhanced_environment import EnhancedEnvironmentScanner
            self._env_scanner = EnhancedEnvironmentScanner()
            logger.info("EnhancedEnvironmentScanner initialized for %s", character_id)
        except Exception as e:
            logger.warning("Failed to init EnhancedEnvironmentScanner: %s", e)

        # ── 手机活动感知（MacroDroid HTTP 上报） ──
        self._phone_activity = None
        self._phone_receiver = None
        self._phone_enabled = True
        try:
            from core.phone_activity import PhoneActivityPerception
            from core.phone_receiver import PhoneActivityReceiver
            self._phone_activity = PhoneActivityPerception()
            auth_token = os.environ.get('PHONE_AUTH_TOKEN', '')
            self._phone_receiver = PhoneActivityReceiver(self._phone_activity, auth_token=auth_token)
            self._phone_receiver.start()
            logger.info("PhoneActivityReceiver started on port %d", self._phone_receiver.port)
        except Exception as e:
            logger.warning("Failed to init PhoneActivityReceiver: %s", e)

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

    @property
    def phone_activity(self):
        """手机活动感知层（MacroDroid 上报）"""
        return self._phone_activity

    @property
    def phone_receiver(self):
        """手机活动 HTTP 接收器"""
        return self._phone_receiver

    @property
    def permissions(self) -> PetPermissions:
        """权限开关"""
        return self._permissions

    # ── 屏幕 ──

    def start_screen(self, interval: int = 120):
        if not self._permissions.screenshot_enabled:
            logger.info("Screen disabled by permissions")
            return
        self._screen._interval = interval
        self._screen.start()

    def stop_screen(self):
        self._screen.stop()

    def get_screen_context(self) -> str:
        return self._screen.get_context()

    # ── Session ──

    def get_current_session(self) -> dict:
        """获取当前 Session 摘要（不加载完整历史）"""
        if not self._permissions.session_read_enabled:
            return {}
        try:
            from .hanako_context import HanakoContext
            ctx = HanakoContext(self._character_id)
            return ctx.read_current_session()
        except Exception as e:
            logger.debug("Failed to read session: %s", e)
            return {}

    def get_session_context(self) -> str:
        """获取 Session 摘要文本（注入 LLM prompt 用）"""
        try:
            from .hanako_context import HanakoContext
            ctx = HanakoContext(self._character_id)
            return ctx.get_session_summary()
        except Exception:
            return ""

    def list_other_sessions(self, max_count: int = 10) -> list[dict]:
        """列出其他 Session（只读摘要）"""
        if not self._permissions.cross_session_enabled:
            return []
        try:
            from .hanako_context import HanakoContext
            ctx = HanakoContext(self._character_id)
            return ctx.list_sessions(max_count)
        except Exception:
            return []

    def get_cross_session_context(self) -> str:
        """获取跨 Session 摘要文本（注入 LLM prompt 用）"""
        try:
            from .hanako_context import HanakoContext
            ctx = HanakoContext(self._character_id)
            return ctx.get_cross_session_summary()
        except Exception:
            return ""

    # ── 日报生成 ──

    def generate_daily_diary(self, output_dir: str = "", preview_only: bool = False) -> str | None:
        """从活动事件生成日报 Markdown

        Args:
            output_dir: Obsidian 日记目录，默认 W:/Games/Obsidian/Work/无极限/03-日记/日常
            preview_only: True 则只返回 Markdown 内容，不写文件

        Returns:
            preview_only=True: Markdown 内容
            preview_only=False: 写入的文件路径
        """
        if not self._permissions.diary_enabled and not preview_only:
            logger.info("Diary disabled by permissions")
            return None
        from datetime import datetime
        now = datetime.now()
        date_str = now.strftime("%Y-%m-%d")
        time_str = now.strftime("%H:%M")

        # 获取今日活动（从 00:00 开始）
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        with self._screen._lock:
            today_activities = [
                e for e in self._screen._activity_history
                if e.start_time >= midnight
            ]

        if not today_activities:
            return None if not preview_only else "（今日无活动记录）"

        # 按分类分组
        categories = {
            'work': ('💼 工作', []),
            'learn': ('📚 学习', []),
            'entertainment': ('🎮 娱乐', []),
            'communication': ('💬 交流', []),
            'other': ('📌 其他', []),
        }
        for event in today_activities:
            cat = event.category if event.category in categories else 'other'
            categories[cat][1].append(event)

        # 生成 Markdown
        lines = [
            f"---",
            f"title: 桌宠日报 {date_str}",
            f"date: {date_str}",
            f"tags: [日报, 桌宠]",
            f"---",
            f"",
            f"# 桌宠日报 {date_str}",
            f"",
            f"生成时间：{time_str}",
            f"活动事件数：{len(today_activities)}",
            f"",
        ]

        for cat_key, (cat_label, events) in categories.items():
            if not events:
                continue
            lines.append(f"## {cat_label}")
            lines.append("")
            for e in events:
                start = datetime.fromtimestamp(e.start_time).strftime("%H:%M")
                confidence_mark = "" if e.confidence >= 0.7 else " ⚠️ 低置信度"
                duration = f" ({e.duration_minutes:.0f}分钟)" if e.duration_minutes > 0 else ""
                lines.append(f"- **{start}** {e.summary}{duration}{confidence_mark}")
                if e.app:
                    lines.append(f"  - 应用：{e.app}")
            lines.append("")

        # 时间缺口检测
        if len(today_activities) > 1:
            gaps = []
            for i in range(1, len(today_activities)):
                prev_end = today_activities[i-1].end_time or today_activities[i-1].start_time
                curr_start = today_activities[i].start_time
                gap_min = (curr_start - prev_end) / 60.0
                if gap_min > 30:  # 超过 30 分钟的缺口
                    gap_start = datetime.fromtimestamp(prev_end).strftime("%H:%M")
                    gap_end = datetime.fromtimestamp(curr_start).strftime("%H:%M")
                    gaps.append(f"{gap_start} ~ {gap_end}（{gap_min:.0f}分钟）")
            if gaps:
                lines.append("## ⏳ 时间缺口")
                lines.append("")
                for g in gaps:
                    lines.append(f"- {g}")
                lines.append("")

        lines.append(f"---")
        lines.append(f"*由桌宠自动生成*")
        content = "\n".join(lines)

        if preview_only:
            return content

        # 写入文件
        if not output_dir:
            output_dir = "W:/Games/Obsidian/Work/无极限/03-日记/日常"
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)
        filename = f"{date_str}-桌宠日报.md"
        filepath = output_path / filename
        filepath.write_text(content, encoding="utf-8")
        logger.info("Daily diary written: %s", filepath)
        return str(filepath)

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

        # ── 手机活动感知 ──
        if self._phone_activity and self._phone_enabled:
            try:
                phone_ctx = self._phone_activity.format_for_prompt()
                if phone_ctx:
                    parts.append(phone_ctx)
            except Exception as e:
                logger.debug("Phone activity build_context failed: %s", e)

        return "\n".join(parts) if parts else ""

    def get_perception_status(self) -> dict:
        """获取当前感知状态全貌（用于设置面板展示）"""
        return {
            "permissions": self._permissions.to_dict(),
            "screen": {
                "enabled": self._permissions.screenshot_enabled,
                "running": self._screen._running if self._screen else False,
                "last_description": self._screen.last_description[:50] if self._screen else "",
                "last_activity": self._screen._last_activity.to_dict() if self._screen and self._screen._last_activity else None,
            },
            "session": {
                "read_enabled": self._permissions.session_read_enabled,
                "cross_session_enabled": self._permissions.cross_session_enabled,
            },
            "emotion": {
                "current": self._emotion.current,
                "intensity": round(self._emotion.intensity, 2),
            },
            "diary": {
                "enabled": self._permissions.diary_enabled,
                "activity_count": len(self._screen._activity_history) if self._screen else 0,
            },
        }
