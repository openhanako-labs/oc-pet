"""AudioMixin — 桌宠音频事件回调（AUDIO-07 / TTS 口型）。

由 PetWindow 通过多重继承混入。方法体内访问的 self._renderer / self._set_anim_seq /
self._last_tts_emotion 等均由 PetWindow 在运行时提供（鸭子类型，无需 import pet）。
"""
import logging

logger = logging.getLogger(__name__)


class AudioMixin:
    """音频回调：TTS 口型、音乐/通知/音量/暂停恢复。"""

    # ── AUDIO-07 回调：TTS 开始 → PET-02 口型 ──
    def on_tts_start(self, emotion: str = "neutral") -> None:
        """AUDIO-07 回调：TTS 开始 → PET-02 口型。

        2026-09-14：同时负责显示**延后的气泡**——
        只在 TTS 真正开始播放这一刻显示一次，避免“文本回复完一次 +
        TTS 合成完又一次”的重复气泡。
        """
        r = getattr(self, '_renderer', None)
        # Live2D 等实时渲染器：直接驱动口型参数
        if r is not None and hasattr(r, 'set_speaking'):
            r.set_speaking(True)
        # 显示延后的气泡（只在开播时一次）
        self._flush_pending_bubble(emotion)
        # _frames 可能尚未初始化（渲染器未加载/加载失败时），None 按无口型帧处理
        frames = getattr(r, '_frames', None) or {}
        if emotion in ('happy', 'angry', 'surprised'):
            speak_seq = 'speak_open'
        else:
            speak_seq = 'speak_half'
        for seq in (speak_seq, 'speak_open', 'speak_half', 'speak_closed'):
            if seq in frames and r is not None:
                r.play_anim(seq)
                self._anim_seq = seq
                logger.debug("AUDIO-07 TTS mouth: %s (emotion=%s)", seq, emotion)
                return
        logger.debug("AUDIO-07 TTS mouth: no speak frames, skip")

    def _flush_pending_bubble(self, emotion: str = "") -> None:
        """显示延后的气泡（TTS 开播时调）。

        只对 `_do_engine_reply_inner` 暂存的文本生效；
        无暂存内容时什么也不做（不干扰其他气泡路径）。
        """
        text = getattr(self, "_pending_bubble_text", "") or ""
        if not text:
            return
        emo = emotion or getattr(self, "_pending_bubble_emotion", "neutral") or "neutral"
        self._pending_bubble_text = ""
        try:
            self._show_bubble(text, emotion=emo, priority=1)
            logger.debug("TTS 开播，显示气泡: %r", text[:30])
        except Exception:
            logger.debug("显示延后气泡失败（非致命）", exc_info=True)

    def on_tts_end(self) -> None:
        """AUDIO-07 回调：TTS 结束 → 恢复 idle"""
        r = getattr(self, '_renderer', None)
        if r is not None and hasattr(r, 'set_speaking'):
            r.set_speaking(False)
        # LIP-1：清除口型时间轴（否则下一句会沿用旧时间轴）
        if r is not None and hasattr(r, 'clear_lip_timeline'):
            try:
                r.clear_lip_timeline()
            except Exception:
                logger.debug("清口型时间轴失败", exc_info=True)
        self._set_anim_seq('idle')
        logger.debug("AUDIO-07 TTS mouth: restored idle")

    def on_music_start(self, track_name: str = "") -> None:
        """AUDIO-07 回调：音乐开始 → 强制闭嘴"""
        self._set_anim_seq('idle')  # 确保不处于说话状态
        logger.info("AUDIO-07 music start: %s (mouth closed)", track_name)

    def on_music_end(self) -> None:
        """AUDIO-07 回调：音乐结束 → 恢复正常"""
        logger.info("AUDIO-07 music end")

    def on_notification_play(self) -> None:
        """AUDIO-07 回调：提示音 → 瞬时反应"""
        # 可选：触发 extra 帧 blink/jump
        logger.debug("AUDIO-07 notification played")

    def on_volume_change(self, volume: float) -> None:
        """AUDIO-07 回调：音量变化"""
        pass

    def on_pause(self, audio_type) -> None:
        """AUDIO-07 回调：播放暂停"""
        logger.debug("AUDIO-07 pause: %s", audio_type.value if hasattr(audio_type, 'value') else audio_type)

    def on_resume(self, audio_type) -> None:
        """AUDIO-07 回调：播放恢复"""
        logger.debug("AUDIO-07 resume: %s", audio_type.value if hasattr(audio_type, 'value') else audio_type)

    # ── 旧接口兼容（直接由 TTSTtsPlayer 调用）──

    def _on_tts_start(self):
        """兼容 TTSTtsPlayer.on_start → 转发给桥接器"""
        self.on_tts_start(getattr(self, '_last_tts_emotion', 'neutral'))

    def _on_tts_end(self):
        """兼容 TTSTtsPlayer.on_end → 转发给桥接器"""
        self.on_tts_end()
