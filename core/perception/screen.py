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
from .screen_intent import (
    ScreenScene,
    classify_screen_scene,
    enrich_screen_scene,
)

logger = logging.getLogger(__name__)


# ════════════════════════════════════════════════════════════
#  常量
# ════════════════════════════════════════════════════════════

SCREENSHOT_SCALE = 4
JPEG_QUALITY = 50
VISION_PROMPT = """分析用户当前屏幕内容，以 JSON 格式返回。尽量详细，描述你看到的具体内容。

返回格式：
{
  "activity": "具体活动描述（英文，如 writing code in VS Code / watching Bilibili / playing Minecraft）",
  "category": "分类（work/learn/entertainment/communication/other）",
  "summary": "中文摘要（30-50字，描述具体在做什么、用什么应用、看到什么内容）",\n  "detail": "更详细的观察（50-80字，包括应用名、具体内容、界面状态等）",
  "confidence": 0.0到1.0的置信度
}

规则：
- category 必须是 work / learn / entertainment / communication / other 之一
- confidence 反映你对判断的确信程度（看到明确内容=0.8+，模糊不清=0.3-0.5）
- 不要提及任何密码、验证码、密钥、token、银行账户等敏感信息
- 如果屏幕包含敏感信息，返回 {"activity": "private", "category": "other", "summary": "处理私密信息", "confidence": 0.9}
- 只返回 JSON，不要其他文字"""


def build_vision_prompt(app: str = "", title: str = "") -> str:
    """构造发给视觉模型的提示词（P5：拼接窗口名提示）。

    app/title 至少有一个时，在 JSON 格式要求之后追加 [当前窗口] 提示，让视觉
    模型优先结合窗口名判断用户在做什么（避免多开/相似界面认错）；窗口名与截图
    内容矛盾时以窗口名为准。无窗口名时原样返回 VISION_PROMPT（不影响格式约束）。
    """
    if not app and not title:
        return VISION_PROMPT
    window_hint = (
        f"\n[当前窗口] 进程={app or '未知'}, 标题={title or '未知'}\n"
        "规则：窗口标题是最准确的信息，截图可能包含残留图标、菜单、无关窗口区域。"
        "窗口标题与截图内容矛盾时，绝对以窗口标题为准，并说明判断依据。"
    )
    return VISION_PROMPT + window_hint

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
    "错误": ("surprised", 0.45),
    "error": ("surprised", 0.45),
    "崩溃": ("surprised", 0.55),
    "crash": ("surprised", 0.55),
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


def _extract_json_object(text: str) -> dict | None:
    """从可能夹带文字/多个 JSON 的回复中提取第一个完整 JSON 对象。

    用括号配对 + 字符串引号感知扫描，支持嵌套对象/数组，
    比单层正则更稳健（视觉模型可能返回嵌套结构）。

    Returns:
        解析出的 dict，失败返回 None
    """
    if not text:
        return None
    start = text.find('{')
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == '\\':
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def _is_screen_blacklisted(app: str, title: str, enabled: bool = False) -> bool:
    """检查前台窗口是否在截图黑名单中（仅在 enabled=True 时生效）"""
    if not enabled:
        return False
    if app in SCREENSHOT_PROCESS_BLACKLIST:
        return True
    title_lower = title.lower()
    for keyword in SCREENSHOT_TITLE_BLACKLIST:
        if keyword.lower() in title_lower:
            return True
    return False


# ── 感知哈希（P1：整帧 MD5 之外的近邻去重）────────────────────────


def dhash_frame(img, size: int = 8) -> int:
    """差分感知哈希（dHash）：缩为 (size+1)×size 灰度，逐行比较水平相邻像素。

    返回 size*size 位整数（默认 64 位）。**近邻帧**（闪烁光标、跳动时钟、
    视频微动）哈希相近、Hamming 距离小——这正是整帧 MD5 完全抓不到的。

    失败（无 PIL / 图像异常）返回 -1，调用方应视为「不可用」直接放行。
    """
    try:
        from PIL import Image
        g = img.convert("L").resize((size + 1, size), Image.LANCZOS)
        px = list(g.getdata())
        bits = 0
        for row in range(size):
            base = row * (size + 1)
            for col in range(size):
                bits = (bits << 1) | (1 if px[base + col] > px[base + col + 1] else 0)
        return bits
    except Exception as exc:
        logger.debug("dhash_frame 失败（视为不可用）: %s", exc)
        return -1


def hamming(a: int, b: int) -> int:
    """两个整数位串的 Hamming 距离（不同位数）；任一不可用（<0）返回极大值。"""
    if a < 0 or b < 0:
        return 1 << 30
    return bin(a ^ b).count("1")


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
    MAX_CONSECUTIVE_EMPTY = 3  # 连续空响应阈值：超过则停用（避免无限空转）

    def __init__(self, interval: int = 120, agent_id: str = ""):
        self._interval = interval
        self._base_interval = interval
        # 2026-09-17：绑定 Hanako agent。视觉模型配置优先从该 agent 的
        # config.yaml 读 `models.vision`（与 models.chat 同级），
        # 未配置才回退 catalog 默认。原先这里不传 agent_id，导致
        # `HanakoContext()` 用默认值 yuexinmiao —— 读的是别人的配置。
        self._agent_id = (agent_id or "").strip()
        # 2026-09-14：对话避让钩子。由 pet.py 注入 `_is_conversation_busy`，
        # 为 None 时行为不变（不避让）。
        self.busy_check = None
        # 随机间隔范围（秒）：interval 为基准，实际每次截屏后在下限~上限间随机。
        # 默认基准 ± 30%，既防模式化打扰，也不至于等太久
        self._interval_min = int(interval * 0.7)
        self._interval_max = int(interval * 1.3)
        self._enabled = True  # 屏幕感知总开关（默认开）
        self._blur_enabled = False  # 高斯模糊（默认关，需要时手动开）
        self._blacklist_enabled = False  # 敏感窗口黑名单（默认关，需要时手动开）
        self._compress_enabled = True  # 缩放+压缩（默认开，关掉则原图发送）
        self._running = False
        self._thread = None
        self._last_description: str = ""
        self._last_event: ScreenEvent | None = None  # 结构化元数据
        self._last_activity: ActivityEvent | None = None  # 结构化活动事件
        self._last_event_capture: float = 0          # 最近一次 event 截图时间
        self._last_timer_capture: float = 0          # 最近一次 timer 截图时间
        self._activity_history: list[ActivityEvent] = []  # 最近 50 个活动事件
        self._last_frame_hash: str = ""
        # O1-P3（2026-09-19）：事件爆发合并（去抖，借鉴 Cortico batching）。
        # 关掉则回到旧行为（第一个事件立刻分析、其余在冷却期内丢弃）。
        # 导入失败也回退旧行为——合并是省钱项，不该拖垮屏幕感知本身。
        self._burst_enabled = False
        self._burst = None
        self._burst_timer = None
        self._burst_lock = threading.Lock()
        self._last_burst = None
        try:
            from core.perception.event_burst import EventBurstCoalescer

            self._burst = EventBurstCoalescer()
            self._burst_enabled = True
        except Exception as e:  # noqa: BLE001 — 合并是省钱项，失败就回退旧行为
            logger.warning("屏幕事件合并不可用（回退旧行为）: %s", e)
        # P1：感知哈希近邻去重（0=关，默认关）。整帧 MD5 之外再挡一层
        # 「像素变了但画面没变」（闪烁光标 / 跳动时钟 / 视频微动）。
        self._phash_threshold: int = 0
        self._last_phash: int | None = None  # 上次**真正分析过**那帧的 dHash（跳过时不更新）
        self._phash_skips: int = 0           # 连续跳过次数（仅用于日志/观测）
        self._consecutive_failures: int = 0
        self._consecutive_empty: int = 0  # 连续空响应计数
        # 视觉模型配置状态（避免未配置/配置错误时反复请求刷屏）
        self._vision_disabled: bool = False            # 400/401 后本次会话停用
        self._vision_skip_logged: bool = False          # 未配置时只提示一次
        self._vision_config_error_logged: bool = False  # 配置错误时只提示一次
        # 屏幕情绪冷却：避免同一类屏幕内容反复触发情绪（尤其是 surprised）
        self._last_screen_emotion_time: float = 0.0
        self._screen_emotion_cooldown: int = 45  # 秒
        self._lock = threading.Lock()
        self.on_update: callable = lambda desc: None
        self.on_emotion: callable = lambda emotion, intensity: None
        self.on_screen_proactive: callable = lambda prompt: None  # 屏幕内容触发主动对话
        # P1-6 屏幕/意图感知升级：场景分类 + LLM 语义增强（可选）
        self._last_scene: ScreenScene | None = None          # 最近一次场景分类结果
        self._llm_enrich: bool = True                        # LLM 语义增强开关（config screen.llm_enrich）
        self._enrich_provider: callable | None = None        # callable(prompt) -> str | None（走 Hanako source="screen_enrich"）
        self.on_scene: callable = lambda scene: None         # 场景分类回调（规则结果，与 on_update 同线程）
        # 429 限流缓解：LLM 语义增强冷却（秒）。视觉分析每次截图本来就打一次 API，
        # 若每次成功都再打一次 enrich，等于每 2 分钟 2 次 LLM 请求——高频期很容易 429。
        # 默认 300s：场景未变化时最多 5 分钟打一次 enrich；场景变化立即补一次（保持灵敏）。
        self._enrich_cooldown: int = 300
        self._last_enrich_at: float = 0.0
        self._last_enriched_scene: str = ""

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

    # ── P1-6 屏幕/意图感知升级：场景分类 + LLM 增强 ──

    @property
    def last_scene(self) -> ScreenScene | None:
        """最近一次场景分类结果（ScreenScene；无结果返回 None）。"""
        with self._lock:
            return self._last_scene

    def get_scene_snapshot(self) -> dict | None:
        """场景快照（dict 形式，供 proactive/focus 读取；无结果返回 None）。"""
        with self._lock:
            scene = self._last_scene
            return scene.to_dict() if scene is not None else None

    def set_llm_enrich(self, enabled: bool):
        """开关 LLM 语义增强（默认开；未注入 provider 时自动退化为规则）。"""
        self._llm_enrich = bool(enabled)

    def set_enrich_provider(self, provider: callable | None):
        """注入 LLM 语义增强提供函数 ``fn(prompt: str) -> str | None``。

        生产环境建议包装 Hanako 适配器：``adapter.chat(prompt, inject_memory=False,
        source="screen_enrich")`` 返回文本；None=关闭增强（纯规则分类）。
        """
        self._enrich_provider = provider

    def set_enrich_cooldown(self, seconds: int) -> None:
        """配置 LLM 语义增强冷却（秒，429 限流缓解）。

        场景未变化时最多每 ``seconds`` 秒补一次 enrich；场景变化立即补（保持灵敏）。
        过小输入（<30）钳到 30s 下限（避免把感知增强彻底关掉）；
        无效（非数字/空）回退默认 300s。
        """
        try:
            val = int(seconds or 300)
        except (TypeError, ValueError):
            val = 300
        self._enrich_cooldown = max(30, val)

    def _should_enrich(self, scene: ScreenScene | None, now: float) -> bool:
        """是否应发起一次 LLM 语义增强（冷却 + 场景变化判断）。

        2026-09-14 避让：**对话进行中不发起 enrich**。
        实测屏幕感知与用户回复抢同一条 API（日志：`Vision API timeout` 连续三次，
        与回复同时段），用户感知为"回复变慢"。
        场景还在那里，等对话结束再分析不迟。

        Returns:
            True=放行（并已记录本次 enrich 时间/场景，供下次判断）。
        """
        if scene is None:
            return False
        # 避让：对话进行中先不做 LLM 增强（不消耗 _last_enrich_at，
        # 这样对话结束后可以立即补一次，不会被冷却拖住）
        busy = getattr(self, "busy_check", None)
        if callable(busy):
            try:
                if busy():
                    logger.debug("Screen enrich 避让：对话进行中")
                    return False
            except Exception:
                logger.debug("busy_check 失败，按不忙处理", exc_info=True)
        scene_changed = (scene.scene or "") != getattr(self, "_last_enriched_scene", "")
        if scene_changed or (now - self._last_enrich_at >= self._enrich_cooldown):
            self._last_enrich_at = now
            self._last_enriched_scene = scene.scene or ""
            return True
        logger.debug("Screen enrich skipped (cooldown %.0fs, scene '%s' unchanged)",
                     self._enrich_cooldown, scene.scene)
        return False

    def _classify_activity(self, activity: ActivityEvent, app: str, title: str) -> ScreenScene:
        """对一次 ActivityEvent 做纯规则场景分类（P1-6）。

        在截图后台线程执行（同步、廉价、无 I/O）。时间上下文从 TimePerception
        读取；任何异常回退 other（不阻塞感知）。
        """
        period = "other"
        hour = 12
        weekday = 0
        is_weekend = False
        fg_duration_min = getattr(self, "_fg_duration_min", 0.0) or 0.0
        try:
            from .time import TimePerception
            tctx = TimePerception().get_context()
            period = tctx.get("period", "other")
            hour = int(tctx.get("hour", 12) or 12)
            weekday = int(tctx.get("weekday", 0) or 0)
            is_weekend = bool(tctx.get("is_weekend", False))
        except Exception as e:
            # 时间上下文读取失败 → 场景分类退化为通用（影响主动对话时机）
            logger.warning("screen: 时间上下文读取失败，场景分类将退化: %s", e)
        try:
            return classify_screen_scene(
                category=activity.category or "other",
                activity=getattr(activity, "activity", "") or "idle",
                period=period,
                hour=hour,
                weekday=weekday,
                is_weekend=is_weekend,
                fg_duration_min=fg_duration_min,
                app=app or "",
                title=title or "",
                description=getattr(activity, "summary", "") or "",
                detail=getattr(activity, "detail", "") or "",
            )
        except Exception as exc:
            logger.debug("Screen scene classify failed: %s", exc)
            return ScreenScene(
                scene="other", intent="work", confidence=0.4,
                category=activity.category or "other",
                activity=getattr(activity, "activity", "") or "idle",
                period=period,
            )

    def _launch_enrichment(self, activity: ActivityEvent, scene: ScreenScene, app: str, title: str):
        """后台线程做 LLM 语义增强（不阻塞感知循环）。

        失败/超时/解析错误 → enrich_screen_scene 返回原规则结果（不丢场景）。
        只更新数据 + 发事件总线（不触碰 UI/COM）；UI 侧如有需要请订阅
        ``screen_scene_enriched`` 事件并在主线程处理。
        """
        if self._enrich_provider is None:
            return

        def _worker():
            # O1-P2：语义增强同样过全局闸门（冷却中 / 超预算就跳过这一次增强，
            # 保留规则结果，不丢场景）
            from core.llm_gate import get_gate

            gate = get_gate()
            if not gate.acquire("enrich"):
                logger.debug("屏幕语义增强被全局闸门跳过（本轮保留规则结果）")
                return
            try:
                extra = {
                    "app": app or "",
                    "title": title or "",
                    "summary": getattr(activity, "summary", "") or "",
                    "detail": getattr(activity, "detail", "") or "",
                    "fg_duration_min": getattr(self, "_fg_duration_min", 0.0) or 0.0,
                }
                merged = enrich_screen_scene(scene, self._enrich_provider, **extra)
            except Exception as exc:
                logger.debug("Screen enrich worker failed: %s", exc)
                return
            finally:
                gate.release()
            if merged is None or merged.source == "rule":
                return  # 增强失败 → 保留规则结果，不发事件
            with self._lock:
                self._last_scene = merged
                if activity is not None:
                    activity.scene = merged.scene
                    activity.scene_confidence = merged.confidence
                    activity.scene_propensity = merged.propensity
                    activity.scene_source = merged.source
            try:
                from core.event_bus import EventBus
                EventBus.emit("screen_scene_enriched", scene=merged)
            except Exception as exc:
                logger.debug("screen_scene_enriched emit failed: %s", exc)

        threading.Thread(
            target=_worker,
            daemon=True,
            name="ScreenEnrich",
        ).start()

    def capture_now(self, mode: str = "manual") -> ScreenEvent | None:
        """主动截图（不等待定时器）

        Args:
            mode: "manual"（用户主动） 或 "event"（前台切换触发）

        Returns:
            ScreenEvent 或 None（黑名单/失败时）

        注意：这是同步调用，内部会做 Vision API 请求（最长 timeout=30s）。
        从主线程/对话线程调用时可能阻塞，请用 capture_async。
        """
        if not self._enabled:
            return None
        return self._capture_and_analyze(mode=mode)

    def capture_async(self, mode: str = "manual"):
        """异步主动截图：后台线程执行，不阻塞调用方。

        结果通过 on_update / on_emotion / on_screen_proactive 回调返回。
        用于主线程/对话线程触发截图，避免 Vision API 阻塞 UI 或消息队列。
        """
        if not self._enabled:
            return
        threading.Thread(
            target=self._capture_and_analyze,
            args=(mode,),
            daemon=True,
            name="ScreenCaptureAsync",
        ).start()

    def on_foreground_change(self, app: str, category: str, title: str):
        """前台窗口切换时调用（由 ForegroundWatcher 触发）

        黑名单内 → 跳过
        开合并 → 攒进爆发，等安静后**只打一次**视觉 API
        关合并 → 旧行为：立刻分析，冷却期内丢弃
        """
        if _is_screen_blacklisted(app, title, self._blacklist_enabled):
            logger.debug("Screenshot skipped (blacklisted): %s - %s", app, title[:30])
            return
        if not (self._burst_enabled and self._burst is not None):
            # 旧行为（保留可回退）：事件触发也加冷却
            now = time.time()
            if not hasattr(self, '_last_event_capture'):
                self._last_event_capture = 0
            if now - self._last_event_capture < self._interval:
                return
            self._last_event_capture = now
            self._capture_and_analyze(mode="event", app=app, title=title)
            return
        try:
            flush_now, delay = self._burst.add(app, title, category)
        except Exception as e:  # noqa: BLE001 — 合并出问题不能吞掉事件路径
            logger.warning("屏幕爆发合并失败（回退本次立刻分析）: %s", e)
            self._capture_and_analyze(mode="event", app=app, title=title)
            return
        if flush_now:
            self._flush_burst()
        else:
            self._schedule_burst_flush(delay)

    def _schedule_burst_flush(self, delay: float):
        """排一次冲刷；**每次事件都重排**——这就是去抖：事件越密、动手越晚。"""
        with self._burst_lock:
            old = self._burst_timer
            timer = threading.Timer(max(0.05, float(delay)), self._flush_burst)
            timer.daemon = True
            timer.name = "ScreenBurstFlush"
            self._burst_timer = timer
        # 取消放锁外，避免 cancel 回调里再抢锁
        if old is not None:
            try:
                old.cancel()
            except Exception:  # noqa: BLE001
                pass
        timer.start()

    def set_burst(self, enabled: bool = True, **kw) -> None:
        """改事件合并参数（quiet_gap_s / min_age_s / max_age_s / max_size）。

        不给配置块是有意的：没接进读取链路的配置键就是"装饰品"
        （这个项目里已经踩过 `auth_token` 那个坑）。要用时调这里。
        """
        try:
            from core.perception.event_burst import EventBurstCoalescer

            self._burst = EventBurstCoalescer(**kw)
            self._burst_enabled = bool(enabled)
        except Exception as e:  # noqa: BLE001
            logger.warning("设置事件合并参数失败（保持原状）: %s", e)

    def cancel_pending_burst(self) -> None:
        """丢弃还没冲的爆发（停止感知时调，避免停完又打一条 API）。"""
        with self._burst_lock:
            timer = self._burst_timer
            self._burst_timer = None
        if timer is not None:
            try:
                timer.cancel()
            except Exception:  # noqa: BLE001
                pass
        if self._burst is not None:
            try:
                self._burst.reset()
            except Exception:  # noqa: BLE001
                pass

    def _flush_burst(self):
        """把攒下的事件合并成**一次**分析。"""
        try:
            with self._burst_lock:
                self._burst_timer = None
            if not self._running:
                return
            burst = self._burst.take() if self._burst is not None else None
            if burst is None or not burst.events:
                return
            last = burst.last
            # 定时器路径仍按 _last_event_capture 避让，不变
            self._last_event_capture = time.time()
            logger.info("屏幕爆发合并：%d 个事件 / %.1fs → %s",
                        burst.size, burst.duration, burst.window_chain())
            self._capture_and_analyze(mode="event", app=last.app,
                                      title=last.title, burst=burst)
        except Exception as e:  # noqa: BLE001
            logger.warning("屏幕爆发冲刷失败（非致命）: %s", e)

    def start(self):
        if not self._enabled:
            logger.info("ScreenPerception disabled by config")
            return
        self._running = True
        # 重新启动时重置视觉模型配置状态，允许用户在设置面板修正后恢复
        self._vision_disabled = False
        self._vision_skip_logged = False
        self._vision_config_error_logged = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logger.info("ScreenPerception started | interval=%ds", self._interval)

    def set_interval(self, interval: int):
        """设置基准间隔（秒），随机范围随之浮动。"""
        self._interval = max(30, int(interval))
        self._base_interval = self._interval
        self._interval_min = max(30, int(round(interval * 0.7)))
        self._interval_max = max(45, int(round(interval * 1.3)))

    def set_interval_range(self, min_sec: int, max_sec: int):
        """设置随机间隔范围（秒）。手动配置时用这个；基准取中点，退避逻辑不受影响。"""
        lo = max(30, int(min_sec))
        hi = max(lo + 1, int(max_sec))
        self._interval_min = lo
        self._interval_max = hi
        self._interval = (lo + hi) // 2
        self._base_interval = self._interval

    def _next_interval(self) -> int:
        """随机挑下一个截屏间隔（在下限~上限之间均匀分布）。"""
        lo, hi = self._interval_min, self._interval_max
        if hi <= lo:
            return lo
        import random
        return random.randint(lo, hi)

    def disable(self):
        """禁用屏幕感知"""
        self._enabled = False
        self.stop()

    def enable(self):
        """启用屏幕感知"""
        self._enabled = True

    def set_blur(self, enabled: bool):
        """开关高斯模糊"""
        self._blur_enabled = enabled

    def set_blacklist(self, enabled: bool):
        """开关敏感窗口黑名单"""
        self._blacklist_enabled = enabled

    def set_compress(self, enabled: bool):
        """开关缩放+压缩（True=缩放4x+50%压缩，False=原图+85%压缩）"""
        self._compress_enabled = enabled

    def set_phash_threshold(self, threshold: int):
        """感知哈希近邻去重的 Hamming 阈值（0=关闭，默认关）。

        整帧 MD5 只能挡「一个像素都没变」；本阈值挡「像素变了、画面没变」
        （闪烁光标 / 跳动时钟 / 视频微动）——正对屏幕感知白烧 LLM 与 429 限流。

        建议 3~6：越大越省，但过大会漏掉局部小变化（如弹窗突然出现）。
        无效输入回退 0（关闭）；置 0 同时清空历史哈希。
        """
        try:
            t = int(threshold)
        except (TypeError, ValueError):
            t = 0
        self._phash_threshold = max(0, min(64, t))
        if self._phash_threshold == 0:
            self._last_phash = None

    def _is_perceptually_unchanged(self, phash: int) -> bool:
        """近邻判定（含 ``_last_phash`` 维护）。不可用/阈值 0 → 恒 False（放行）。

        对比对象是**上次真正分析过**的那帧（跳过时不更新），因此缓慢渐变会
        累积到超过阈值 → 自然触发一次分析，不会无限跳过。
        """
        if self._phash_threshold <= 0 or phash < 0:
            self._last_phash = phash if phash >= 0 else None
            self._phash_skips = 0
            return False
        prev = self._last_phash
        if prev is not None and hamming(phash, prev) <= self._phash_threshold:
            self._phash_skips += 1
            return True
        self._last_phash = phash
        self._phash_skips = 0
        return False

    def stop(self):
        self._running = False

    def _run(self):
        time.sleep(10)  # 首次延迟
        while self._running:
            try:
                # 若 event 触发刚截过图（冷却期内），timer 也跳过本轮——
                # 避免"切窗口一次 + 定时一次"背靠背打两条视觉 API
                now = time.time()
                last_any = max(
                    getattr(self, '_last_event_capture', 0),
                    getattr(self, '_last_timer_capture', 0),
                )
                if now - last_any < self._interval:
                    # 仍在冷却：按剩余时间 sleep，然后继续下轮
                    remain = int(self._interval - (now - last_any))
                    for _ in range(max(1, remain)):
                        if not self._running:
                            return
                        time.sleep(1)
                    continue
                # 定时截图时获取当前前台窗口信息（用于黑名单检查）
                try:
                    from motion.foreground_watcher import _get_foreground_process_name, _get_foreground_window_title
                    app = _get_foreground_process_name()
                    title = _get_foreground_window_title()
                except Exception as e:
                    # 失败 = 黑名单与窗口互动失效（截图照常，但判断基础没了）
                    app, title = "", ""
                    _seen = getattr(self, "_fg_err_reported", None)
                    if _seen is None:
                        _seen = self._fg_err_reported = set()
                    if str(e) not in _seen:
                        _seen.add(str(e))
                        logger.warning("screen: 前台窗口信息获取失败，黑名单/窗口互动将失效: %s", e)
                self._capture_and_analyze(mode="timer", app=app, title=title)
                self._last_timer_capture = time.time()
                # 随机化：每次截屏后重新掷下一个间隔，避免固定 120s 的机械感
                next_iv = self._next_interval()
                if next_iv != self._interval:
                    logger.debug("ScreenPerception next interval=%ds (range %d-%d)",
                                 next_iv, self._interval_min, self._interval_max)
                    self._interval = next_iv
            except Exception as e:
                logger.warning("ScreenPerception error: %s", e)
            for _ in range(self._interval):
                if not self._running:
                    return
                time.sleep(1)

    def _capture_and_analyze(self, mode: str = "timer", app: str = "", title: str = "",
                             burst=None) -> ScreenEvent | None:
        import hashlib as _hashlib
        from core.hanako_context import HanakoContext

        # O1-P3：记住本次是哪个爆发触发的。序列文本在 burst.hint() 里，
        # 目前用于日志与观测；**接进视觉提示词是下一步**（提示词不在本文件里）。
        if burst is not None:
            self._last_burst = burst
            try:
                logger.info("屏幕分析附带爆发序列：%s", burst.hint())
            except Exception:  # noqa: BLE001
                pass

        # 视觉模型配置错误（400/401）后本次会话停用，避免反复请求刷屏
        if getattr(self, "_vision_disabled", False):
            return None

        # 黑名单检查（定时模式需要检查，事件模式已在 on_foreground_change 检查过）
        if mode == "timer":
            if app and title and _is_screen_blacklisted(app, title, self._blacklist_enabled):
                logger.debug("Screenshot skipped (blacklisted): %s", app)
                return None

        img = ImageGrab.grab()
        if self._compress_enabled:
            new_size = (img.width // SCREENSHOT_SCALE, img.height // SCREENSHOT_SCALE)
            img = img.resize(new_size)

        # 隐私保护：对截图进行模糊处理（降低敏感信息可读性）
        if self._blur_enabled:
            try:
                from PIL import ImageFilter
                img = img.filter(ImageFilter.GaussianBlur(radius=2))
            except Exception:
                logger.debug("screen: 非致命异常(已静默吞掉)", exc_info=True)

        # 变化检测：对比上一帧 hash
        frame_hash = _hashlib.md5(img.tobytes()).hexdigest()
        if frame_hash == self._last_frame_hash:
            logger.debug("Screen unchanged, skipping API call")
            return
        self._last_frame_hash = frame_hash

        # P1 近邻去重（整帧 MD5 之外的一层）：像素变了但画面没变时不打 API。
        if self._phash_threshold > 0 and self._is_perceptually_unchanged(dhash_frame(img)):
            logger.debug("Screen perceptually unchanged (hamming<=%d, skips=%d), skipping API call",
                         self._phash_threshold, self._phash_skips)
            return

        buf = io.BytesIO()
        quality = JPEG_QUALITY if self._compress_enabled else 85
        img.save(buf, format="JPEG", quality=quality)
        b64 = base64.b64encode(buf.getvalue()).decode()
        size_info = img.size if not self._compress_enabled else (img.width, img.height)
        logger.debug("Screenshot: %s, %dKB base64", size_info, len(b64) // 1024)

        ctx = HanakoContext(self._agent_id) if self._agent_id else HanakoContext()

        # 优先使用视觉专用模型配置（.env → agent config 的 models.vision → catalog）
        from env_config import get_vision_config, get_llm_config
        vision_cfg = get_vision_config(self._agent_id)

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

        # 视觉模型（含 base_url / api_key / model）三者齐全才算可用；
        # 缺失则视为未配置，暂停屏幕感知并仅提示一次，不打 ERROR 刷屏。
        if not (api_url and api_key and model):
            if not self._vision_skip_logged:
                logger.info(
                    "屏幕感知已暂停：未配置可用的视觉模型"
                    "（在设置面板填写视觉模型/Key，或保持留空以关闭屏幕感知）。"
                )
                self._vision_skip_logged = True
            return None

        # P5: 窗口名拼接进视觉提示（视觉模型优先结合窗口名判断，避免多开/相似界面认错）
        vision_text = build_vision_prompt(app, title)

        # O1-P2 全局闸门：冷却中 / 超预算 / 并发已满 → 本轮直接跳过。
        # 屏幕感知是后台循环，晚一轮没有代价；三条线程一起撞上去才是 429 的成因。
        from core.llm_gate import get_gate

        gate = get_gate()
        if not gate.acquire("vision"):
            _ok, _why = gate.check("vision")
            self._gate_skips = getattr(self, "_gate_skips", 0) + 1
            if self._gate_skips <= 3 or self._gate_skips % 20 == 0:
                logger.info("屏幕感知被全局闸门跳过（%s，累计 %d 次）——等下个周期",
                            _why or "busy", self._gate_skips)
            return None

        try:
            resp = requests.post(
                api_url,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": [
                        {"type": "text", "text": vision_text},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                    ]}],
                    "max_tokens": 1000,
                    "temperature": 0.3,
                },
                timeout=30,
            )
            if resp.status_code == 200:
                gate.notify_ok()
                raw = resp.json()["choices"][0]["message"].get("content", "").strip()
                if raw:
                    # 尝试解析 JSON（新版提示词返回结构化数据）
                    activity = self._parse_activity_json(raw, app or "")
                    # 保留自然语言描述用于兼容
                    description = activity.summary if activity else raw

                    # P1-6 屏幕/意图感知升级：场景分类（规则，永不阻塞；失败回退 other）
                    scene = None
                    if activity:
                        scene = self._classify_activity(activity, app or "", title or "")
                        activity.scene = scene.scene
                        activity.scene_confidence = scene.confidence
                        activity.scene_propensity = scene.propensity
                        activity.scene_source = scene.source

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
                        if scene is not None:
                            self._last_scene = scene
                    # A 记忆地基：活动事件 → 事件总线（PetWindow 订阅后写事件流）。
                    # 注意：summary/detail 文本不进流（隐私约束），只发结构化事件。
                    try:
                        if activity:
                            from core.event_bus import EventBus
                            EventBus.emit("activity_event", event=activity)
                    except Exception as e:
                        logger.debug("activity_event emit failed: %s", e)

                    # P1-6 屏幕场景 → 事件总线 + 回调 + 可选 LLM 增强（后台线程，失败回退规则）
                    if scene is not None:
                        try:
                            from core.event_bus import EventBus
                            EventBus.emit("screen_scene", scene=scene)
                        except Exception as e:
                            logger.debug("screen_scene emit failed: %s", e)
                        try:
                            self.on_scene(scene)
                        except Exception as e:
                            logger.debug("on_scene callback failed: %s", e)
                        # LLM 语义增强：加冷却（429 限流缓解）——场景变化立即补一次，
                        # 场景未变化时最多每 _enrich_cooldown 秒补一次，避免每次截图都打 LLM。
                        if self._llm_enrich and self._should_enrich(scene, time.time()):
                            self._launch_enrichment(activity, scene, app or "", title or "")
                    self._consecutive_failures = 0
                    self._consecutive_empty = 0  # 成功一次即重置空响应计数
                    self._interval = self._next_interval()  # 恢复正常（随机）间隔
                    logger.info("Screen analysis [%s]: %s", mode, description[:50])
                    self.on_update(description)
                    # 触发屏幕情绪
                    self._detect_screen_emotion(description)
                    # 触发屏幕内容主动对话（传入 detail 字段）
                    detail_text = getattr(activity, 'detail', '') if activity else ''
                    self._check_screen_proactive(description, detail=detail_text)
                    return event
                else:
                    # 空响应：模型返回 200 但无 content。
                    # 可能是模型不支持图像输入 / 返回格式异常，不是单纯网络故障。
                    # 连续空响应达到阈值则停用，避免每个周期空打 API 无限空转。
                    self._consecutive_empty += 1
                    self._consecutive_failures += 1
                    if self._consecutive_empty >= self.MAX_CONSECUTIVE_EMPTY:
                        if not self._vision_skip_logged:
                            logger.warning(
                                "屏幕感知已停用：视觉模型连续 %d 次返回空内容。"
                                "通常当前模型不支持图像输入，或视觉模型配置有误。"
                                "请在设置面板检查视觉模型，或保持留空以关闭屏幕感知。",
                                self.MAX_CONSECUTIVE_EMPTY,
                            )
                        self._vision_disabled = True
                        return None
                    logger.warning(
                        "Vision API 返回空内容 (%d/%d)：模型可能不支持图像输入",
                        self._consecutive_empty, self.MAX_CONSECUTIVE_EMPTY,
                    )
            else:
                # 400/401 属于配置问题（模型不存在 / Key 无效），不是临时故障；
                # 停用屏幕感知避免每个周期重复请求刷屏，仅提示一次。
                if resp.status_code in (400, 401):
                    if not self._vision_config_error_logged:
                        logger.warning(
                            "屏幕感知已停用：视觉 API 返回 %d"
                            "(通常是视觉模型/Key 未正确配置)。"
                            "请在设置面板检查视觉模型，或保持留空以关闭屏幕感知。",
                            resp.status_code,
                        )
                        self._vision_config_error_logged = True
                    self._vision_disabled = True
                    return None
                if resp.status_code == 429:
                    # 限流信号：不要等累计 3 次失败才退避，立即拉长轮询间隔，
                    # 避免 429 期间每个周期仍打 API 反复撞墙（深度分析04 方案 C）。
                    # O1-P2：同时通知全局闸门——让**所有**后台源一起收手，
                    # 而不是只有屏幕这一条在退避、另两条继续撞。
                    gate.notify_429("vision")
                    self._consecutive_failures += 1
                    self._interval = self._next_interval() + self.BASE_BACKOFF_SECONDS
                    logger.warning(
                        "屏幕感知遇 429 限流，立即退避: interval=%ds",
                        self._interval,
                    )
                    return None
                logger.warning("Vision API error: %d", resp.status_code)
                self._consecutive_failures += 1
        except requests.exceptions.Timeout:
            logger.warning("Vision API timeout")
            self._consecutive_failures += 1
        except Exception as e:
            logger.warning("Vision analysis failed: %s", e)
            self._consecutive_failures += 1
        finally:
            # 并发槽只盖住这次视觉调用（成功/失败/异常都归还）
            gate.release()

        # 失败退避：指数退避（连续失败时拉长间隔）
        if self._consecutive_failures >= self.MAX_CONSECUTIVE_FAILURES:
            backoff = min(self.BASE_BACKOFF_SECONDS * (2 ** (self._consecutive_failures - self.MAX_CONSECUTIVE_FAILURES)),
                         self.MAX_BACKOFF_SECONDS)
            self._interval = self._next_interval() + backoff  # 随机间隔基础上拉长
            logger.warning("ScreenPerception backoff: interval=%ds (failures=%d, backoff=%ds)",
                         self._interval, self._consecutive_failures, backoff)
        return None

    def _parse_activity_json(self, raw: str, app: str) -> ActivityEvent | None:
        """解析视觉模型返回的 JSON，生成 ActivityEvent"""
        try:
            # 提取 JSON（模型可能在 JSON 前后加文字）。
            # 用括号配对扫描替代单层正则：支持嵌套 JSON（detail 等字段可能含对象）。
            data = _extract_json_object(raw)
            if data is None:
                return None

            valid_categories = {'work', 'learn', 'entertainment', 'communication', 'other'}
            category = data.get('category', 'other')
            if category not in valid_categories:
                category = 'other'

            return ActivityEvent(
                app=app,
                activity=data.get('activity', ''),
                category=category,
                summary=data.get('summary', ''),
                detail=data.get('detail', ''),
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
        """根据屏幕内容触发情绪（带冷却，避免高频重复）"""
        now = time.time()
        if now - self._last_screen_emotion_time < self._screen_emotion_cooldown:
            return
        desc_lower = description.lower()
        for keyword, (emotion, intensity) in SCREEN_EMOTION_MAP.items():
            if keyword in desc_lower:
                self._last_screen_emotion_time = now
                logger.info("Screen emotion triggered: %s (%.1f) from '%s'", emotion, intensity, description[:30])
                self.on_emotion(emotion, intensity)
                return

    # ── 屏幕感知主动评论模板 ──
    # 注：模板经 .format(detail=...) 渲染，正文里的 JSON 大括号必须写成 {{ }} 转义，
    # 否则 .format 会把 {gesture:...} 当成字段名抛 KeyError。
    #
    # 2026-09-17 重写：原模板每条 200+ 字符，其中 2/3 是 [action:] 标签说明，
    # 且带多个示例（peek/excited/curious/cheer/tsukkomi/concern…）。
    # 实测后果（日志 13:24:09）：模型在缺具体素材时**把指令里的关键词复述了出来**——
    #     LLM 回复: 桌宠 鼓励 用户 阅读 技术博客 桌面宠物 交互风格
    # 这不是句子，是关键词堆。根因：模板里可被复述的词太多、示例太多，
    # 而 {detail} 填的是场景摘要（非具体内容），模型无从发挥。
    #
    # 新写法：模板只留**一句要求**，动作说明统一在 _PROACTIVE_ACTION_HINT
    # 里追加（且只给一个最小示例）。这样正文要求短、示例少，复述面大幅缩小。
    _PROACTIVE_TEMPLATES = [
        # 自由评论型
        "你是一个桌宠，你看到用户的屏幕内容如下：\n{detail}\n\n用一句话（10-30字）自然反应。像真正看到屏幕的人那样，可以吐槽、关心、好奇或评论。不要罗列词语，要成句。",
        # 好奇提问型
        "你是一个桌宠，你偷看了一眼用户的屏幕：\n{detail}\n\n用好奇的语气问一个问题（10-30字）。要成句，不要罗列词语。",
        # 鼓励型
        "你是一个桌宠，你看到用户正在：\n{detail}\n\n用鼓励的语气说一句话（10-30字）。要真诚、成句，不要罗列词语。",
        # 吐槽型
        "你是一个桌宠，你看到用户的屏幕：\n{detail}\n\n用吐槽的语气说一句话（10-30字）。幽默一点，要成句。",
        # 关心型
        "你是一个桌宠，你注意到用户：\n{detail}\n\n用关心的语气说一句话（10-30字）。比如提醒休息。要成句。",
    ]

    # 动作标签提示（统一追加，只给一个最小示例）。
    # 拆出来的理由：原实现把这段塞进每个模板，占了模板 2/3 篇幅，
    # 是「模型复述指令」的主要素材来源。
    _PROACTIVE_ACTION_HINT = (
        "\n\n可选：想在说这句话时配个动作，就在末尾加 "
        '[action:{{"gesture":"<动作名>","intensity":<0到1>}}]。'
        "没有合适的就不加。"
    )

    def _check_screen_proactive(self, description: str, detail: str = ""):
        """根据屏幕内容触发主动评论（多模板随机，自适应性格）"""
        # 冷却检查（5-15分钟随机间隔）
        if not hasattr(self, '_last_screen_proactive'):
            self._last_screen_proactive = 0
        if not hasattr(self, '_proactive_cooldown'):
            self._proactive_cooldown = random.randint(300, 900)
        if time.time() - self._last_screen_proactive < self._proactive_cooldown:
            return

        # 随机触发（20%→10% 概率）。
        # 2026-09-17：与模板瘦身配套——降低触发率减少「缺素材」场景的绝对发生数。
        if random.random() > 0.10:
            return

        # 用 detail 如果有，否则用 description
        screen_info = detail or description
        if not screen_info:
            return

        # 注入 agent 身份（如果有）
        agent_brief = getattr(self, '_agent_identity', '')
        identity_line = f"你的身份：{agent_brief[:150]}\n" if agent_brief else ""

        # 随机选模板
        template = random.choice(self._PROACTIVE_TEMPLATES)
        prompt = identity_line + template.format(detail=screen_info) + self._PROACTIVE_ACTION_HINT

        logger.info("Screen proactive: %s", screen_info[:60])
        self._last_screen_proactive = time.time()
        self._proactive_cooldown = random.randint(300, 900)  # 下次随机间隔
        self.on_screen_proactive(prompt)

    def set_agent_identity(self, identity: str):
        """注入 agent 身份（从 HanakoContext 读取）"""
        self._agent_identity = identity or ""
