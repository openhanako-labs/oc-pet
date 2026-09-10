"""主动对话调度 — 意图分类 + 规则引擎 + 空闲检测 + 前台分类 + 活动感知

触发流程：
1. tick() 检查是否过冷却期
2. 计算对话空闲时间（since last conversation）
3. 【P1 灵魂】先跑 IntentClassifier：综合 时间+前台+活动+持续时长 推断意图，
   命中场景（深夜加班/长时间工作/连续学习/频繁切窗…）且置信度达标 → 优先触发
4. 意图分类未命中时，退回旧规则引擎（按 idle_min 倒序匹配 foreground 分类）
5. 检查活动状态（typing/idle/mouse）——打字中打断成本高，降低触发权重

打扰成本逻辑：
    typing → 用户专注打字，打断成本最高，权重乘以 COST_TYPING（默认 0.2）
    mouse  → 用户在操作鼠标，可能划水也可能工作，权重乘以 COST_MOUSE（0.6）
    idle   → 用户不在/长时间空闲，最佳时机，权重不变

外部依赖：
- random（概率触发）
- motion.activity_tracker（可选注入，未注入时退回纯规则）
- core.perception.intent（P1 意图分类器）
- core.perception.scenarios（场景文案池）
"""
from __future__ import annotations

import ctypes
import logging
import random
import time

logger = logging.getLogger(__name__)

from .intent import classify_intent, LATE_NIGHT_WORK_MINUTES
from .scenarios import get_reaction, is_disruptive, get_recall_reaction, get_associate_reaction
from .proactive_contracts import (
    PROACTIVE_REASON_PASS_THROTTLED,
)
from .proactive_state import ProactiveThrottle
from .proactive_generation import ProactiveGenerator


DEFAULT_RULES = [
    {"idle_min": 5,  "foreground": ["writing", "development", "browsing"], "prompt": "写了这么久，休息一下吧？", "weight": 0.7},
    {"idle_min": 15, "foreground": ["gaming", "entertainment"],             "prompt": "带我一起玩嘛～",          "weight": 0.5},
    {"idle_min": 30, "foreground": ["communication"],                       "prompt": "还在忙吗？想和你说说话～", "weight": 0.3},
]
# 注意：idle_min=60 的兜底规则（"好安静啊……你在做什么呢？"）已被移除——
# P1 intent 的 chat_idle 场景（60 分钟没说话）已覆盖该时长，保留会导致重复触发同类文案。

# 打扰成本乘数（活动状态 → 权重衰减）
COST_TYPING = 0.2    # 打字中：打断成本最高
COST_MOUSE = 0.6     # 鼠标活动：中断成本中等
COST_IDLE = 1.0      # 空闲/离开：最佳时机，权重不变

# 打字中完全抑制的时间阈值：持续打字超过该时长则打死不搭话
TYPING_SUPPRESS_SECONDS = 60.0

# P1 意图触发的最低置信度（低于此值不触发，退回规则引擎）
INTENT_MIN_CONFIDENCE = 0.6

# P6 每日主动对话总量上限（load_config 可用 daily_limit 覆盖）
DAILY_LIMIT = 20

# P6 自适应冷却边界：被无视惩罚上限 / 用户回应奖励下限（分钟）
COOLDOWN_MAX_MINUTES = 60.0
COOLDOWN_MIN_MINUTES = 5.0

# D 场景回忆冷却（分钟，默认 30；config memory.recall.cooldown_minutes 可覆盖）
RECALL_COOLDOWN_MINUTES = 30.0

# P0-1 主动搭话 LLM 生成（默认开；config proactive.llm_generation 可覆盖）
DEFAULT_LLM_GENERATION = True

# P0-2 同会话去重：生成/模板文案与近期主动搭话高度相似时跳过（日志 dedup）
DEDUP_LOG_TAG = "[proactive] dedup"

# 意图命中并已启动异步生成的哨兵返回值（tick 据此停止本轮，防回忆/规则双投递）
_GENERATION_PENDING = "__proactive_generation_pending__"


class ProactiveScheduler:
    """主动对话调度器 - 意图分类 + 规则引擎 + 空闲检测 + 前台分类 + 活动感知

    P0-1/P0-2 升级：
      - tick 管线：意图 → LLM 生成（可选） → 回忆 → 规则兜底
      - 半衰期节流（ProactiveThrottle）：source 级去重 + 每日预算 + 文案相似去重
      - LLM 生成（ProactiveGenerator）：复用 Hanako 通道；失败/超时回退模板池
    """

    def __init__(self, foreground_watcher=None, on_proactive: callable = None, activity_tracker=None, config: dict | None = None):
        self._foreground_watcher = foreground_watcher
        self._activity_tracker = activity_tracker  # 可选注入 ActivityTracker
        # 配置（_is_dnd_active 等会读 proactive.dnd）：缺省空字典，避免 AttributeError
        self._config: dict = config or {}
        self._enabled = True
        self._cooldown_minutes = 10
        self._rules: list[dict] = list(DEFAULT_RULES)
        self._cooldown_until: float = 0.0
        self._last_conversation: float = time.time()  # 上次对话时间
        self._typing_since: float | None = None  # 连续打字起始时间（None=不在打字）
        self.on_proactive: callable = on_proactive or (lambda text: None)

        # 2026-09-10: 接用使用记忆（core/usage_memory.py）——用户明确说「别再说 X」后
        # 24h 内不再触发该类内容。None 时惰性创建，不阻断调度。
        self._usage_memory = None
        self._last_speak_key = ""  # 最近一次主动说话的内容键（供桌面菜单「别再说这类」使用）

        # ── P6 自适应冷却：动态冷却（无视→翻倍惩罚 / 回应→减半奖励）──
        self._current_cooldown: float = 10.0  # 动态冷却，初始同静态默认
        self._last_proactive_at: float = 0.0  # 上次主动触发时间
        self._user_replied_since_last: bool = True  # 上次触发后用户是否回应过（初始视为可触发）

        # ── P6 每日总量限制 ──
        self._daily_limit: int = DAILY_LIMIT
        self._daily_count: int = 0
        self._daily_date: str = ""
        
        # 2026-09-06: 按时间段分配的打扰预算
        self._period_limits: dict[str, int] = {
            "morning": 2,   # 08:00-12:00
            "afternoon": 2, # 12:00-18:00
            "evening": 2,   # 18:00-24:00
            "night": 0      # 00:00-08:00
        }
        self._period_counts: dict[str, int] = {
            "morning": 0,
            "afternoon": 0,
            "evening": 0,
            "night": 0
        }

        # ── P6 全屏检测（游戏/视频全屏不打扰）──
        self._fullscreen_threshold: float = 0.95  # 窗口覆盖屏幕比例阈值（可配置）
        self._fullscreen_suppress: bool = True    # 全屏时是否抑制主动搭话（可配置）

        # ── D/E 场景记忆回忆（可选注入；未注入/开关关闭 → 整体失效、行为不变）──
        self._scene_memory = None                 # SceneMemory 实例（由 pet.py 注入）
        self._recall_enabled = True               # config memory.recall.enabled
        self._associate_enabled = True            # config memory.associate.enabled
        self._recall_cooldown_minutes: float = RECALL_COOLDOWN_MINUTES
        self._recall_cooldown_until: float = 0.0  # 回忆独立冷却截止

        # ── P0-1/P0-2 半衰期节流 + LLM 生成（默认不注入 → 行为与旧版完全一致）──
        self._throttle = ProactiveThrottle(daily_limit=self._daily_limit)
        self._generator: ProactiveGenerator | None = None   # 由 pet.py 注入
        self._llm_generation: bool = DEFAULT_LLM_GENERATION  # config proactive.llm_generation
        self._generation_in_flight: bool = False             # 生成进行中，防止并发/重复触发
        self._pending_fallback_prompt: str = ""              # 生成失败时的回退文案
        self._pending_source_key: str = ""                   # 生成上下文 source_key（节流用）

        # ── P1-6 屏幕/意图感知联动：屏幕场景提供者（可选注入）──
        # provider: callable() -> ScreenScene | dict | None（读取屏幕感知分类结果）。
        # 注入后 tick 会把它并入 signals，供意图触发/场景回忆使用；未注入零变化。
        self._screen_scene_provider = None

        # ── P1-5 反重复（语义指纹 + 跨会话去重；与 throttle 字符串相似去重互补）──
        self._anti_repeat = None            # AntiRepeatCorpus 实例（可选注入）
        self._anti_repeat_name: str = ""    # 角色名（corpus 分 key）
        self._last_user_message: float = time.time()  # 用户最后一次真实消息时间（未回应信号用）

    def set_scene_memory(self, scene_memory) -> None:
        """注入 SceneMemory（D 场景回忆的检索端）。

        未注入时 _try_recall 直接 return None，现有主动对话路径零变化（兼容性）。
        """
        self._scene_memory = scene_memory

    def set_screen_scene_provider(self, provider: callable | None) -> None:
        """注入屏幕场景提供者（P1-6 屏幕/意图感知联动）。

        Args:
            provider: callable() -> ScreenScene | dict | None。每次 tick 读取，
                并入 signals（screen_scene/screen_intent/screen_confidence）。
                未注入 / 返回 None → 屏幕信号缺席，行为与旧版一致。
        """
        self._screen_scene_provider = provider if callable(provider) else None

    def set_anti_repeat(self, corpus=None, agent_name: str = "") -> None:
        """注入反重复语料库（P1-5 语义指纹 + 跨会话去重）。

        Args:
            corpus: AntiRepeatCorpus 实例；None=关闭语义去重（行为与旧版一致）。
            agent_name: 角色名（corpus 内分 key；缺省 "default"）。

        与 ``ProactiveThrottle.is_duplicate``（字符串相似）互补：
        - 字符串相似 → 抓字面复读（1h 窗 + 0.90 阈值）
        - 语义指纹 → 抓"换说法但同一话题"近重复（BM25 IDF×TF，跨会话持久化）
        - 未回应信号 → 抓"隔几轮/跨小时又出现的高度相似主动搭话"
        """
        self._anti_repeat = corpus
        self._anti_repeat_name = (agent_name or "").strip() or "default"

    def _anti_repeat_allows(self, text: str, now: float | None = None) -> bool:
        """P1-5 语义去重检查：True=放行；False=与近期/历史主动搭话语义重复，拒绝。

        两个互补信号（与 ``_throttle.is_duplicate`` 字符串相似互补）：
        - BM25 短窗（score_draft ≥ DROP_THRESHOLD）：最近 10 分钟内"换说法但同一
          话题"的近重复（语义指纹）
        - 未回应长窗（score_unanswered_proactive_draft triggered）：用户一直没回应
          时，隔几轮/跨小时再次出现的高度相似主动搭话（跨会话去重）
        """
        if self._anti_repeat is None or not (text or "").strip():
            return True
        try:
            name = self._anti_repeat_name
            now = now if now is not None else time.time()
            if self._anti_repeat.is_repeat(name, text, now=now):
                logger.info("[proactive] semantic dedup (bm25): %s", text[:40])
                return False
            sig = self._anti_repeat.score_unanswered_proactive_draft(
                name, text, silence_since=self._last_user_message, now=now,
            )
            if sig.triggered:
                logger.info(
                    "[proactive] semantic dedup (unanswered, %d matches, sim=%.2f): %s",
                    sig.match_count, sig.best_similarity, text[:40],
                )
                return False
            return True
        except Exception as e:
            logger.debug("anti_repeat check failed (allow): %s", e)
            return True

    def _anti_repeat_record(self, text: str, now: float | None = None) -> None:
        """P1-5 登记一条已投递的主动搭话到语义语料库（跨会话去重依据）。"""
        if self._anti_repeat is None or not (text or "").strip():
            return
        try:
            self._anti_repeat.record_output(
                self._anti_repeat_name, text, is_proactive=True,
                now=now if now is not None else time.time(),
            )
        except Exception as e:
            logger.debug("anti_repeat record failed: %s", e)

    def load_memory_config(self, memory_cfg: dict) -> None:
        """加载记忆层配置段（config["memory"]）。

        Args:
            memory_cfg: {"recall": {"enabled", "cooldown_minutes"},
                         "associate": {"enabled"}}
        """
        if not isinstance(memory_cfg, dict):
            return
        recall_cfg = memory_cfg.get("recall", {}) or {}
        self._recall_enabled = bool(recall_cfg.get("enabled", True))
        try:
            self._recall_cooldown_minutes = float(
                recall_cfg.get("cooldown_minutes", RECALL_COOLDOWN_MINUTES)
            )
        except Exception:
            self._recall_cooldown_minutes = RECALL_COOLDOWN_MINUTES
        assoc_cfg = memory_cfg.get("associate", {}) or {}
        self._associate_enabled = bool(assoc_cfg.get("enabled", True))

    def load_config(self, config: dict):
        self._enabled = config.get("enabled", True)
        self._cooldown_minutes = config.get("cooldown_minutes", 10)
        self._current_cooldown = float(config.get("cooldown_minutes", 10))
        self._rules = config.get("rules", list(DEFAULT_RULES))
        self._daily_limit = config.get("daily_limit", DAILY_LIMIT)
        self._throttle = ProactiveThrottle(daily_limit=self._daily_limit)
        # P0-1 主动搭话 LLM 生成开关（T01 config 骨架默认 True；缺省按 True 向后兼容）
        self._llm_generation = bool(config.get("llm_generation", DEFAULT_LLM_GENERATION))
        # P6-2 全屏检测可配置：阈值（覆盖比例，默认 0.95）与总开关（默认开启）
        self._fullscreen_threshold = float(config.get("fullscreen_threshold", 0.95))
        self._fullscreen_suppress = bool(config.get("fullscreen_suppress", True))

    def set_generator(self, generator: ProactiveGenerator | None, llm_generation: bool | None = None) -> None:
        """注入 LLM 生成器（P0-1）。

        Args:
            generator: ProactiveGenerator 实例；None=关闭生成（回退模板池）。
            llm_generation: 可选覆盖 config 的生成开关；None=保持 load_config 值。
        """
        self._generator = generator
        if llm_generation is not None:
            self._llm_generation = bool(llm_generation)

    def generation_available(self) -> bool:
        """LLM 生成是否可用（开关开 + 已注入可用生成器 + 无在途生成）。"""
        return (
            self._llm_generation
            and self._generator is not None
            and self._generator.is_available()
            and not self._generation_in_flight
        )

    def _start_generation(self, context: dict, fallback_prompt: str, source_key: str) -> bool:
        """启动一次异步 LLM 生成（P0-1）。

        成功返回 True（生成已在后台执行，结果经 Qt Signal 回主线程后投递）；
        失败/未启用返回 False（调用方应立即使用 fallback_prompt 走模板池）。

        Args:
            context: 生成上下文（scenario/signals/intent/fallback_prompt…）。
            fallback_prompt: 生成失败时的回退模板文案。
            source_key: 本次触发的节流 key（场景名/规则 prompt 等）。
        """
        if not self.generation_available():
            return False
        self._generation_in_flight = True
        self._pending_fallback_prompt = fallback_prompt or ""
        self._pending_source_key = source_key or ""
        try:
            self._generator.set_callbacks(
                on_generated=self._on_generation_result,
                on_fallback=self._on_generation_fallback,
            )
            self._generator.generate(context, fallback_prompt)
            logger.info("[proactive] generated via llm (async started): source=%s", source_key)
            return True
        except Exception as e:
            logger.warning("[proactive] generation start failed, fallback: %s", e)
            self._generation_in_flight = False
            self._pending_fallback_prompt = ""
            self._pending_source_key = ""
            return False

    def _on_generation_result(self, text: str) -> None:
        """LLM 生成成功回调（主线程经 Qt Signal 调用）。

        验收日志：`[proactive] generated via llm`；生成文本与近期重复 → dedup 日志。
        """
        self._generation_in_flight = False
        fallback_prompt = self._pending_fallback_prompt
        source_key = self._pending_source_key
        self._pending_fallback_prompt = ""
        self._pending_source_key = ""
        text = (text or "").strip()
        if not text:
            logger.info("[proactive] fallback: generation empty -> %s", fallback_prompt)
            self._deliver(fallback_prompt, source_key=source_key)
            return
        # P0-2 同会话去重：与近期主动搭话高度相似 → 跳过（不投递、不计数）
        if self._throttle.is_duplicate(text):
            logger.info("%s: %s", DEDUP_LOG_TAG, text[:40])
            return
        self._throttle.record_chat(text)
        logger.info("[proactive] generated via llm: %s", text)
        self._deliver(text, source_key=source_key)

    def _on_generation_fallback(self, fallback_text: str) -> None:
        """LLM 生成失败/超时回退回调（主线程经 Qt Signal 调用）。

        验收日志：`fallback`；直接走模板池投递（仍计触发与节流）。
        """
        self._generation_in_flight = False
        fallback_prompt = self._pending_fallback_prompt or (fallback_text or "")
        source_key = self._pending_source_key
        self._pending_fallback_prompt = ""
        self._pending_source_key = ""
        logger.info("[proactive] fallback: %s", fallback_prompt)
        self._deliver(fallback_prompt, source_key=source_key)

    def set_usage_memory(self, memory) -> None:
        """注入 UsageMemory（可选；不注入时惰性创建单例）。"""
        self._usage_memory = memory

    def _get_usage_memory(self):
        """惰性获取 UsageMemory 单例；不可用时返回 None（不阻断主流程）。"""
        if self._usage_memory is not None:
            return self._usage_memory
        try:
            from core.usage_memory import UsageMemory
            self._usage_memory = UsageMemory()
        except Exception as e:
            logger.warning("[proactive] 使用记忆不可用，跳过内容禁言检查: %s", e)
            self._usage_memory = False  # 哨兵：标记已尝试且失败，避免每次重试
            return None
        return self._usage_memory

    def is_content_banned(self, source_key: str) -> bool:
        """该内容类型是否被用户禁止（或在 24h 禁言期内）。"""
        if not source_key:
            return False
        mem = self._get_usage_memory()
        if not mem:
            return False
        try:
            return bool(mem.is_banned(source_key))
        except Exception as e:
            logger.warning("[proactive] 禁言查询失败: %s", e)
            return False

    def _speak(self, prompt: str, source_key: str = "") -> None:
        """**唯一**调用 on_proactive 的出口。

        所有主动说话路径（_deliver / intent / recall / associate / rules）都经过这里，
        所以内容禁言只需在此拦一次。新增触发路径必须走本方法，不得直调 on_proactive。
        """
        if self.is_content_banned(source_key):
            logger.info("[proactive] 内容类型已被用户禁止，跳过：%s", source_key)
            return
        self._last_speak_key = source_key or ""
        self.on_proactive(prompt)

    def ban_last_content(self, hours: float = 24.0) -> str:
        """禁言最近一次主动说话的内容类型（供桌面菜单「别再说这类」）。

        Returns:
            被禁言的内容键；无内容键时返回空字符串。
        """
        key = self._last_speak_key
        if not key:
            return ""
        mem = self._get_usage_memory()
        if not mem:
            return ""
        try:
            mem.record_usage(key, action="explicit_ban", text=key)
            logger.info("[proactive] 已禁言内容类型 %s（%.0fh）", key, hours)
        except Exception as e:
            logger.warning("[proactive] 禁言记录失败: %s", e)
            return ""
        return key

    def _deliver(self, prompt: str, source_key: str = "") -> None:
        """统一投递入口：记录触发 + 节流 + on_proactive。

        生成路径（_on_generation_result/_on_generation_fallback）与同步模板路径共用，
        保证无论文案来自 LLM 还是模板池，触发簿记/节流/每日计数都一致。
        """
        if not (prompt or "").strip():
            return
        # 内容禁言：在簿记前拦截，避免被禁内容浪费当日预算与冷却
        if self.is_content_banned(source_key):
            logger.info("[proactive] 内容类型已被用户禁止，跳过：%s", source_key)
            return
        now = time.time()
        # P1-5 语义去重（生成路径兜底：fallback 模板也可能与历史话题重复）
        if not self._anti_repeat_allows(prompt, now):
            return
        self._record_proactive_trigger(now)
        if source_key:
            self._throttle.record_used(source_key, kind="chat", now=now)
        self._throttle.record_chat(prompt, now=now)
        self._anti_repeat_record(prompt, now)
        self._speak(prompt, source_key)

    def mark_conversation(self, user_reply: bool = False):
        """标记对话发生。

        Args:
            user_reply: True=用户真实回应（奖励：冷却减半，下限 5 分钟）；
                        False=proactive 自身触发后的记录（只重置空闲计时，
                        不影响 _user_replied_since_last 与动态冷却）。
        """
        self._last_conversation = time.time()
        if user_reply:
            self._user_replied_since_last = True
            self._last_user_message = time.time()  # P1-5 未回应信号：用户真实消息时间
            self._current_cooldown = max(COOLDOWN_MIN_MINUTES, self._current_cooldown * 0.5)

    def _record_proactive_trigger(self, now: float) -> None:
        """统一处理"主动触发成功"的簿记（P6）。

        - 用户此前没回应过（_user_replied_since_last=False）→ 被无视惩罚：冷却翻倍
        - 用户刚回应过（_user_replied_since_last=True）→ 合理触发，不翻倍
        - 记录 _last_proactive_at / 重置 _user_replied_since_last / 更新冷却截止 / 每日计数
        - 达到每日上限时 logger.info 一次（当天后续 tick 直接静默返回）
        """
        if not self._user_replied_since_last:
            # 上次触发后用户一直没回应 → 学乖：冷却翻倍（上限 60 分钟）
            self._current_cooldown = min(COOLDOWN_MAX_MINUTES, self._current_cooldown * 2.0)
        self._last_proactive_at = now
        self._user_replied_since_last = False
        self._daily_count += 1
        # 2026-09-06: 更新按时间段计数
        period = self._get_period()
        self._period_counts[period] = self._period_counts.get(period, 0) + 1
        self._cooldown_until = now + self._current_cooldown * 60
        if self._daily_count == self._daily_limit:
            logger.info("Proactive daily limit reached (%d), no more triggers today", self._daily_count)

    def _get_period(self) -> str:
        """获取当前时间段（morning/afternoon/evening/night）。
        
        2026-09-06: 按时间段分配的打扰预算
        """
        hour = time.localtime().tm_hour
        if hour < 8:
            return "night"      # 00:00-08:00
        elif hour < 12:
            return "morning"    # 08:00-12:00
        elif hour < 18:
            return "afternoon"  # 12:00-18:00
        else:
            return "evening"    # 18:00-24:00
    
    def _is_dnd_active(self) -> bool:
        """检查是否处于静默模式（Do Not Disturb）。
        
        2026-09-06: 深夜时段（00:00-08:00）不响，用户可手动开启勿扰。
        """
        # 1. 检查配置
        cfg = self._config.get("proactive", {}).get("dnd", {}) or {}
        if not cfg.get("enabled", True):
            return False
        
        # 2. 深夜时段检查
        now = time.localtime()
        hour = now.tm_hour
        start = cfg.get("late_night_start", 0)
        end = cfg.get("late_night_end", 8)
        if hour < end or hour >= 24 - start:  # 00:00-08:00 或 22:00-24:00（如果 start=2）
            if hour < end:
                return True
        
        # 3. 用户手动开启勿扰（未来实现）
        # if self._dnd_manual:
        #     return True
        
        return False
    
    def _is_fullscreen(self) -> bool:
        """检测当前前台窗口是否全屏（游戏/视频全屏不打扰）。

        Returns:
            True=前台窗口为真正全屏（覆盖 ≥fullscreen_threshold 屏幕且原点在 (0,0)）；
            False=非全屏（含最大化窗口）或检测失败。
        """
        try:
            from motion.foreground_watcher import _get_foreground_window_rect
            rect = _get_foreground_window_rect()
            if not rect:
                return False
            x, y, width, height = rect
            screen_w = ctypes.windll.user32.GetSystemMetrics(0)  # SM_CXSCREEN
            screen_h = ctypes.windll.user32.GetSystemMetrics(1)  # SM_CYSCREEN
            if screen_w <= 0 or screen_h <= 0:
                return False
            # 几何判据：覆盖 ≥fullscreen_threshold 屏幕（用户可在设置面板调阈值）。
            # 额外要求原点非负——Windows 最大化窗口的 rect 会因隐形边框带负坐标
            # （如 -7,-7），并非真正的全屏（游戏/视频全屏原点为 0,0）。避免把
            # 最大化 IDE/终端误判成全屏而一直不搭话。
            return (
                x >= 0 and y >= 0
                and width >= screen_w * self._fullscreen_threshold
                and height >= screen_h * self._fullscreen_threshold
            )
        except Exception as e:
            logger.debug("Fullscreen detection failed: %s", e)
            return False

    @property
    def enabled(self) -> bool:
        return self._enabled

    def enable(self):
        self._enabled = True

    def disable(self):
        self._enabled = False

    def reset(self):
        self._cooldown_until = time.time() + self._current_cooldown * 60

    def _collect_signals(self, now: float) -> dict:
        """收集意图分类所需信号（时间/前台/活动/持续时长）。"""
        signals = {
            "period": "other", "category": "other", "activity": "idle",
            "fg_duration_min": 0.0, "conversation_idle_min": 0.0,
            "window_switches_5min": 0, "is_weekend": False,
            # P1-6 屏幕/意图感知联动（缺省 None = 屏幕信号缺席）
            "screen_scene": None, "screen_intent": None, "screen_confidence": 0.0,
            "screen_propensity": "open",
        }
        try:
            from .time import TimePerception
            tp = TimePerception()
            tctx = tp.get_context()
            signals["period"] = tctx.get("period", "other")
            signals["is_weekend"] = tctx.get("is_weekend", False)
        except Exception:
            logger.debug("proactive: 非致命异常(已静默吞掉)", exc_info=True)
        if self._foreground_watcher:
            signals["category"] = getattr(self._foreground_watcher, "last_category", "") or "other"
            signals["fg_duration_min"] = getattr(self._foreground_watcher, "fg_duration_min", 0.0) or 0.0
            signals["window_switches_5min"] = getattr(self._foreground_watcher, "window_switches_5min", 0) or 0
        if self._activity_tracker is not None:
            try:
                signals["activity"] = self._activity_tracker.state.state
            except Exception:
                signals["activity"] = "idle"
        signals["conversation_idle_min"] = (now - self._last_conversation) / 60.0
        # P1-6 屏幕场景提供者：并入 signals（provider 返回 ScreenScene/dict/None）
        if self._screen_scene_provider is not None:
            try:
                raw = self._screen_scene_provider()
                if raw is not None:
                    if hasattr(raw, "to_dict"):
                        raw = raw.to_dict()
                    if isinstance(raw, dict):
                        signals["screen_scene"] = raw.get("scene") or ""
                        signals["screen_intent"] = raw.get("intent") or ""
                        signals["screen_confidence"] = float(raw.get("confidence") or 0.0)
                        signals["screen_propensity"] = raw.get("propensity") or "open"
            except Exception as e:
                logger.debug("screen scene provider failed: %s", e)
        return signals

    def _try_intent(self, now: float, signals: dict) -> str | None:
        """P1 意图分类优先触发：命中场景 + 置信度达标 → 触发（P0-1 可走 LLM 生成）。

        P1-6 屏幕/意图感知联动：
        - 屏幕场景 propensity=closed（私密）→ 直接不触发（硬跳过）
        - 屏幕场景置信度达标 → 用屏幕场景作为触发场景（更贴近用户当前状态），
          意图置信度取 max(屏幕, 规则意图)；失败/缺席 → 走原 classify_intent
        """
        try:
            intent = classify_intent(
                period=signals["period"],
                category=signals["category"],
                activity=signals["activity"],
                fg_duration_min=signals["fg_duration_min"],
                conversation_idle_min=signals["conversation_idle_min"],
                window_switches_5min=signals["window_switches_5min"],
                is_weekend=signals["is_weekend"],
            )
            scenario = intent.get("scenario", "")
            confidence = intent.get("confidence", 0.0)

            # P1-6 屏幕场景优先：私密硬跳过；高置信度场景覆盖规则意图场景
            screen_scene = signals.get("screen_scene") or ""
            screen_conf = float(signals.get("screen_confidence") or 0.0)
            screen_propensity = signals.get("screen_propensity") or "open"
            if screen_propensity == "closed":
                logger.debug("Proactive intent skipped: screen scene closed (private)")
                return None
            if screen_scene and screen_conf >= INTENT_MIN_CONFIDENCE:
                from .screen_intent import to_intent_scenario
                # to_intent_scenario 内部已按 is_weekend 约束 weekend_play：
                # 非周末（取自真实时间戳）的 slacking/music_listening 会自动
                # 降级为 chat_idle，日期判断集中在这一个地方，无需调用处补丁。
                mapped = to_intent_scenario(screen_scene, signals.get("is_weekend"))
                if mapped:
                    scenario = mapped
                    confidence = max(confidence, screen_conf)
                    intent = dict(intent)
                    intent["scenario"] = scenario
                    intent["intent"] = signals.get("screen_intent") or intent.get("intent", "work")
                    intent["reason"] = f"屏幕感知({screen_scene} conf={screen_conf:.2f})"

            if confidence < INTENT_MIN_CONFIDENCE or not scenario:
                return None
            # 打扰成本：打字中且场景非"值得打扰" → 不触发
            activity = signals["activity"]
            if activity == "typing" and not is_disruptive(scenario):
                logger.debug("Proactive intent skipped: typing + 非值得打扰场景 %s", scenario)
                return None
            # P0-2 半衰期节流：同一场景在硬跳过窗口内不重复触发
            if self._throttle.should_skip(scenario, kind="chat", now=now):
                logger.info(
                    "[proactive] %s scenario=%s (hard-skip/half-life)",
                    PROACTIVE_REASON_PASS_THROTTLED, scenario,
                )
                return None
            # 深夜加班类场景：即使打字也值得提醒（强度高）
            reaction = get_reaction(scenario, intensity=confidence)
            prompt = reaction["text"]
            # P0-1 LLM 生成：意图命中 → 尝试生成更自然文案；失败回退模板池
            if self.generation_available():
                context = {
                    "scenario": scenario,
                    "intent": intent,
                    "signals": signals,
                    "fallback_prompt": prompt,
                }
                started = self._start_generation(context, fallback_prompt=prompt, source_key=scenario)
                if started:
                    logger.info(
                        "Proactive intent -> generation: scenario=%s intent=%s conf=%.2f %s",
                        scenario, intent.get("intent"), confidence, intent.get("reason"),
                    )
                    return _GENERATION_PENDING  # 结果异步投递（哨兵停止本轮）
            # P1-5 语义去重：与近期/历史主动搭话话题重复 → 拒绝（与字符串去重互补）
            if not self._anti_repeat_allows(prompt, now):
                return None
            self._record_proactive_trigger(now)
            self._throttle.record_used(scenario, kind="chat", now=now)
            self._throttle.record_chat(prompt, now=now)
            self._anti_repeat_record(prompt, now)
            logger.info(
                "Proactive intent triggered: scenario=%s intent=%s conf=%.2f %s",
                scenario, intent.get("intent"), confidence, intent.get("reason"),
            )
            self._speak(prompt, scenario)
            return prompt
        except Exception as e:
            logger.debug("Proactive intent failed (fallback to rules): %s", e)
            return None

    def _try_recall(self, now: float, signals: dict) -> str | None:
        """D/E 场景回忆触发：命中历史场景 → 说一句带记忆的话。

        调用顺序（tick 内）：_try_intent（意图优先）→ _try_recall（回忆）→ 规则兜底。
        回忆不抢意图的触发机会（意图命中则回忆让位）。

        守卫（复用 tick 已有守卫）：
          - 未注入 scene_memory / memory.recall.enabled=False → return None
          - 独立冷却 _recall_cooldown_until（默认 30 分钟）
          - 打字中且场景非值得打扰（is_disruptive）→ return None
          - 每日上限 / 全屏 / 持续打字抑制已在 tick 统一处理

        E 联想：回忆未命中且 memory.associate.enabled → associate 标签交集联想。
        """
        if self._scene_memory is None or not self._recall_enabled:
            return None
        try:
            if now < self._recall_cooldown_until:
                return None
            category = signals.get("category", "") or ""
            if not category or category == "other":
                return None
            # 用意图分类推导场景名（回忆匹配维度；失败不阻塞）
            scenario = ""
            emotion = "neutral"
            period = signals.get("period", "other")
            try:
                intent = classify_intent(
                    period=period,
                    category=category,
                    activity=signals.get("activity", "idle"),
                    fg_duration_min=signals.get("fg_duration_min", 0.0),
                    conversation_idle_min=signals.get("conversation_idle_min", 0.0),
                    window_switches_5min=signals.get("window_switches_5min", 0),
                    is_weekend=signals.get("is_weekend", False),
                )
                scenario = intent.get("scenario", "") or ""
            except Exception:
                scenario = ""
            tags = [category, scenario, period, emotion]
            # P1-6 屏幕/意图感知联动：屏幕场景标签并入回忆匹配维度
            screen_scene = signals.get("screen_scene") or ""
            screen_intent = signals.get("screen_intent") or ""
            if screen_scene:
                tags.append(screen_scene)
            if screen_intent:
                tags.append(screen_intent)
            # 打扰成本：打字中且场景非"值得打扰" → 不触发
            if signals.get("activity") == "typing" and scenario and not is_disruptive(scenario):
                logger.debug("Proactive recall skipped: typing + 非值得打扰场景 %s", scenario)
                return None
            matches = self._scene_memory.find_matching(category, scenario, tags, max_results=3)
            if matches:
                scene = matches[0]
                text = get_recall_reaction(scene, {"topic": scene.topics[0] if scene.topics else ""})
                if text:
                    # P0-2 同会话去重：与近期主动搭话高度相似 → 跳过
                    if self._throttle.is_duplicate(text, now=now):
                        logger.info("%s: recall %s", DEDUP_LOG_TAG, text[:40])
                        return None
                    # P1-5 语义去重：与历史主动搭话话题重复 → 拒绝
                    if not self._anti_repeat_allows(text, now):
                        return None
                    self._record_proactive_trigger(now)
                    self._recall_cooldown_until = now + self._recall_cooldown_minutes * 60
                    self._throttle.record_used(scene.scene_id, kind="chat", now=now)
                    self._throttle.record_chat(text, now=now)
                    self._anti_repeat_record(text, now)
                    logger.info(
                        "Proactive recall triggered: scene=%s category=%s scenario=%s",
                        scene.scene_id, category, scenario,
                    )
                    self._speak(text, scene.scene_id)
                    return text
                return None
            # E 联想：回忆未命中且开关开启 → 标签交集联想
            if self._associate_enabled:
                current = {
                    "category": category,
                    "scenario": scenario,
                    "emotion": emotion,
                    "period": period,
                }
                scene = self._scene_memory.associate(
                    current, self._scene_memory.recent_scenes(20)
                )
                if scene is not None:
                    text = get_associate_reaction(
                        scene, {"topic": scene.topics[0] if scene.topics else ""}
                    )
                    if text:
                        # P0-2 同会话去重：与近期主动搭话高度相似 → 跳过
                        if self._throttle.is_duplicate(text, now=now):
                            logger.info("%s: associate %s", DEDUP_LOG_TAG, text[:40])
                            return None
                        # P1-5 语义去重：与历史主动搭话话题重复 → 拒绝
                        if not self._anti_repeat_allows(text, now):
                            return None
                        self._record_proactive_trigger(now)
                        self._recall_cooldown_until = now + self._recall_cooldown_minutes * 60
                        self._throttle.record_used(scene.scene_id, kind="chat", now=now)
                        self._throttle.record_chat(text, now=now)
                        self._anti_repeat_record(text, now)
                        logger.info(
                            "Proactive associate triggered: from=%s to=%s",
                            scene.scene_id, category,
                        )
                        self._speak(text, scene.scene_id)
                        return text
        except Exception as e:
            logger.debug("Proactive recall failed: %s", e)
        return None

    def tick(self) -> str | None:
        if not self._enabled or not self._rules:
            return None
        now = time.time()

        # ── P6 每日总量限制：跨天重置；达到上限当天不再触发 ──
        today = time.strftime("%Y-%m-%d")
        if today != self._daily_date:
            self._daily_date = today
            self._daily_count = 0
            self._period_counts = {"morning": 0, "afternoon": 0, "evening": 0, "night": 0}
        if self._daily_count >= self._daily_limit:
            return None
        
        # 2026-09-06: 按时间段分配的打扰预算
        period = self._get_period()
        if self._period_counts.get(period, 0) >= self._period_limits.get(period, 0):
            logger.debug("Proactive suppressed: period budget exhausted (%s)", period)
            return None

        if now < self._cooldown_until:
            return None

        # 2026-09-06: 静默模式（Do Not Disturb）——深夜时段不响
        if self._is_dnd_active():
            logger.debug("Proactive suppressed: DND active")
            return None

        # P0-1 生成在途：等待异步结果回主线程投递，本轮不再触发其他路径（防重复）
        if self._generation_in_flight:
            return None

        # 对话空闲时间（上次对话到现在）
        conversation_idle = now - self._last_conversation

        category = "other"
        if self._foreground_watcher:
            category = self._foreground_watcher.last_category or "other"

        # ── 活动感知：打扰成本 ──
        activity = "idle"
        cost = COST_IDLE
        if self._activity_tracker is not None:
            try:
                astate = self._activity_tracker.state
                activity = astate.state
                if activity == "typing":
                    cost = COST_TYPING
                    if self._typing_since is None:
                        self._typing_since = now
                    # 持续打字超阈值：直接抑制搭话
                    if now - self._typing_since >= TYPING_SUPPRESS_SECONDS:
                        logger.debug("Proactive suppressed: typing %ds", int(now - self._typing_since))
                        return None
                else:
                    self._typing_since = None
                    if activity == "mouse":
                        cost = COST_MOUSE
            except Exception:
                cost = COST_IDLE
        else:
            self._typing_since = None

        # ── P6 全屏检测：游戏/视频全屏不打扰（communication 豁免；可在设置面板关闭）──
        if self._fullscreen_suppress and category != "communication" and self._is_fullscreen():
            logger.debug("Proactive suppressed: fullscreen fg=%s", category)
            return None

        # ── P1 意图分类优先触发 ──
        signals = self._collect_signals(now)
        intent_prompt = self._try_intent(now, signals)
        if intent_prompt:
            return intent_prompt
        # 意图命中但已启动异步生成 → 本轮不再走回忆/规则（防同 tick 重复触发）
        if self._generation_in_flight:
            return None

        # ── D/E 场景回忆（意图优先，回忆让位；未注入/关闭则零行为）──
        recall_prompt = self._try_recall(now, signals)
        if recall_prompt:
            return recall_prompt

        # ── 规则引擎兜底 ──
        sorted_rules = sorted(self._rules, key=lambda r: r.get("idle_min", 0), reverse=True)
        for rule in sorted_rules:
            required_idle = rule.get("idle_min", 0) * 60
            if conversation_idle < required_idle:
                continue
            fg_match = rule.get("foreground", ["*"])
            if "*" in fg_match or category in fg_match:
                base_weight = rule.get("weight", 0.5)
                # 打扰成本衰减：typing 时权重 ×0.2，mouse 时 ×0.6
                weight = base_weight * cost
                if random.random() < weight:
                    prompt = rule.get("prompt", "")
                    if prompt:
                        # P0-2 半衰期节流：同一规则 prompt 在硬跳过窗口内不重复触发
                        if self._throttle.should_skip(prompt, kind="chat", now=now):
                            logger.info(
                                "[proactive] %s rule='%s' (hard-skip/half-life)",
                                PROACTIVE_REASON_PASS_THROTTLED, prompt,
                            )
                            continue
                        # P0-2 同会话去重：与近期主动搭话高度相似 → 跳过
                        if self._throttle.is_duplicate(prompt, now=now):
                            logger.info("%s: rule '%s'", DEDUP_LOG_TAG, prompt[:40])
                            continue
                        # P1-5 语义去重：与历史主动搭话话题重复 → 跳过
                        if not self._anti_repeat_allows(prompt, now):
                            continue
                        # P0-1 LLM 生成：规则命中 → 尝试生成更自然文案；失败回退模板池
                        if self.generation_available():
                            context = {
                                "scenario": "rule_fallback",
                                "signals": signals,
                                "fallback_prompt": prompt,
                            }
                            started = self._start_generation(
                                context, fallback_prompt=prompt, source_key=prompt
                            )
                            if started:
                                logger.info(
                                    "Proactive rule -> generation: idle=%ds fg=%s act=%s w=%.2f",
                                    int(conversation_idle), category, activity, weight,
                                )
                                return _GENERATION_PENDING  # 结果异步投递（哨兵停止本轮）
                        self._record_proactive_trigger(now)
                        self._throttle.record_used(prompt, kind="chat", now=now)
                        self._throttle.record_chat(prompt, now=now)
                        self._anti_repeat_record(prompt, now)
                        logger.info(
                            "Proactive triggered: idle=%ds fg=%s act=%s w=%.2f rule='%s'",
                            int(conversation_idle), category, activity, weight, prompt,
                        )
                        self._speak(prompt, prompt)
                        return prompt
        return None
