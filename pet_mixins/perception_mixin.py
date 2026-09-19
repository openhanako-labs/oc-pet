"""PerceptionMixin — 感知与记忆接线（P1 集成层 + 调度器）。

由 PetWindow 多重继承（pet.py 类定义中加入）。方法体内访问
``self.config`` / ``self._perception`` / ``self._proactive`` / ``self._engine``
等均由 PetWindow 提供（鸭子类型）。

## 包含两部分

### 1. 调度器与感知控制器（`_init_schedulers`）

建立桌宠的「心跳源」：

| 组件 | 作用 |
|---|---|
| `ActionLinker` | 动作联动（感知→动作） |
| `ForegroundWatcher` | 前台窗口检测（Win32） |
| `ProactiveScheduler` | 主动对话（会说话） |
| `DialogueScheduler` | 统一对话调度 |
| `PresenceScheduler` | 轻存在感（只做微动作，不说话） |
| `PerceptionController` | 感知中枢（时间/情绪/日程/屏幕） |

### 2. P1 集成层（`_init_neko_p1` 及五条线）

| 线 | 模块 | 作用 |
|---|---|---|
| A | `memory_hybrid` + `memory_embedding` | 向量召回（默认关，退化纯 BM25） |
| B | `memory_facts` / `memory_reflection` | 事实库 + 反思引擎 |
| C | `anti_repeat` / `screen` enrich | 反重复 + 屏幕语义增强 |

**共同设计**：全部防御式——任何一线失败只记日志，绝不影响既有功能。

## 依赖顺序（重要）

`_init_schedulers` 读取 `self._diag_disable_perception`，该字段由
`_init_diag_switches` 设置——`__init__` 保证前者先调用。

搬家自 pet.py（2026-09-17，技术债①）。行为零变化。
"""
from __future__ import annotations

import logging
import time

from PySide6.QtCore import QTimer

from config import load_config
from core.perception.controller import PerceptionController
from core.perception.proactive import ProactiveScheduler
from motion.action_linker import ActionLinker
from motion.foreground_watcher import ForegroundWatcher

logger = logging.getLogger(__name__)


class PerceptionMixin:
    """调度器与感知：动作联动 / 前景检测 / 主动对话 / 存在感 / 感知中枢 + P1 集成。"""

    # ── 调度器与感知控制器 ──────────────────────────────────

    def _init_schedulers(self):
        """动作联动/前景检测/Proactive/Presence/感知控制器（与 __init__ 原顺序一致）。"""
        # ── 动作联动 ──
        al_cfg = self.config.get("action_linker", {})
        self._action_linker = ActionLinker(
            character_id=self._current_char,
            highlight_duration=al_cfg.get("highlight_duration", 30),
            enabled=al_cfg.get("enabled", True),
        )

        # ── 前景窗口检测 ──
        self._foreground_watcher = ForegroundWatcher()
        self._foreground_watcher.on_change = self._on_foreground_change
        self._foreground_watcher.start()
        self._foreground_timer = QTimer(self)
        self._foreground_timer.timeout.connect(self._foreground_tick)

        # ── Proactive 主动对话调度器(P1)──
        proactive_cfg = self.config.get("proactive", {})
        self._proactive_cfg = proactive_cfg  # 供 _init_visual_startup 使用
        # 活动感知（打字/划水/空闲）：零成本，给 Proactive 提供打扰成本维度
        try:
            from motion.activity_tracker import ActivityTracker
            self._activity_tracker = ActivityTracker()
        except Exception:
            self._activity_tracker = None
        self._proactive = ProactiveScheduler(
            foreground_watcher=self._foreground_watcher,
            on_proactive=self._on_proactive_trigger,
            activity_tracker=self._activity_tracker,
        )
        self._proactive.load_config(proactive_cfg)

        # 2026-09-06: P1 统一调度器（DialogueScheduler）
        try:
            from core.dialogue_scheduler import DialogueScheduler
            self._scheduler = DialogueScheduler()
            self._scheduler.load_config(self.config)
        except Exception as e:
            logger.warning("Failed to init DialogueScheduler: %s", e)
            self._scheduler = None
        self._proactive_grace = time.time() + 120  # 启动后 2 分钟内不触发主动对话
        # T02 P0-1：LLM 生成器注入（复用 Hanako 通道 source="proactive"）。
        # 适配器在 _init_engine 里由 ConversationEngine.start() 创建，因此延迟到
        # _init_engine 末尾统一注入（见 _inject_proactive_generator）。

        # ── Presence 轻存在感调度器（不同于 proactive：不说话只做动作）──
        # 与主动对话互补：proactive 会打断（说话），presence 只在空闲时做微动作，
        # 让角色“在线”。“对话中暂停”通过 _mark_user_interaction → mark_interaction 实现。
        self._presence = None
        self._presence_timer = None
        # P2: 动作冷却追踪（连续触发合并成一次，防连发）
        # {action_id: last_trigger_time}
        self._action_cooldowns: dict[str, float] = {}
        self._action_cooldown_sec: float = 2.0  # 默认 2 秒冷却
        try:
            from core.presence import PresenceScheduler
            self._presence = PresenceScheduler(on_presence=self._on_presence_action)
            self._presence.load_config(self.config.get("presence", {}) or {})
            self._presence_timer = QTimer(self)
            self._presence_timer.timeout.connect(self._presence_tick)
            self._presence_timer.start(60_000)  # 每 60s 检查一次空闲状态
        except Exception as e:
            logger.warning("Presence 初始化失败（非致命）: %s", e)

        # ── 感知控制器(P2: 时间 + 情绪状态机 + 日程)──
        # 定时/巡检读取绑定的 Hanako agent：与对话后端一致（默认 ophelia），
        # 而非显示角色 miku（miku 在 ~/.hanako/agents/ 下无目录 → 读空）。
        _dlg_agent = ""
        try:
            _dlg_agent = (load_config().get("dialog", {}) or {}).get("agent_id", "") or ""
        except Exception:
            _dlg_agent = ""
        if not _dlg_agent:
            _dlg_agent = self._current_char
        self._perception = PerceptionController(self._current_char, agent_id=_dlg_agent)
        # BugFix #5-D：Hanako 任务巡检命中 → 主动汇报（复用 proactive 触发链路）
        try:
            self._perception.set_inspection_callback(self._on_proactive_trigger)
        except Exception as e:
            logger.debug("Inspection callback wiring failed: %s", e)
        # 屏幕内容→情绪回调
        self._perception.screen.on_emotion = self._on_screen_emotion
        self._perception.screen.on_screen_proactive = self._on_screen_proactive
        self._perception.screen.on_update = self._on_screen_update
        # 2026-09-14 对话避让：屏幕 LLM 增强与用户回复抢同一条 API
        # （实测 Vision API timeout 与回复同时段），对话进行中让路。
        try:
            self._perception.screen.busy_check = self._is_conversation_busy
        except Exception as e:
            logger.debug("屏幕感知避让钩子注入失败（非致命）: %s", e)

        # ── 屏幕感知开关（从配置读取）──
        screen_cfg = self.config.get("screen", {})
        if not screen_cfg.get("enabled", True):
            self._perception.screen.disable()
            logger.info("Screen perception disabled by config")
        # P0 调试：环境变量强制禁用感知（二分定位用）
        if self._diag_disable_perception:
            self._perception.screen.disable()
            logger.warning("Screen perception DISABLED via OC_DISABLE_PERCEPTION=1")
        # ── 媒体播放感知（SMTC）──
        try:
            self._perception.media.start()
            logger.info("MediaPerception (SMTC) started")
        except Exception as e:
            logger.debug("MediaPerception start failed: %s", e)

        # 截图保护开关（默认全关，配置开启）
        if screen_cfg.get("blur", False):
            self._perception.screen.set_blur(True)
        if screen_cfg.get("blacklist", False):
            self._perception.screen.set_blacklist(True)
        if not screen_cfg.get("compress", True):
            self._perception.screen.set_compress(False)
        # P1 感知哈希近邻去重（0=关）：像素变了但画面没变时不打视觉 API
        try:
            if hasattr(self._perception.screen, "set_phash_threshold"):
                self._perception.screen.set_phash_threshold(
                    int(screen_cfg.get("phash_threshold", 0) or 0))
        except Exception:
            logger.debug("pet: 非致命异常(已静默吞掉)", exc_info=True)
        # 截屏间隔（随机范围优先，缺省 interval±30%）
        try:
            _iv = int(screen_cfg.get("interval", 120) or 120)
            _lo = screen_cfg.get("interval_min")
            _hi = screen_cfg.get("interval_max")
            if _lo and _hi:
                self._perception.screen.set_interval_range(int(_lo), int(_hi))
            else:
                self._perception.screen.set_interval(_iv)
        except Exception:
            self._perception.screen.set_interval(120)

    # ── P1 集成总入口 ──────────────────────────────────────

    def _init_neko_p1(self):
        """P1 集成总入口：反重复 / 屏幕感知升级 / 事实库 / 反思引擎 / 向量嵌入确认。"""
        self._init_p1_anti_repeat()
        self._init_p1_screen_enrich()
        self._init_p1_fact_store()
        self._init_p1_reflection()
        self._init_p1_embedding_check()

    # ── C 线：反重复 + 屏幕增强 ────────────────────────────

    def _init_p1_anti_repeat(self):
        """C 线 P1-5：proactive 注入 AntiRepeatCorpus（语义指纹 + 时间窗去重）。"""
        try:
            from core.anti_repeat import get_anti_repeat_corpus
            if not getattr(self, "_proactive", None):
                return
            if not (self.config.get("anti_repeat", {}) or {}).get("enabled", True):
                logger.info("P1 anti_repeat disabled by config")
                return
            corpus = get_anti_repeat_corpus()
            self._proactive.set_anti_repeat(corpus, self._current_char)
            logger.info("P1 anti_repeat injected (agent=%s)", self._current_char)
        except Exception as e:
            logger.warning("P1 anti_repeat 注入失败（非致命）: %s", e)

    def _init_p1_screen_enrich(self):
        """C 线 P1-6：屏幕感知 → proactive 场景 provider + LLM 语义增强 provider。

        - ``proactive.set_screen_scene_provider(screen.get_scene_snapshot)``：
          场景快照并入 proactive signals（screen_scene/screen_intent/confidence）。
        - ``screen.set_enrich_provider(adapter 包装 source="screen_enrich")``：
          语义增强走 ``chat_direct`` 直连（不写 Hanako 会话历史，与
          proactive/idle 同策略），失败/超时自动退化纯规则分类。
        """
        try:
            screen = getattr(getattr(self, "_perception", None), "screen", None)
            if screen is None:
                return
            proactive = getattr(self, "_proactive", None)
            if proactive is not None:
                proactive.set_screen_scene_provider(screen.get_scene_snapshot)
            screen_cfg = self.config.get("screen", {}) or {}
            llm_enrich = bool(screen_cfg.get("llm_enrich", True))
            screen.set_llm_enrich(llm_enrich)
            # 429 限流缓解：LLM 语义增强冷却（秒）。场景未变化时最多每 N 秒补一次，
            # 避免"每次截图 = 视觉 API + enrich LLM 两次请求"的高频打满限流。
            # hasattr 兜底：兼容未实现该方法的 duck-typed screen（测试 fake 等）。
            try:
                if hasattr(screen, "set_enrich_cooldown"):
                    screen.set_enrich_cooldown(int(screen_cfg.get("llm_enrich_cooldown", 300) or 300))
            except Exception:
                logger.debug("pet: 非致命异常(已静默吞掉)", exc_info=True)
            adapter = getattr(getattr(self, "_engine", None), "_adapter", None)
            if llm_enrich and adapter is not None:
                def _screen_enrich_provider(prompt: str):
                    try:
                        reply, _emotion = adapter.chat_direct(
                            prompt, inject_memory=False, source="screen_enrich",
                        )
                        return (reply or "").strip() or None
                    except Exception:
                        return None
                screen.set_enrich_provider(_screen_enrich_provider)
            else:
                screen.set_enrich_provider(None)
            logger.info("P1 screen enrich injected (llm_enrich=%s, adapter=%s)",
                        llm_enrich, "yes" if adapter else "no")
        except Exception as e:
            logger.warning("P1 屏幕感知升级接线失败（非致命）: %s", e)

    # ── B 线：事实库 + 反思引擎 ────────────────────────────

    def _init_p1_fact_store(self):
        """B 线 P1-2：FactStore 注入 + 对话事实记录钩子。"""
        try:
            facts_cfg = (self.config.get("memory", {}) or {}).get("facts", {}) or {}
            if not facts_cfg.get("enabled", True):
                logger.info("P1 FactStore disabled by config")
                self._fact_store = None
                return
            from core.memory_facts import FactStore
            adapter = getattr(getattr(self, "_engine", None), "_adapter", None)
            self._fact_store = FactStore(
                agent_id=self._agent_id,
                adapter=adapter,
                use_qt_bridge=True,
            )
            self._fact_store.set_changed_callback(self._on_fact_store_changed)
            logger.info("P1 FactStore ready (agent=%s, adapter=%s)",
                        self._agent_id, "yes" if adapter else "no")
        except Exception as e:
            logger.warning("P1 FactStore 初始化失败（非致命）: %s", e)
            self._fact_store = None

    def _init_p1_reflection(self):
        """B 线 P1-3：ReflectionEngine 注入 + 定时触发（presence 60s tick）。"""
        try:
            refl_cfg = (self.config.get("memory", {}) or {}).get("reflection", {}) or {}
            if not refl_cfg.get("enabled", True):
                logger.info("P1 ReflectionEngine disabled by config")
                self._reflection_engine = None
                return
            from core.memory_reflection import ReflectionEngine
            adapter = getattr(getattr(self, "_engine", None), "_adapter", None)
            self._reflection_engine = ReflectionEngine(
                agent_id=self._agent_id,
                adapter=adapter,
                event_source=getattr(self, "_event_stream", None),
                use_qt_bridge=True,
            )
            self._reflection_engine.set_changed_callback(self._on_reflection_changed)
            logger.info("P1 ReflectionEngine ready (agent=%s, adapter=%s)",
                        self._agent_id, "yes" if adapter else "no")
        except Exception as e:
            logger.warning("P1 ReflectionEngine 初始化失败（非致命）: %s", e)
            self._reflection_engine = None

    # ── A 线：向量召回确认 ─────────────────────────────────

    def _init_p1_embedding_check(self):
        """A 线 P1-1：确认 HybridMemoryRecall 默认 embedding provider 已接。

        ``core.memory_hybrid.HybridMemoryRecall`` 构造时已默认调用
        ``memory_embedding.default_embedding_provider()``（config
        ``memory.embedding.enabled=False`` → None → 纯 BM25 退化）。此处只做
        确认与日志，不强制启用（用户后续自行开）。
        """
        try:
            from core.memory_hybrid import _default_embedding_provider
            provider = _default_embedding_provider()
            emb_cfg = (self.config.get("memory", {}) or {}).get("embedding", {}) or {}
            enabled = bool(emb_cfg.get("enabled", False))
            if provider is not None:
                logger.info("P1 embedding provider available (enabled=%s)", enabled)
            else:
                logger.info("P1 embedding provider 未启用（memory.embedding.enabled=false），hybrid 走纯 BM25")
        except Exception as e:
            logger.debug("P1 embedding provider 检查跳过: %s", e)
