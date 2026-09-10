"""ChatMixin — 桌宠对话入口（输入框 / 语音 / 发送 / 新建会话）。

由 PetWindow 多重继承。访问 self._engine / self._tts_player / self._voice_input /
self._perception / self.bubble / self.input_widget / self.input_field / self._show_bubble /
self._set_anim_seq / self._mark_user_interaction 等，均由 PetWindow 提供（鸭子类型）。

亮点：发送消息时触发 P1 全链路打断（new_message 推进代际，作废旧回复）；
      语音开始录音时触发打断（voice_start，barge-in）。

拆分自 pet.py 的对话入口区块（原 1294-1353 / 1704-1970 行），降低 PetWindow 体积。
"""
import logging
import threading
import time

import numpy as np

from config import get_transition_style

logger = logging.getLogger(__name__)


class ChatMixin:
    """对话入口：输入框切换、语音录音、消息发送、新建会话。"""

    # ── 输入框 / 语音 ──

    def _toggle_input(self):
        """切换输入框显示"""
        self._mark_user_interaction()
        if self.input_widget.isVisible():
            self.input_widget.hide()
        else:
            self.input_widget.show()
            self.input_field.setFocus()

    def _toggle_voice(self):
        """切换语音录音"""
        self._mark_user_interaction()
        if not self._voice_input:
            self._show_bubble("语音输入不可用", emotion="neutral")
            return

        if not self._voice_recording:
            # 开始录音 → 立即打断当前对话（barge-in），进入聆听
            if self._engine:
                try:
                    self._engine.interrupt(reason="voice_start")
                except Exception:
                    logger.debug("chat_mixin: 非致命异常(已静默吞掉)", exc_info=True)
            self._tts_player.stop()
            if self._voice_input.start():
                self._voice_recording = True
                self._voice_action.setText("⏹ 停止")
            else:
                self._show_bubble("录音启动失败", emotion="neutral")
        else:
            # 停止录音 -> 识别 -> 发送
            self._voice_action.setText("🎤 说话")
            self._voice_recording = False

            # 在后台线程识别，避免阻塞 UI
            def _do_asr():
                text = self._voice_input.stop()
                if text:
                    # 通过引擎发送
                    self._engine.send(text, character=self._current_char)
                    logger.info("Voice input sent: %s", text[:30])
                    # 不显示输入文字，隐藏气泡
                    self.voice_status_signal.emit("")
                    # 截停 TTS：QMediaPlayer 是 COM 组件，不能在后台线程直调 stop()
                    # （会触发 0x8001010D），经 tts_stop_signal 绕回主线程执行。
                    self.tts_stop_signal.emit()
                    # 聊天状态（_is_thinking/_pending_*/_record_topic）也经信号绕回
                    # 主线程执行，避免后台线程直写实例属性造成主线程读到中间态（B3-2）。
                    self.chat_state_signal.emit(text)
                else:
                    self.voice_status_signal.emit("没听清...")

            t = threading.Thread(target=_do_asr, daemon=True)
            t.start()

    def _on_voice_status(self, msg: str):
        """语音输入状态 - 从后台线程，通过信号转主线程"""
        self.voice_status_signal.emit(msg)

    def _toggle_voice_continuous(self):
        """切换持续监听模式（无需按键，自动检测语音并识别）"""
        self._mark_user_interaction()
        if not self._voice_input:
            self._show_bubble("语音输入不可用", emotion="neutral")
            return

        if not self._voice_continuous:
            # 开启持续监听
            if self._voice_input.start():
                self._voice_continuous = True
                self._voice_buffer_lock = getattr(self, "_voice_buffer_lock", None) or threading.Lock()
                self._continuous_asr_sem = getattr(self, "_continuous_asr_sem", None) or threading.Semaphore(2)
                self._voice_continuous_buffer = []
                self._voice_continuous_silence = 0
                self._voice_continuous_started = False
                self._voice_continuous_action.setChecked(True)
                self._voice_continuous_action.setText("🎤 监听中")
                self._show_bubble("👂 持续监听已开启", emotion="happy")
            else:
                self._show_bubble("录音启动失败", emotion="neutral")
        else:
            # 关闭持续监听
            self._voice_continuous = False
            with self._voice_buffer_lock:
                self._voice_continuous_buffer = []
            self._voice_continuous_silence = 0
            self._voice_continuous_started = False
            self._voice_input.cancel()
            self._voice_continuous_action.setChecked(False)
            self._voice_continuous_action.setText("🎤 持续监听")
            self._show_bubble("持续监听已关闭", emotion="neutral")

    def _on_voice_vad(self, chunk: np.ndarray, rms: float):
        """VAD 回调（音频线程调用）：检测语音活动，自动切分语音段。

        设计：
        - rms > 0.02 视为有人说话，累积音频
        - rms <= 0.02 视为静音，计数静音帧
        - 静音超过 40 帧（约 1.3s）视为语音段结束，自动识别发送
        - 语音段太短（< 0.5s）则丢弃
        """
        if not self._voice_continuous:
            return

        # 自适应底噪（惰性初始化）：环境在放视频/音乐时噪声抬高，阈值随之抬高，
        # 避免把背景音（如 B 站视频台词）当成真人说话触发识别。
        # 只在“未开始说话”的静音态更新底噪，说话中断音不更新，避免截断语句。
        if not self._voice_continuous_started:
            nf = getattr(self, "_vad_noise_floor", 0.006)
            nf = 0.9 * nf + 0.1 * max(rms, 1e-5)
            self._vad_noise_floor = nf
            THRESHOLD = max(0.02, nf * 3.0)
        else:
            THRESHOLD = max(0.02, getattr(self, "_vad_noise_floor", 0.006) * 3.0)
        SILENCE_FRAMES_LIMIT = 40  # 约 1.3s（512 帧/帧）

        if rms > THRESHOLD:
            # 有人说话：累积音频，重置静音计数
            with self._voice_buffer_lock:
                self._voice_continuous_buffer.append(chunk.copy())
            self._voice_continuous_silence = 0
            if not self._voice_continuous_started:
                self._voice_continuous_started = True
        else:
            if self._voice_continuous_started:
                # 静音中，但之前有语音
                self._voice_continuous_silence += 1
                if self._voice_continuous_silence >= SILENCE_FRAMES_LIMIT:
                    # 静音超时 → 语音段结束
                    with self._voice_buffer_lock:
                        audio = np.concatenate(self._voice_continuous_buffer, axis=0).flatten()
                        self._voice_continuous_buffer = []
                    self._voice_continuous_silence = 0
                    self._voice_continuous_started = False

                    # 太短丢弃
                    if len(audio) < int(self._voice_input.SAMPLE_RATE * 0.5):
                        return

                    # 后台线程 ASR 识别并发送（Semaphore 限制并发，避免线程堆积）
                    vi = self._voice_input
                    engine = self._engine
                    sem = self._continuous_asr_sem
                    if not sem.acquire(blocking=False):
                        logger.debug("Continuous ASR 已达上限，丢弃本句")
                        return

                    def _do_continuous_asr(audio_data=audio, vi_ref=vi, eng=engine, sem=sem):
                        try:
                            text = vi_ref.transcribe_audio(audio_data)
                            if text and eng:
                                # 去重：同文本 5 秒内不重复发（VAD 切两次/用户复读同一句）
                                _now = time.monotonic()
                                _prev_t = getattr(self, "_continuous_last_sent_t", 0.0)
                                _prev_text = getattr(self, "_continuous_last_sent_text", "")
                                if text == _prev_text and _now - _prev_t < 5.0:
                                    logger.debug("Continuous ASR 同句去重，丢弃: %s", text[:20])
                                    return
                                self._continuous_last_sent_t = _now
                                self._continuous_last_sent_text = text
                                eng.send(text, character=self._current_char)
                                logger.info("Continuous voice sent: %s", text[:30])
                        finally:
                            sem.release()
                    t = threading.Thread(target=_do_continuous_asr, daemon=True)
                    t.start()
            else:
                # 静音且无语音段：清空 buffer 防累积
                with self._voice_buffer_lock:
                    self._voice_continuous_buffer = []

    def _do_voice_status(self, msg: str):
        """在主线程处理语音状态"""
        if msg:
            self._show_bubble(msg, emotion="thinking")
        else:
            try:
                self.bubble.hide_bubble()
            except Exception:
                logger.debug("chat_mixin: 非致命异常(已静默吞掉)", exc_info=True)

    # ── 聊天切换 / 发送 ──

    def _toggle_chat(self):
        self._stop_walking()
        if self.input_widget.isVisible():
            self.input_widget.hide()
            self.input_field.clear()
        else:
            self.input_widget.show()
            self.input_widget.raise_()
            self.input_field.setFocus()

    def _send_message(self):
        text = self.input_field.text().strip()
        if not text or self._is_thinking:
            return

        self._mark_user_interaction()
        self.input_field.clear()
        self.input_widget.hide()

        # P2: 用户交互 -> 重置情绪状态机
        try:
            self._perception.reset_emotion()
        except Exception:
            logger.debug("chat_mixin: 非致命异常(已静默吞掉)", exc_info=True)

        # 标记对话时间（主动对话用）——用户真实回应：冷却减半奖励
        if self._perception.proactive:
            self._perception.proactive.mark_conversation(user_reply=True)

        # P2 关系：记录用户话题到陪伴记忆（隔天能接上）
        self._record_topic(text)

        # ── 用户发新消息 → 立即截停旧 TTS(P2 可中断管线)──
        self._tts_player.stop()
        # P1 全链路打断：推进代际 + 中断 LLM 层（旧消息作废，转入新对话）
        if self._engine:
            try:
                self._engine.interrupt(reason="new_message")
            except Exception as e:
                # 失败 = 旧回复不作废且旧 TTS 不被打断 → 两条声音叠着播
                logger.warning("新消息到达但引擎打断失败，可能出现重复播报: %s", e)

        # 通过对话引擎发送（异步）
        if self._engine:
            self._engine.send(text, character=self._current_char)

        # P1-2：对话事实写入点（PetWindow 注入 FactStore 时生效；防御式）
        try:
            record = getattr(self, "_record_conversation_facts", None)
            if record is not None:
                record(text)
        except Exception:
            logger.debug("chat_mixin: 非致命异常(已静默吞掉)", exc_info=True)

        # T05 P0-6：ChatPanel 同步——用户消息回显 + 思考点；专注打分（P0-5）
        try:
            chat_panel = getattr(self, "_chat_panel", None)
            if chat_panel is not None:
                chat_panel.append_user(text)
                chat_panel.set_thinking(True)
        except Exception:
            logger.debug("chat_mixin: 非致命异常(已静默吞掉)", exc_info=True)
        try:
            feed = getattr(self, "_feed_focus_score", None)
            if feed is not None:
                feed(text)
        except Exception:
            logger.debug("chat_mixin: 非致命异常(已静默吞掉)", exc_info=True)
        # P2 互动层：聊天关键词 → 小游戏/音乐/休息卡片（防御式，任一线失败不影响发送）
        try:
            dispatch = getattr(self, "_dispatch_chat_interaction", None)
            if dispatch is not None:
                dispatch(text)
        except Exception:
            logger.debug("chat_mixin: 非致命异常(已静默吞掉)", exc_info=True)

        self.bubble.set_text("⏳ 思考中...")
        self._reposition_bubble()
        self.bubble.show()
        self.bubble.raise_()
        self._is_thinking = True
        self._pending_user_msg = text
        self._pending_emotion = "neutral"
        self._pending_chat = True

        # 立即切换到思考动画（视觉反馈）
        try:
            self._set_anim_seq("working", emotion="thinking", style=get_transition_style("thinking"))
        except Exception as e:
            # 失败 = 发送后无「思考中」动作反馈，用户以为没发出去
            logger.warning("发送后思考动画切换失败: %s", e)

        # 超时保护：30 秒无回复自动恢复
        if not hasattr(self, '_think_timeout'):
            from PySide6.QtCore import QTimer as _QTimer
            self._think_timeout = _QTimer(self)  # 带 parent，随窗口一起销毁，避免泄漏
            self._think_timeout.setSingleShot(True)
            self._think_timeout.timeout.connect(self._on_think_timeout)
        # M4: Hanako 模式下默认 180 秒（长任务支持）；直连模式保持 30 秒
        think_timeout_ms = 30000
        try:
            if hasattr(self._engine, '_adapter') and self._engine._adapter:
                if getattr(self._engine._adapter, 'transport_mode', 'direct') != 'direct':
                    think_timeout_ms = int(
                        getattr(self._engine._adapter, '_reply_timeout', 180) * 1000
                    )
        except Exception:
            logger.debug("chat_mixin: 非致命异常(已静默吞掉)", exc_info=True)
        self._think_timeout.start(think_timeout_ms)

    # ── P2 关系：记录话题到陪伴记忆 ──

    def _record_topic(self, text: str):
        """把用户消息记录到 CompanionMemory（隔天能接上话题）。

        A 记忆地基：顺带写事件流（source="topic"，emotion 由 provider 自动填）。
        """
        if not text:
            return
        try:
            mem = getattr(self, "_companion_memory", None)
            if mem is not None:
                mem.record_topic(text)
                # A：事件流追加（当前前台分类作 category；topic 截断在 record_event 内）
                category = ""
                try:
                    if hasattr(self, "_foreground_watcher"):
                        category = getattr(self._foreground_watcher, "last_category", "") or ""
                except Exception:
                    category = ""
                mem.record_event(category=category, topic=text, source="topic")
        except Exception as e:
            logger.debug("P2 记录话题失败: %s", e)

    # ── 新建会话 ──

    def _create_new_session(self):
        """右键菜单：创建新 Session"""
        self._mark_user_interaction()
        if not hasattr(self, '_engine') or self._engine is None:
            self._show_bubble("引擎还没起来", emotion="thinking")
            return
        if not hasattr(self._engine, 'create_new_session'):
            self._show_bubble("当前模式不支持新建对话", emotion="neutral")
            return
        session = self._engine.create_new_session(agent_id=self._current_char)
        if session is not None:
            try:
                self.bubble.hide_bubble()
            except Exception:
                logger.debug("chat_mixin: 非致命异常(已静默吞掉)", exc_info=True)
            self._show_bubble("🔄 新对话已创建", emotion="happy")
            logger.info("新 Session 创建成功: %s", getattr(session, 'session_id', '?'))
        else:
            self._show_bubble("新对话创建失败", emotion="sad")
