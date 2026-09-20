"""ChatMixin — 桌宠对话入口（输入框 / 语音 / 发送 / 新建会话）。

由 PetWindow 多重继承。访问 self._engine / self._tts_player / self._voice_input /
self._perception / self.bubble / self.input_widget / self.input_field / self._show_bubble /
self._set_anim_seq / self._mark_user_interaction 等，均由 PetWindow 提供（鸭子类型）。

亮点：发送消息时触发 P1 全链路打断（new_message 推进代际，作废旧回复）；
      语音开始录音时触发打断（voice_start，barge-in）；
      持续监听模式由 `_maybe_bargein()` 在音频回调帧级判断打断（2026-09-19）。

拆分自 pet.py 的对话入口区块（原 1294-1353 / 1704-1970 行），降低 PetWindow 体积。
"""
import logging
import threading
import time

import numpy as np

from config import get_transition_style

logger = logging.getLogger(__name__)


# ── barge-in（持续监听：用户开口即停嘴）──────────────────────────
#
# 背景（2026-09-19）：持续监听模式此前**完全不会打断 TTS**——
# 按住说话有 barge-in（`_toggle_voice` 里的 interrupt + stop），
# 持续监听整条路径上零次 TTS 停止。用户插话只能等桌宠说完，
# 或手动点停止。
#
# 判据（全部满足才打断）：
#   ① 连续语音 ≥ _BARGEIN_FRAMES 帧
#   ② 距上次打断 ≥ _BARGEIN_COOLDOWN_S 秒
#   ③ 当前确实在说话（`self._renderer._speaking`）
#
# 为什么 15 帧：Silero 固定窗口 512 样本 @16kHz = 32ms（见
# core/audio_input/vad.py:56），15 × 32ms ≈ 480ms，与 livekit
# 的 `min_interruption.duration = 0.5s` 同量级。
#
# ⚠️ 为什么不用 `is_playing()`：本回调跑在**音频回调线程**，而两个
# 播放器的 is_playing() 都会调 Qt（sink.state()/playbackState()）——
# 那是 Qt 内部锁，跨线程调用会与音频线程形成锁序反转（
# ui/streaming_pcm_player.py:358 的 docstring 有完整事故记录）。
# 改用 `renderer._speaking`：纯 Python 布尔赋值，不碰 Qt。
_BARGEIN_FRAMES = 15
_BARGEIN_COOLDOWN_S = 1.0

# 配置缓存：`load_config()` 每次都读盘，而 `_maybe_bargein()` 跑在
# **音频回调线程、每帧（约 32ms）调用一次**——绝不能每帧读文件。
# barge-in 参数不需要热重载（改完重启即可），故进程内只读一次。
_bargein_cfg_cache: tuple[bool, int, float] | None = None


def _strip_comments(src: str) -> str:
    """剔掉 Python 源码里的注释与 docstring，只留可执行部分。

    用于“代码里不得出现 X”这类断言——否则注释里提到 X 会误报。
    实现用 tokenize（不是正则），能正确处理字符串内的 # 与引号。

    注意：输出是**去空白拼接**的 token 序列（如 `self.tts_stop_signal.emit()`
    会变成 `self . tts_stop_signal . emit ( )`）。调用方比对时需先去空白。
    """
    import io
    import tokenize

    out: list[str] = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            # 丢掉注释与字符串字面量（docstring 也是 STRING）
            if tok.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            out.append(tok.string)
    except Exception:
        # tokenize 失败（极罕见）→ 退回原文，宁可误报也不静默放过
        return src
    return " ".join(out)


def _bargein_config() -> tuple[bool, int, float]:
    """读 barge-in 配置（asr.bargein.*），缺省用保守默认值。

    **带进程级缓存**：本函数在音频回调线程每帧调用，不能每次读盘。
    配置改动需重启生效。

    Returns:
        (enabled, frames, cooldown_s)。配置缺失/异常时回退默认（开启）。
    """
    global _bargein_cfg_cache
    if _bargein_cfg_cache is not None:
        return _bargein_cfg_cache
    try:
        from config import load_config
        cfg = ((load_config().get("asr", {}) or {}).get("bargein", {}) or {})
        enabled = bool(cfg.get("enabled", True))
        frames = int(cfg.get("frames", _BARGEIN_FRAMES) or _BARGEIN_FRAMES)
        cooldown = float(cfg.get("cooldown_seconds", _BARGEIN_COOLDOWN_S) or _BARGEIN_COOLDOWN_S)
        # 下限保护：帧数太少会误打断（视频人声），冷却为负会连发
        if frames < 3:
            frames = 3
        if cooldown < 0.0:
            cooldown = 0.0
        _bargein_cfg_cache = (enabled, frames, cooldown)
    except Exception:
        _bargein_cfg_cache = (True, _BARGEIN_FRAMES, _BARGEIN_COOLDOWN_S)
    return _bargein_cfg_cache


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
                # barge-in 状态：帧计数 + 上次打断时刻（monotonic）
                self._bargein_frames = 0
                self._bargein_last_emit = 0.0
                # ASR-1：懒建 VAD 并重置（首次开启时会加载 Silero ONNX）
                v = self._get_vad()
                if v is not None and hasattr(v, "reset"):
                    try:
                        v.reset()
                    except Exception as e:
                        logger.debug("VAD 初始 reset 失败: %s", e)
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
            self._bargein_frames = 0
            self._voice_input.cancel()
            self._voice_continuous_action.setChecked(False)
            self._voice_continuous_action.setText("🎤 持续监听")
            self._show_bubble("持续监听已关闭", emotion="neutral")

    def _get_vad(self):
        """懒初始化 VAD（ASR-1 2026-09-14）。

        优先 Silero（onnxruntime 直跑，能区分人声与视频声），
        不可用时自动回退能量 VAD（行为与改动前一致）。

        配置：`asr.vad_backend` = auto（默认）/ silero / energy
        """
        v = getattr(self, "_vad", None)
        if v is not None:
            return v
        try:
            from core.audio_input.vad import create_vad
            backend = "auto"
            try:
                backend = (self.config.get("asr", {}) or {}).get("vad_backend", "auto")
            except Exception:
                backend = "auto"
            v = create_vad(backend)
        except Exception as e:
            logger.warning("VAD 创建失败，回退能量判据: %s", e)
            v = None
        self._vad = v
        return v

    def _on_voice_vad(self, chunk: np.ndarray, rms: float):
        """VAD 回调（音频线程调用）：检测语音活动，自动切分语音段。

        判据（ASR-1 2026-09-14 升级）：
        - 优先用 **Silero VAD**（onnxruntime，区分人声与视频/音乐声）
        - 不可用时回退 **RMS 能量阈值**（与改动前行为一致）

        分段逻辑不变：
        - 语音中累积音频
        - 静音超 40 帧（约 1.3s）视为句尾，自动识别发送
        - 语音段 < 0.5s 丢弃
        """
        if not self._voice_continuous:
            return

        # ── 判据：Silero VAD 优先，能量阈值回退 ──
        vad = self._get_vad()
        is_speech = False
        if vad is not None:
            try:
                if getattr(vad, "backend", "") == "silero":
                    is_speech = vad.is_speech(chunk)
                else:
                    # 能量 VAD 需要调用方算好的 rms（保持旧逻辑）
                    if not self._voice_continuous_started:
                        nf = getattr(self, "_vad_noise_floor", 0.006)
                        nf = 0.9 * nf + 0.1 * max(rms, 1e-5)
                        self._vad_noise_floor = nf
                    is_speech = vad.is_speech(chunk, rms)
            except Exception as e:
                logger.debug("VAD 推理异常，本帧按能量回退: %s", e)
                is_speech = rms > max(0.02, getattr(self, "_vad_noise_floor", 0.006) * 3.0)
        else:
            # 无 VAD 对象：完整回退旧逻辑
            if not self._voice_continuous_started:
                nf = getattr(self, "_vad_noise_floor", 0.006)
                nf = 0.9 * nf + 0.1 * max(rms, 1e-5)
                self._vad_noise_floor = nf
            is_speech = rms > max(0.02, getattr(self, "_vad_noise_floor", 0.006) * 3.0)

        SILENCE_FRAMES_LIMIT = 40  # 约 1.3s（512 帧/帧）

        if is_speech:
            # 有人说话：累积音频，重置静音计数
            with self._voice_buffer_lock:
                self._voice_continuous_buffer.append(chunk.copy())
            self._voice_continuous_silence = 0
            if not self._voice_continuous_started:
                self._voice_continuous_started = True

            # ── barge-in：用户开口到一定时长就停掉 TTS ──
            self._maybe_bargein()
        else:
            # ── 静音：任何静音帧都重置 barge-in 计数 ──
            # 必须放在这个 else 的**开头**：判据要求“连续”语音，
            # 用户中途换气（短于 SILENCE_FRAMES_LIMIT 的停顿）也算断。
            # 若只在“无语音段”分支重置，短停顿会保留计数，
            # 两次断续的语音会被当成一次连续语音。
            self._bargein_frames = 0

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
                    # ASR-1：段结束重置 VAD 内部状态（context/触发标志），
                    # 否则下一句会继承上一句的尾部 context 与滞回状态。
                    if vad is not None and hasattr(vad, "reset"):
                        try:
                            vad.reset()
                        except Exception as e:
                            logger.debug("VAD reset 失败: %s", e)

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
                                # 与按住说话模式对齐（_do_asr 里的对称位置）：
                                # 文本即将发送，先停掉正在播的 TTS。
                                # 即使 barge-in 未触发（用户说得短、没到 15 帧），
                                # 这里也必须停——否则指令已发出、桌宠还在说上一段，
                                # 两个声音叠着播（与 chat_mixin 里 new_message 路径
                                # 警告的情形同源）。
                                # QMediaPlayer 是 COM 组件，后台线程只能经信号停。
                                try:
                                    self.tts_stop_signal.emit()
                                except Exception as e:
                                    logger.debug("持续监听 ASR 后停 TTS 失败: %s", e)
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

    def _maybe_bargein(self) -> None:
        """barge-in 判据：连续语音够长 + 冷却已过 + 确实在说话 → 停 TTS。

        在**音频回调线程**调用，所以：
        - 不能直接碰 `_tts_player` / `_stream_player`（Qt/COM，见
          pet.py 的 tts_stop_signal 注释：跨线程调 stop 会触发 0x8001010D）
        - 只能 emit 信号绕回主线程
        - 判定“是否在说话”也不得调 `is_playing()`（同样碰 Qt），
          改用 `renderer._speaking`（纯 Python 赋值）

        失败闭合：任何异常只记 debug，绝不影响语音识别主流程。
        """
        try:
            enabled, frames_need, cooldown = _bargein_config()
            if not enabled:
                return

            # 只在“桌宠正在说话”时计数与打断：
            # - 没说话就不必停
            # - 计数也归零，避免“用户早在桌宠开口前就在说”被算成连续语音
            r = getattr(self, "_renderer", None)
            if r is None or not getattr(r, "_speaking", False):
                self._bargein_frames = 0
                return

            self._bargein_frames = getattr(self, "_bargein_frames", 0) + 1
            if self._bargein_frames < frames_need:
                return

            now = time.monotonic()
            if now - getattr(self, "_bargein_last_emit", 0.0) < cooldown:
                return

            self._bargein_last_emit = now
            self._bargein_frames = 0

            # 停 TTS：必须经信号回主线程（QMediaPlayer 是 COM 组件）
            try:
                self.tts_stop_signal.emit()
            except Exception as e:
                logger.debug("barge-in: tts_stop_signal 发送失败: %s", e)

            # 作废旧回复（推进代际 + 中断 LLM 层）
            eng = getattr(self, "_engine", None)
            if eng is not None:
                try:
                    eng.interrupt(reason="voice_bargein")
                except Exception as e:
                    logger.debug("barge-in: 引擎打断失败: %s", e)

            logger.info("barge-in: 用户插话，已停 TTS（连续 %d 帧）", frames_need)
        except Exception as e:
            logger.debug("barge-in 判据异常（已忽略）: %s", e)

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
            # 2026-09-19：走引擎语义接口，不再碰 _adapter / _reply_timeout
            if self._engine is not None and self._engine.transport_mode() != "direct":
                think_timeout_ms = int(self._engine.reply_timeout_sec() * 1000)
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
