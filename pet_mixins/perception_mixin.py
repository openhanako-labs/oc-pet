"""PerceptionMixin — 感知与记忆接线（P1 集成层）。

由 PetWindow 多重继承（pet.py 类定义中加入）。方法体内访问
``self.config`` / ``self._perception`` / ``self._proactive`` / ``self._engine``
等均由 PetWindow 提供（鸭子类型）。

## 这一组是干什么的

P1 集成层把五条独立的增强线接进主循环：

| 线 | 模块 | 作用 |
|---|---|---|
| A | `memory_hybrid` + `memory_embedding` | 向量召回（默认关，退化纯 BM25） |
| B | `memory_facts` / `memory_reflection` | 事实库 + 反思引擎 |
| C | `anti_repeat` / `screen` enrich | 反重复 + 屏幕语义增强 |

**共同设计**：全部防御式——任何一线失败只记日志，绝不影响既有功能。
每条线都有 config 开关，默认行为保守（如 embedding 默认关）。

## 为什么单独一个 mixin

这 6 个方法（约 145 行）原本散在 `pet.py` 的对话/动画接线之间。
它们共享同一套模式（读 config → 检查依赖 → 注入 → 记日志），
聚在一起后改感知逻辑不必翻渲染代码。

搬家自 pet.py（2026-09-17，技术债①）。行为零变化。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class PerceptionMixin:
    """P1 感知/记忆集成：反重复 / 屏幕增强 / 事实库 / 反思 / 向量召回。"""

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
