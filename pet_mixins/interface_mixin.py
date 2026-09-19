"""InterfaceMixin — 对外接口层（MCP 提供方 / 本地状态口 / 通用外部触发）。

由 PetWindow 多重继承（pet.py 类定义中加入）。方法体内访问
``self.config`` / ``self._perception`` / ``self._renderer`` / ``self._show_bubble``
等均由 PetWindow 提供（鸭子类型）。

## 为什么单独一个 mixin

这三条线（8979 MCP / 8977 状态口 / 8988 外部触发）是**桌宠的对外边界**：
- 它们全默认关闭（config 里 enabled=false），零行为、不占端口
- 它们都是「HTTP/MCP 线程 → 事件总线或 QTimer → 主线程应用」的同一范式
- 它们与桌宠内部逻辑（对话/感知/渲染）几乎无耦合

原本这三组 `_init_*` + 辅助方法（约 220 行）散落在 `pet.py` 里，
与对话、动画、渲染的接线混在一起。搬到此处后，改对外接口不必翻对话代码。

## 线程约束（重要）

- `_init_*` 只在 Qt 主线程调用（PetWindow.__init__ 内）
- `*_action_sink` / `*_state_provider` 会被 **HTTP/MCP 线程**调用——
  它们只做「发事件」或「读快照」，绝不直接碰 Qt 对象
- 真正的动作应用（`_apply_mcp_action` / `_apply_external_trigger`）经
  QTimer.singleShot(0, ...) 或 EventBus 绕回主线程

搬家自 pet.py（2026-09-17，技术债①）。行为零变化。
"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)


class InterfaceMixin:
    """对外接口：MCP 提供方 / 本地状态口 / 通用外部触发。"""

    # ── W1a：桌宠作为 MCP 提供方（8979）─────────────────────

    def _init_mcp_server(self):
        """W1a（2026-09-14）：启动桌宠 MCP 提供方，让 Hana 看见桌宠。

        背景：桌宠一直只做 MCP **消费方**（skyrim_bridge 连 SkyrimNet），
        Hana 侧对桌宠一无所知。这里反过来把桌宠自己的状态/能力/表现暴露成 MCP 工具。

        默认关（config mcp_server.enabled=false），零行为、不占端口。
        线程安全：action_sink 只做 EventBus.emit，实际动作由主线程订阅者执行。
        """
        try:
            from core.mcp_server import build_from_config
            srv = build_from_config(
                self.config,
                state_provider=self._status_snapshot,
                capabilities_provider=self._mcp_capabilities,
                action_sink=self._mcp_action_sink,
                catalog_provider=self._mcp_hana_catalog,
            )
            if srv is None:
                self._mcp_server = None
                return
            # 主线程订阅：MCP 线程 emit → QTimer 转主线程执行
            from core.event_bus import EventBus

            def _on_mcp_action(action, params):
                try:
                    from PySide6.QtCore import QTimer
                    QTimer.singleShot(
                        0, lambda: self._apply_mcp_action(action, params)
                    )
                except Exception as e:
                    logger.warning("MCP 动作调度失败: %s", e)

            self._mcp_action_handler = _on_mcp_action
            EventBus.on("mcp_action", _on_mcp_action)
            srv.start()
            self._mcp_server = srv
        except Exception as e:
            logger.warning("MCP server 启动失败（非致命）: %s", e)
            self._mcp_server = None

    def _mcp_capabilities(self) -> list:
        """MCP 用能力清单。

        分两部分：
        - 桌宠**自己的**内部能力（CAPABILITIES）
        - **Hana 的**全体系摘要（DISC-2）——让 Hana 自己也能看到"我有什么"
        """
        out: list = []
        try:
            from core.capability_registry import CAPABILITIES
            out.extend(
                {"name": c.name, "description": c.description or "", "source": "pet"}
                for c in CAPABILITIES
            )
        except Exception as e:
            logger.warning("MCP 能力清单读取失败: %s", e)
        # DISC-2：附上 Hana 全体系摘要（不附明细，避免 token 爆炸）
        try:
            from core.hana_catalog import get_catalog
            cat = get_catalog()
            out.append({
                "name": "hana_catalog",
                "description": (
                    f"Hana 全体系目录（plugins={cat['totals']['plugins']}, "
                    f"apps={cat['totals']['apps']}, mcp={cat['totals']['mcp_connectors']}, "
                    f"skills={cat['totals']['skills']}, agents={cat['totals']['agents']}）；"
                    "用 pet_hana_catalog 取明细"
                ),
                "source": "hana",
            })
        except Exception as e:
            logger.debug("Hana 目录摘要读取失败: %s", e)
        return out

    def _mcp_hana_catalog(self, system: str = "") -> dict:
        """取 Hana 全体系目录明细（供 MCP 工具调用）。"""
        try:
            from core.hana_catalog import get_catalog
            cat = get_catalog()
        except Exception as e:
            return {"error": str(e)[:200]}
        s = (system or "").strip().lower()
        if s in ("plugins", "apps", "mcp", "skills", "agents"):
            return {s: cat[s]}
        if s == "summary" or not s:
            return {
                "totals": cat["totals"],
                "hana_server_reachable": cat["hana_server_reachable"],
            }
        return {"error": f"未知体系: {system}（可用: plugins/apps/mcp/skills/agents/summary）"}

    def _mcp_action_sink(self, action: str, params: dict) -> str:
        """MCP 写操作入口（MCP 线程调用）。只发事件，立即返回。"""
        from core.event_bus import EventBus
        EventBus.emit("mcp_action", action=action, params=dict(params or {}))
        return f"已派发: {action}"

    def _apply_mcp_action(self, action: str, params: dict) -> None:
        """主线程应用 MCP 动作（白名单已在 mcp_server 侧校验）。"""
        try:
            if action == "set_emotion":
                emo = str(params.get("emotion") or "neutral")
                try:
                    inten = float(params.get("intensity", 1.0))
                except (TypeError, ValueError):
                    inten = 1.0
                if hasattr(self, "_set_surface_emotion"):
                    self._set_surface_emotion(emo, duration_ms=2500)
                r = getattr(self, "_renderer", None)
                if r is not None and hasattr(r, "set_emotion"):
                    r.set_emotion(emo, inten)
            elif action == "play_anim":
                anim = str(params.get("anim") or "idle")
                if hasattr(self, "_set_anim_seq"):
                    self._set_anim_seq(anim)
            elif action == "expression":
                name = str(params.get("name") or "")
                r = getattr(self, "_renderer", None)
                if name and r is not None and hasattr(r, "_apply_expression"):
                    r._apply_expression(name)
            elif action == "say":
                text = str(params.get("text") or "")
                if text:
                    self._show_bubble(text)
            elif action == "celebrate":
                if hasattr(self, "_celebrate"):
                    self._celebrate()
            elif action == "idle":
                if hasattr(self, "_set_anim_seq"):
                    self._set_anim_seq("idle")
                r = getattr(self, "_renderer", None)
                if r is not None and hasattr(r, "set_emotion_expression_only"):
                    r.set_emotion_expression_only("neutral")
            logger.info("MCP 动作已应用: %s %s", action, params)
        except Exception as e:
            logger.warning("MCP 动作应用失败 (%s): %s", action, e)

    # ── F：本地状态口（8977）──────────────────────────────

    def _init_status_http(self):
        """按 config.state_http.enabled 启动本地状态口（默认关，不占端口）。"""
        try:
            sh_cfg = self.config.get("state_http", {}) or {}
            if not sh_cfg.get("enabled", False):
                return
            from core.status_http_server import PetStatusHTTPServer
            self._status_http = PetStatusHTTPServer(
                state_provider=self._status_snapshot,
                auth_token=sh_cfg.get("auth_token", ""),
                port=int(sh_cfg.get("port", 8977) or 8977),
                allow_set_mode=bool(sh_cfg.get("allow_set_mode", False)),
                memory_provider=self._memory_snapshot,
                memory_recall_provider=self._memory_recall,
            )
            self._status_http.start()
        except Exception as e:
            logger.warning("F 本地状态口启动失败（非致命）: %s", e)
            self._status_http = None

    def _memory_snapshot(self, query: str = "") -> dict:
        """记忆检视快照（F GET /pet/memory 只读输出）。

        暴露桌宠自己记的事实与场景，便于在浏览器/curl 里看它到底记了什么。
        ``query`` 非空时按关键词过滤（事实走 FactStore.search；场景按
        label/scenario/category/tags/topics 子串匹配）。只读，不写盘、不走网络。
        """
        from dataclasses import asdict
        q = (query or "").strip()
        out: dict = {"ok": True, "query": q, "facts": [], "scenes": []}
        try:
            fs = getattr(self, "_fact_store", None)
            if fs is not None:
                out["facts"] = fs.search(q, limit=50) if q else fs.get_facts(limit=50)
        except Exception as e:
            out["facts_error"] = str(e)[:200]
        try:
            sm = getattr(self, "_scene_memory", None)
            if sm is not None:
                scenes = list(sm.scenes)
                if q:
                    ql = q.lower()

                    def _hit(s) -> bool:
                        blob = " ".join(
                            [str(s.label), str(s.scenario), str(s.category)]
                            + [str(t) for t in (s.tags or [])]
                            + [str(t) for t in (s.topics or [])]
                        ).lower()
                        return ql in blob

                    scenes = [s for s in scenes if _hit(s)]
                out["scenes"] = [asdict(s) for s in scenes][:50]
        except Exception as e:
            out["scenes_error"] = str(e)[:200]
        return out

    def _memory_recall(self, query: str = "") -> dict:
        """混合召回可视化（F GET /pet/memory/recall?q=...）。

        把桌宠的事实 + 场景拼成候选池，跑一次 ``HybridMemoryRecall``
        （BM25 + cosine + RRF），返回带得分的命中排序——用于看清「一句话
        到底唤起了哪些记忆」。只读，不写盘。
        """
        q = (query or "").strip()
        if not q:
            return {"ok": False, "error": "缺少查询词 q"}
        pool: list = []
        try:
            fs = getattr(self, "_fact_store", None)
            if fs is not None:
                for f in fs.get_facts(limit=200):
                    parts = [str(f.get(k, "")) for k in
                             ("text", "subject", "predicate", "object", "topic") if f.get(k)]
                    pool.append({"id": f"fact:{f.get('id', '')}", "kind": "fact",
                                 "text": " ".join(parts),
                                 "importance": f.get("importance"),
                                 "confidence": f.get("confidence")})
        except Exception as e:
            logger.debug("recall: 读事实失败: %s", e)
        try:
            sm = getattr(self, "_scene_memory", None)
            if sm is not None:
                for s in sm.scenes:
                    parts = ([str(s.label), str(s.scenario), str(s.category)]
                             + [str(t) for t in (s.tags or [])]
                             + [str(t) for t in (s.topics or [])])
                    pool.append({"id": f"scene:{s.scene_id}", "kind": "scene",
                                 "text": " ".join(p for p in parts if p),
                                 "count": getattr(s, "count", 0)})
        except Exception as e:
            logger.debug("recall: 读场景失败: %s", e)
        try:
            from core.memory_hybrid import HybridMemoryRecall, default_score_patch
            patch = default_score_patch()
            hits = HybridMemoryRecall(score_patch=patch).recall(q, pool)
        except Exception as e:
            return {"ok": False, "error": f"召回失败: {e}"}
        return {
            "ok": True, "query": q, "pool_size": len(pool),
            "weighted": patch is not None,
            "hits": [
                {"id": d.get("id"), "kind": d.get("kind"),
                 "score": round(float(d.get("_rrf_score", 0.0) or 0.0), 4),
                 "raw_score": (None if d.get("_rrf_raw") is None
                               else round(float(d["_rrf_raw"]), 4)),
                 "text": (d.get("text", "") or "")[:160]}
                for d in hits[:20]
            ],
        }

    def _status_snapshot(self) -> dict:
        """状态快照（F GET /pet/state 只读输出；MCP ``pet_state`` 工具复用同一份）。

        ``screen`` 段是**拉取路径**的关键：它给外部（agent / HTTP 客户端）一个
        「廉价读一下刚才看到了什么」的口子——直接读缓存（``last_description`` /
        ``get_scene_snapshot``），**不触发新截图、不打视觉 API**。

        线程约束：本方法会被 HTTP/MCP 线程调用，因此只读缓存、绝不碰 Qt 对象
        （screen 侧的读访问均有锁保护）。
        """
        state = "idle"
        try:
            mapper = getattr(self, "_status_mapper", None)
            if mapper is not None:
                state = mapper.current()
        except Exception:
            state = "idle"
        emotion = getattr(self, "_current_emotion", "neutral") or "neutral"
        anim = getattr(self, "_current_anim", "idle") or "idle"
        scenario = ""
        try:
            scenario = getattr(self._perception, "_scenario", "") or ""
        except Exception:
            scenario = ""
        celebrating_active = bool(state == "celebrating")
        return {
            "state": state,
            "emotion": emotion,
            "anim": anim,
            "scenario": scenario,
            "agent_id": self._agent_id,
            "renderer_format": self._renderer_format(),
            "celebrating_active": celebrating_active,
            "screen": self._screen_snapshot(),
            "ts": time.time(),
        }

    def _screen_snapshot(self) -> dict:
        """当前屏幕观察的只读快照（缓存读取，零成本）。

        供两种拉取口：``GET /pet/state`` 与 MCP ``pet_state``。
        无缓存观时返回空 dict（而不是 None）——调用方不用判空。
        """
        out: dict = {}
        try:
            scr = getattr(getattr(self, "_perception", None), "_screen", None)
            if scr is None:
                return out
            try:
                desc = scr.last_description or ""
            except Exception:
                desc = ""
            if desc:
                out["last_description"] = desc
            try:
                getter = getattr(scr, "get_scene_snapshot", None)
                scene = getter() if callable(getter) else None
                if scene:
                    out["scene"] = scene
            except Exception:
                logger.debug("pet: 非致命异常(已静默吞掉)", exc_info=True)
        except Exception:
            logger.debug("pet: 非致命异常(已静默吞掉)", exc_info=True)
        return out

    def _do_pet_set_mode(self, mode: str):
        """主线程槽：登记状态 + 经状态语义层下发（只走统一接口）。"""
        try:
            mapper = getattr(self, "_status_mapper", None)
            if mapper is None:
                return
            mapper.set_state(mode)
            if hasattr(self, "_renderer"):
                mapper.render_for(mode, self._renderer)
        except Exception as e:
            logger.debug("F set-mode 主线程执行失败: %s", e)

    # ── P4/P6：通用外部触发（8988）────────────────────────

    def _init_external_trigger(self):
        """按 config.external_trigger.enabled 启动通用外部触发接收器（默认关）。

        任何外部调度器 POST /trigger 推送给桌宠，回调经 QTimer 转主线程后
        驱动气泡 + 情绪动画；桌宠本地提醒保持自包含，此入口纯通用附加。
        """
        try:
            et_cfg = self.config.get("external_trigger", {}) or {}
            if not et_cfg.get("enabled", False):
                return
            from core.external_trigger_receiver import ExternalTriggerReceiver
            from core.event_bus import EventBus
            # P6: 订阅 EventBus 上的 external_trigger 事件（与 phone_receiver 共享）
            def _on_external_trigger_event(action, text, emotion, source="unknown"):
                try:
                    from PySide6.QtCore import QTimer
                    QTimer.singleShot(0, lambda: self._apply_external_trigger(action, text, emotion, source))
                except Exception as e:
                    logger.warning("外部触发调度失败（via EventBus）: %s", e)
            self._ext_trigger_event_handler = _on_external_trigger_event
            EventBus.on("external_trigger", _on_external_trigger_event)
            self._external_trigger = ExternalTriggerReceiver(
                on_trigger=lambda a, t, e: None,  # 已改走 EventBus，on_trigger 空操作
                auth_token=et_cfg.get("auth_token", ""),
                port=int(et_cfg.get("port", 8988) or 8988),
            )
            self._external_trigger.start()
            logger.info("P4/P6 通用外部触发入口已启动: port=%s, EventBus 已订阅", et_cfg.get("port", 8988))
        except Exception as e:
            logger.warning("P4 通用外部触发入口启动失败（非致命）: %s", e)
            self._external_trigger = None

    def _on_external_trigger(self, action: str, text: str, emotion: str):
        """外部触发回调（HTTP 线程）→ QTimer 转主线程应用。"""
        try:
            from PySide6.QtCore import QTimer
            QTimer.singleShot(0, lambda: self._apply_external_trigger(action, text, emotion))
        except Exception as e:
            logger.warning("外部触发调度失败: %s", e)

    def _apply_external_trigger(self, action: str, text: str, emotion: str, source: str = "unknown"):
        """主线程应用外部触发：气泡 + 情绪动画（非致命包裹）。"""
        try:
            emo = emotion or "neutral"
            if text:
                self._show_bubble(text, emotion=emo)
            if emo != "neutral" and hasattr(self, "_set_surface_emotion"):
                self._set_surface_emotion(emo, duration_ms=2500)
            logger.info("外部触发 [%s]: action=%s source=%s text=%s", action, action, source, text[:30])
        except Exception as e:
            logger.warning("外部触发应用失败: %s", e)
