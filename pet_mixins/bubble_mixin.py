"""BubbleMixin — 桌宠气泡 / Hanako 状态呈现。

由 PetWindow 多重继承。访问 self.bubble / self._bubble_timer / self._bubble_message /
self._bubble_priority / self._pending_bubbles / self._reposition_bubble /
self._tts_player / self._perception / self._update_status_indicator / self._set_anim_seq /
self._current_anim / self._current_emotion 等，均由 PetWindow 提供（鸭子类型）。

线程安全：_show_bubble 是线程安全入口，跨线程调用自动走信号绕回主线程
（MultiPetBridge 的 dispatcher 线程会直接调它）。

拆分自 pet.py 的气泡/Hanako 区块（原 1512-1640 行），降低 PetWindow 体积。
"""
import logging
import os
import threading
import time

from PySide6.QtCore import QThread

from config import get_transition_style

logger = logging.getLogger(__name__)


class BubbleMixin:
    """气泡显示与 Hanako 状态呈现。"""

    def _show_bubble(self, text: str, emotion: str = "neutral", priority: int = 0, duration_ms: int = 0, source: str = ""):
        """显示消息气泡 —— 线程安全入口。

        MultiPetBridge 的 dispatcher 线程（pet_enter 事件 -> mission_tracker
        判定任务完成）会直接调到这里。而实现里要 self._bubble_timer.start()，
        从非主线程调用时 Qt 会拒绝：
            QObject::startTimer: Timers cannot be started from another thread
        比警告更糟的是定时器压根没起来，气泡就再也不会自动隐藏。
        所以这里先做线程判定，跨线程一律走信号绕回主线程。

        duration_ms > 0 时覆盖默认时长（如缩放提示 1500ms 短气泡）。
        """
        if not text or not hasattr(self, 'bubble'):
            return
        if QThread.currentThread() is not self.thread():
            self.bubble_signal.emit(str(text), str(emotion), int(priority))
            return
        self._show_bubble_impl(text, emotion, priority, duration_ms, source)

    def _bubble_duration(self, text: str) -> int:
        """根据文本长度计算气泡显示时长（base 10s + 字数系数，封顶 30s）。"""
        base = 10000
        n = len(text or '')
        extra = int(n / 8) * 1000   # 每 8 字加 1s
        return min(base + extra, 30000)

    def _show_bubble_impl(self, text: str, emotion: str = "neutral", priority: int = 0, duration_ms: int = 0, source: str = ""):
        """气泡实现体（仅限主线程调用；相同内容不重复刷新，高优先级不被低优先级覆盖）

        duration_ms > 0 时覆盖默认时长（如缩放提示短气泡）。
        
        P2: 自动解析文本中的 [emotion:xxx] 和 [action:{...}] 标签，
        解析后清除标签显示，并触发动作。
        """
        if not text or not hasattr(self, 'bubble'):
            return
        
        # P2: 解析 [emotion:xxx] 和 [action:{...}] 标签
        import re
        import json
        
        # 解析 emotion 标签
        m = re.search(r'\[emotion:([a-z_]+)\]', text, re.IGNORECASE)
        if m:
            emotion = m.group(1).lower()
            text = re.sub(r'\[emotion:[a-z_]+\]', '', text).strip()
        
        # 解析 action 标签
        m = re.search(r'\[action:(\{.*?\})\]', text, re.DOTALL)
        if m:
            try:
                action_intent = json.loads(m.group(1))
                text = re.sub(r'\[action:\{.*?\}\]', '', text, flags=re.DOTALL).strip()
                # 触发动作
                r = getattr(self, "_renderer", None)
                if r is not None and hasattr(r, "apply_action_intent"):
                    try:
                        r.apply_action_intent(action_intent)
                    except Exception as e:
                        logger.debug("Action from bubble failed: %s", e)
            except Exception:
                pass
        
        # 2026-09-06: 添加触发来源标签（调试用）
        if source and source not in ("user", "system"):
            text = f"[{source}] {text}" 
        
        # 原始逻辑
        now = time.time()
        # P1: 规范化文本（去除多余空格）用于去重
        import re
        normalized_text = re.sub(r'\s+', ' ', text).strip()
        if normalized_text == getattr(self, '_last_bubble_text', '') and (now - getattr(self, '_last_bubble_time', 0)) < 2.0:
            logger.debug("Bubble dedupe: same text within 2s, skipping log")
            self._bubble_timer.start(duration_ms if duration_ms > 0 else self._bubble_duration(text))  # 只续期
            return
        self._last_bubble_text = normalized_text
        self._last_bubble_time = now
        # 节流：相同内容且气泡可见时不重复设置
        if normalized_text == getattr(self, '_bubble_message', '') and self.bubble.isVisible():
            logger.debug("Bubble throttle: same text still visible")
            self._bubble_timer.start(duration_ms if duration_ms > 0 else self._bubble_duration(text))  # 只续期
            return
        # 高优先级正在显示时，低优先级先排队
        if self.bubble.isVisible() and self._bubble_priority > priority:
            logger.debug("Bubble queued (priority %d < current %d): %s", priority, self._bubble_priority, text[:40])
            self._pending_bubbles.append((text, emotion, priority))
            return
        try:
            self._is_thinking = False
            self._bubble_message = text
            self._bubble_priority = priority
            log_level = "debug" if text == "⏳ 思考中..." else "info"
            getattr(logger, log_level)("Showing bubble: %s [emotion=%s]", text[:80], emotion)
            self.bubble.set_text(text, bright=(emotion == "happy"))
            self._reposition_bubble()
            self.bubble.show()
            self.bubble.raise_()
            self._bubble_timer.start(duration_ms if duration_ms > 0 else self._bubble_duration(text))
            # 诊断日志：气泡实际几何 / 可见性 / 透明度 / z-order。
            # 排查「日志显示 Showing bubble 但用户看不到」的场景：
            # (a) 位置越出父窗口被裁 (b) setFixedSize 置 0 (c) windowOpacity 卡 0.0
            # (d) z-order 被 char_label 压住
            try:
                geo = self.bubble.geometry()
                pos_g = self.bubble.mapToGlobal(self.bubble.pos())
                logger.info(
                    "Bubble shown: text=%r parent_visible=%s "
                    "geo=(%d,%d,%dx%d) global=(%d,%d) windowOpacity=%.2f "
                    "z=%d visible=%s",
                    text[:40], self.isVisible(),
                    geo.x(), geo.y(), geo.width(), geo.height(),
                    pos_g.x(), pos_g.y(), self.bubble.windowOpacity(),
                    self.bubble.stackingOrder(),
                    self.bubble.isVisible(),
                )
            except Exception as e:
                logger.debug("Bubble diagnostic log failed: %s", e)
        except Exception:
            logger.exception("Show bubble failed")
        
        # 2026-09-07: 触发 TTS（让 idle chatter、屏幕感知 proactive 等回复也有语音）
        # 但排除系统提示（短气泡、状态提示、操作提示）
        try:
            # 过滤条件：短气泡、状态提示、操作提示不触发 TTS
            _skip_tts = False
            
            # 1. 短气泡（duration_ms > 0）：如缩放提示、工具进度
            if duration_ms > 0:
                _skip_tts = True
            
            # 2. 状态提示：思考中、语音生成中、观察中等
            _status_keywords = ("思考中", "语音生成中", "正在观察", "正在思考", "正在回复", "观察中", "分析中")
            if any(kw in text for kw in _status_keywords):
                _skip_tts = True
            
            # 2026-09-08: 工具执行状态提示：不触发 TTS（如"正在使用 search_memory…"）
            _tool_status_keywords = ("正在使用", "正在执行", "工具执行完成", "工具调用")
            if any(kw in text for kw in _tool_status_keywords):
                _skip_tts = True
            
            # 2026-09-09: 工具执行完成状态：不触发 TTS（如"已为你完成「current_status」"）
            _tool_done_keywords = ("已为你完成", "工具执行", "已完成", "工具调用完成")
            if any(kw in text for kw in _tool_done_keywords):
                _skip_tts = True
            
            # 2026-09-09: 包含工具名称的回复：不触发 TTS（如"已完成「current_status」"）
            _tool_names = ("current_status", "search_memory", "check_pending_tasks", "web_search", "web_fetch")
            if any(tool in text for tool in _tool_names):
                _skip_tts = True
            
            # 2026-09-08: 工具执行失败：不触发 TTS（避免连续播放多个失败语音）
            _error_keywords = ("工具执行失败", "工具调用失败", "工具失败", "⚠️")
            if any(kw in text for kw in _error_keywords):
                _skip_tts = True
            
            # 3. 操作提示：缩放、拖拽、点击等系统操作
            _op_keywords = ("🔍", "缩放", "拖拽", "点击", "右键", "双击", "快捷键")
            if any(kw in text for kw in _op_keywords):
                _skip_tts = True
            
            # 4. 文本太短（< 10 字）：通常是提示，不是对话
            if len(text) < 10:
                _skip_tts = True
            
            if not _skip_tts:
                # P1: TTS 去重 — 同一文本在短时间内不重复调用 TTS
                normalized_tts_text = re.sub(r'\s+', ' ', text).strip()
                now = time.time()
                if (normalized_tts_text == getattr(self, '_last_tts_text', '') and 
                    (now - getattr(self, '_last_tts_time', 0)) < 3.0):
                    logger.debug("TTS dedupe: same text within 3s, skipping")
                else:
                    self._last_tts_text = normalized_tts_text
                    self._last_tts_time = now
                    engine = getattr(self, "_engine", None)
                    if engine and hasattr(engine, "speak"):
                        # 音频合成完成后播放（通过 tts_audio_signal 绕回主线程）
                        def _on_audio(audio_path):
                            try:
                                if audio_path:
                                    self.tts_audio_signal.emit(audio_path)
                            except Exception as e:
                                logger.debug("TTS signal from bubble failed: %s", e)
                        engine.speak(text, emotion=emotion, on_audio=_on_audio)
        except Exception as e:
            logger.debug("TTS trigger from bubble failed: %s", e)
    
    def _show_bubble_stream(self, initial_text: str = "", emotion: str = "neutral", priority: int = 0):
        """P1: 流式气泡开始 — 显示初始文本（空或"思考中"），准备接收后续 chunk"""
        self._is_streaming_bubble = True
        self._stream_bubble_emotion = emotion
        self._stream_bubble_priority = priority
        self._stream_bubble_text = initial_text
        self._show_bubble_impl(initial_text or "…", emotion, priority)
        
    def _append_bubble_text(self, chunk: str):
        """P1: 流式气泡追加 — 追加 chunk 到当前气泡，模拟打字机效果"""
        if not hasattr(self, '_is_streaming_bubble') or not self._is_streaming_bubble:
            return
        self._stream_bubble_text += chunk
        # 节流：每 50ms 最多刷新一次，避免频繁 set_text 卡顿
        now = time.time()
        if now - getattr(self, '_last_stream_update', 0) < 0.05:
            return
        self._last_stream_update = now
        try:
            self._bubble_message = self._stream_bubble_text
            self.bubble.set_text(self._stream_bubble_text, bright=(self._stream_bubble_emotion == "happy"))
            self._reposition_bubble()
            self._bubble_timer.stop()  # 流式期间不自动隐藏
        except Exception:
            logger.debug("Append bubble text failed: %s", chunk[:20])
    
    def _finish_bubble_stream(self):
        """P1: 流式气泡结束 — 停止追加，恢复自动隐藏计时器"""
        if not hasattr(self, '_is_streaming_bubble') or not self._is_streaming_bubble:
            return
        self._is_streaming_bubble = False
        if self._stream_bubble_text:
            duration = self._bubble_duration(self._stream_bubble_text)
            self._bubble_timer.start(duration)
            logger.debug("Stream bubble finished: %d chars", len(self._stream_bubble_text))

    # ── 右键菜单 ──

    def _show_context_menu(self, pos):
        """右键菜单"""
        self._mark_user_interaction()
        if not hasattr(self, '_menu'):
            return
        # 更新动态部分
        if hasattr(self, '_behavior_actions'):
            for mode, a in self._behavior_actions.items():
                a.setChecked(mode == self._behavior_mode)
        if hasattr(self, '_action_menu_items') and hasattr(self, '_action_linker'):
            highlighted = self._action_linker.highlighted_actions
            for aid, a in self._action_menu_items.items():
                a.setVisible(aid in highlighted)
        if hasattr(self, '_passthrough_action'):
            self._passthrough_action.setChecked(self._mousePassthrough)
        # M4: 根据 transport_mode 决定是否启用"新对话"入口
        if hasattr(self, '_new_session_action'):
            hanako_mode = False
            try:
                if self._engine and self._engine._adapter:
                    hanako_mode = getattr(self._engine._adapter, 'transport_mode', 'direct') != 'direct'
            except Exception:
                hanako_mode = False
            self._new_session_action.setVisible(hanako_mode)
        # 任务系统：每次弹出前刷新进度 / 盲盒资源
        if hasattr(self, '_refresh_mission_menu'):
            self._refresh_mission_menu()
        try:
            self._menu.popup(self.mapToGlobal(pos))
        except Exception:
            pass

    # ── Hanako 状态呈现 ──

    def _on_hanako_state(self, anim_name: str, message: str, emotion: str = "neutral", state: str = "idle", audio_path: str = ""):
        """Hanako 状态变化回调 — 从 WS 后台线程调用，通过信号切主线程"""
        self.hanako_state_signal.emit(anim_name, message, emotion, state, audio_path)

    def _do_hanako_state(self, anim_name: str, message: str, emotion: str, state: str, audio_path: str):
        """在主线程处理 Hanako 状态变化"""
        # G：celebrating 分支（在 safe_anims 收窄之前 return；开关关闭则降级旧 happy）。
        # 关键约束：只走 AvatarRenderer 统一接口（_status_mapper.render_for），
        # 绝不 import/触碰渲染器内部实现，不新增渲染线程。
        # P2 节流：避免短时间内多次 tool_end 并发触发 celebrating
        if state == "celebrating":
            now = time.time()
            last_celeb = getattr(self, "_last_celebrating_at", 0.0)
            if now - last_celeb < 5.0:  # 5s 内只触发一次 celebrating
                logger.debug("celebrating 节流：距离上次 %.1fs < 5s，跳过", now - last_celeb)
                return
            self._last_celebrating_at = now
            
            celeb_cfg = self.config.get("celebrating", {}) or {}
            if celeb_cfg.get("enabled", True):
                try:
                    self._update_status_indicator("celebrating")
                except Exception:
                    pass
                # BugFix #5-E：长任务完成带摘要汇报（tool_end details/summary
                # 有实质摘要时显示摘要气泡+TTS；否则维持原庆祝动画）
                summary = self._get_celebration_summary()
                self._do_celebrating(summary=summary)
                return
            # 开关关闭 → 恢复旧 happy 行为（mood=happy, anim=waving）
            state = "happy"
            anim_name = "waving"
            emotion = "happy"
        try:
            self._update_status_indicator(state)
        except Exception:
            pass

        # P2: 触发情绪状态机
        if emotion and emotion != "neutral":
            try:
                self._perception.trigger_emotion(emotion)
            except Exception:
                pass

        # 1. 消息气泡
        show_text = message.strip()
        if show_text:
            try:
                tts_cfg = self.config.get("tts", {})
                if tts_cfg.get("enabled", True) and audio_path:
                    if os.path.exists(audio_path):
                        logger.info("Playing TTS: %s", audio_path)
                        self._last_tts_emotion = emotion or "neutral"
                        self._tts_player.play(audio_path)
                    else:
                        logger.warning("TTS audio not found: %s", audio_path)
                else:
                    if not audio_path:
                        logger.debug("No audio_path in response")
                bubble_priority = 1 if state == "speaking" and show_text else 0
                self._show_bubble(show_text, emotion=emotion, priority=bubble_priority)
            except Exception as e:
                logger.warning("TTS/bubble error: %s", e)

        # 2. 动画(P3: 传递 emotion,支持帧区间)
        # 收窄：surprised/angry 不切瞪眼帧，避免高频瞪眼（只保留气泡情绪）
        # P1 修复：turn_end 走 push_event 直通 idle，若 _current_anim 已是 idle 会跳过
        # 下方 _set_anim_seq，导致前倾/脸红等贴图表情残留。state==idle 统一走
        # renderer._force_idle()（ResetExpressions + FORCE 优先级强制回 idle）。
        # P2-9 修复：回复态显式同步 neutral 到渲染器，防止表情脱钩。
        try:
            if state == "idle":
                renderer = getattr(self, "_renderer", None)
                if renderer is not None and hasattr(renderer, "force_idle"):
                    renderer.force_idle()
                self._current_anim = "idle"
            else:
                # P2-9: emotion 为 neutral 时，即使 anim==_current_anim 也要显式清渲染器表情
                if emotion in ("", "neutral"):
                    renderer = getattr(self, "_renderer", None)
                    if renderer is not None and hasattr(renderer, "set_emotion_expression_only"):
                        renderer.set_emotion_expression_only("neutral")
                elif anim_name != self._current_anim:
                    safe_anims = ['idle', 'walk', 'extra']
                    if anim_name not in safe_anims:
                        anim_name = 'idle'
                    if emotion in ('surprised', 'angry'):
                        anim_name = 'idle'
                    self._current_anim = anim_name
                    self._set_anim_seq(anim_name, emotion=emotion, style=get_transition_style(emotion))
        except Exception:
            pass

        # A2: 情绪过期 — thinking 类情绪"延长而非重置"。
        # hanako_monitor 的 mood 节流是 1s，而 _emotion_expiry_timer 是 3s：
        # 持续 thinking 事件每 1s 到达都会 start(3000) 重置计时器，
        # 导致 _on_emotion_expired 永远不触发、情绪脸持续滞留。
        # 修：thinking 且计时器已在运行时不再重置（保留原到期点），
        # 让表情在 3s 后自然过期回 neutral。
        self._current_emotion = emotion or "neutral"
        if self._current_emotion != "neutral":
            if not (self._current_emotion == "thinking" and self._emotion_expiry_timer.isActive()):
                self._emotion_expiry_timer.start(3000)
        else:
            self._emotion_expiry_timer.stop()

        # 3. 动作联动
        if state in ("working", "listening") and self._action_linker.enabled:
            try:
                self._action_linker.check()
            except Exception:
                pass

        # 4. 重置状态(当收到 Agent 回复时)
        if state == "speaking" and message and self._pending_chat:
            # 重置跟踪
            self._pending_user_msg = ""
            self._pending_emotion = "neutral"
            self._pending_chat = False

        # 5. Hanako 自身状态变化不算用户活动；否则会取消正在生成的闲聊。
        idle_chatter = getattr(self, "_idle_chatter", None)
        if not idle_chatter or not idle_chatter.is_running:
            self._idle_stage = None
            self._last_interaction = time.time()

    def _get_celebration_summary(self) -> str:
        """从 HanakoMonitor 取最近一次 tool_end 摘要（无实质摘要返回空串）。

        BugFix #5-E：tool_end 携带 details/summary 且长度可观时，celebrating
        显示「<摘要>」气泡 + TTS（复用现有庆祝链路的 bubble/TTS 管道），
        否则维持原"完成啦"庆祝动画。
        """
        try:
            monitor = getattr(self, "_hanako_monitor", None)
            if monitor is not None and hasattr(monitor, "get_last_tool_end_summary"):
                return monitor.get_last_tool_end_summary()
        except Exception as e:
            logger.debug("读取 tool_end 摘要失败: %s", e)
        return ""

    # ── G：celebrating（庆祝态）主线程实现 ──

    def _do_celebrating(self, summary: str = ""):
        """G celebrating：撒花动作 + 3s 情绪表情 + 气泡 + 完工音 + 3s 后回 idle。

        BugFix #5-E：summary 非空时气泡/TTS 显示摘要（长任务完成汇报），
        否则维持原「完成啦」庆祝动画。

        硬约束：只通过 AvatarRenderer 统一接口（PetStatusMapper.render_for）驱动，
        不 import/触碰渲染器内部实现（Live2D C 层/渲染线程），不新增渲染线程。
        TTS 合成在后台线程，播放经 tts_celebration_signal 回主线程（绝不直接碰 Qt）。
        """
        # 并发防护：上一次庆祝未结束（3s revert 计时器未到/合成线程未收尾）时
        # 跳过重复触发，避免同一批 tool_end 产生多条庆祝序列和多个合成线程。
        if getattr(self, "_celebration_in_progress", False):
            logger.debug("celebrating 进行中，跳过重复触发")
            return
        # 时间节流兜底（_do_hanako_state 已节流，这里防御其他入口直调）
        _now = time.time()
        if _now - getattr(self, "_last_celebrating_at", 0.0) < 5.0:
            logger.debug("celebrating 节流：5s 内已触发，跳过")
            return
        self._last_celebrating_at = _now
        self._celebration_in_progress = True
        from PySide6.QtCore import QTimer
        QTimer.singleShot(3000, lambda: setattr(self, "_celebration_in_progress", False))
        # 1. 双形态撒花动作（统一接口）
        try:
            mapper = getattr(self, "_status_mapper", None)
            if mapper is not None and hasattr(self, "_renderer"):
                mapper.render_for("celebrating", self._renderer)
        except Exception:
            pass
        # 2. 情绪/表情脸（3s 过期，复用现有机制）
        try:
            self._set_surface_emotion("happy", duration_ms=3000, source="celebrating")
        except Exception:
            pass
        # 3. 气泡（完工反馈；BugFix #5-E：有摘要显示摘要）
        try:
            bubble_text = (summary or "").strip() or "完成啦！"
            self._show_bubble(bubble_text, emotion="happy", priority=1)
        except Exception:
            pass
        # 4. TTS 完工音：走现有 tts provider 管道，非阻塞（后台合成 → 信号回主线程）
        try:
            celeb_cfg = self.config.get("celebrating", {}) or {}
            tts_cfg = self.config.get("tts", {}) or {}
            if celeb_cfg.get("tts_enabled", True) and tts_cfg.get("enabled", True):
                threading.Thread(
                    target=self._synth_celebration_tts,
                    args=((summary or "").strip() or "完成啦！",),
                    daemon=True,
                ).start()
        except Exception:
            pass
        # 5. 3s 后回 idle（复用 _pet_revert_timer，现有防御已覆盖 Live2D 手势超时）
        try:
            self._pet_revert_timer.stop()
            self._pet_revert_timer.start(3000)
        except Exception:
            pass

    def _synth_celebration_tts(self, text: str = "完成啦！"):
        """后台线程：合成完工音/摘要 → 信号回主线程播放（绝不直接碰 Qt/渲染）。

        BugFix #5-E：text 可传入摘要文本（长任务完成汇报）；缺省维持"完成啦！"。

        合成不在主线程执行（cosyvoice 等 provider 的重型链路会冻住事件循环）；
        播放经 tts_celebration_signal（Qt Signal）自动转主线程。
        """
        try:
            provider = None
            engine = getattr(self, "_engine", None)
            if engine is not None:
                provider = getattr(engine, "_tts", None) or getattr(engine, "_tts_provider", None)
            if provider is None:
                provider = getattr(self, "_tts_provider", None)
            if provider is None or not hasattr(provider, "synthesize"):
                return
            # P2-7: 完工音也按角色音色解析（空 = provider 默认；失败同样回退）
            _voice = ""
            _resolver = getattr(self, "_resolve_tts_voice", None)
            if callable(_resolver):
                try:
                    _voice = _resolver(getattr(self, "_current_char", ""), "happy") or ""
                except Exception:
                    _voice = ""
            audio = provider.synthesize(
                text or "完成啦！", character_id=getattr(self, "_current_char", ""), voice=_voice,
            )
            if audio and os.path.exists(audio):
                self.tts_celebration_signal.emit(audio)
        except Exception as e:
            logger.debug("完工音合成失败（忽略）: %s", e)

    def _do_tts_celebration(self, audio_path: str):
        """主线程槽：播放完工音（TTS 合成已在后台线程完成）。"""
        try:
            if not audio_path or not os.path.exists(audio_path):
                return
            tts_cfg = self.config.get("tts", {}) or {}
            if not tts_cfg.get("enabled", True):
                return
            self._tts_player.stop()
            self._last_tts_emotion = "happy"
            self._tts_player.play(audio_path)
        except Exception as e:
            logger.debug("完工音播放失败（忽略）: %s", e)