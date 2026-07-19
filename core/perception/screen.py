"""屏幕感知 — 后台定时截屏 + 视觉模型分析

关键点：
- 变化检测：对比上一帧 md5，相同则跳过 API 调用（节省算力）
- 失败退避：连续失败时指数拉长间隔（避免空打 API）
- 模糊处理：截图默认走 GaussianBlur，敏感信息降可读性
- 黑名单：密码管理器/锁屏/敏感关键词窗口 → 跳过
- 屏幕 → 情绪：SCREEN_EMOTION_MAP 命中关键词触发情绪回调

外部依赖：
- PIL.ImageGrab：屏幕截图
- requests：调视觉模型 API
- motion.foreground_watcher：前台窗口检测（黑名单/事件触发）
- env_config：视觉/LLM 配置
"""
from __future__ import annotations

import base64
import io
import json
import logging
import random
import re
import threading
import time

import requests
from PIL import ImageGrab

from .screen_types import ScreenEvent, ActivityEvent

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════
#  常量
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


# ════════════════════════════════════════════════════════════
#  屏幕感知主类
# ════════════════════════════════════════════════════════════

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

    def get_recent_activity_events(self, minutes: int = 60) -> list[ActivityEvent]:
        """获取最近 N 分钟的 ActivityEvent 列表（attribute 形式，供 UI 组件用）

        与 get_recent_activities 的区别：
        - get_recent_activities → list[dict]（给 LLM / 日报）
        - get_recent_activity_events → list[ActivityEvent]（给 UI 组件直接访问字段）
        """
        cutoff = time.time() - minutes * 60
        with self._lock:
            return [e for e in self._activity_history if e.start_time >= cutoff]

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
