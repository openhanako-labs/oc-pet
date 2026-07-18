"""pet_audio_bridge.py — AUDIO-07 桌宠音频事件桥接器

将 Hanako 音频事件总线（AUDIO-05）的播放/停顿/音量/结束事件
暴露给桌宠，区分 TTS、音乐和提示音三种类型：

  - tts:     触发口型（speak_open / speak_half / speak_closed），
             可被情感参数驱动。
  - music:   不触发说话嘴型；可选跟随节奏摆动（idle_wobble）。
  - notification: 短促反馈（jump / blink），不持久改变口型。

与 PET-02 对接方式：
  - 通过 TTSTtsPlayer.on_start / on_end 回调驱动口型状态机。
  - 音乐播放时强制闭嘴（speak_closed），防止不合适的嘴型。
  - 提示音只触发瞬时动画（extra 帧），不进入说话状态。

依赖：
  - AUDIO-05 事件总线（window.audioEventBus）
  - PET-02 口型系统（TTSTtsPlayer + SpriteRenderer）
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Callable, Optional

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════
# 数据类型
# ═══════════════════════════════════════════

class AudioType(str, Enum):
    """音频类型枚举"""
    TTS = "tts"
    MUSIC = "music"
    NOTIFICATION = "notification"


@dataclass
class AudioEvent:
    """从 AUDIO-05 事件总线收到的标准化事件"""
    type: str                    # 'play' | 'pause' | 'volume' | 'end' | 'track-change' | 'progress'
    audio_type: AudioType        # tts / music / notification
    track_id: Optional[str] = None
    track_name: Optional[str] = None
    payload: dict = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


@dataclass
class MouthState:
    """PET-02 口型状态"""
    state: str = "speak_closed"  # speak_closed / speak_half / speak_open
    emotion: str = "neutral"     # 当前情绪，影响嘴型幅度
    last_change: float = field(default_factory=time.time)


# ═══════════════════════════════════════════
# 回调接口 — 桌宠主程序实现这些
# ═══════════════════════════════════════════

class PetAudioCallbacks:
    """桌宠音频回调接口。

    PetWindow 或主循环需要实现此接口的部分或全部方法，
    桥接器在收到对应事件时调用。
    """

    def on_tts_start(self, emotion: str = "neutral") -> None:
        """TTS 开始播放 → 触发口型（PET-02）"""
        pass

    def on_tts_end(self) -> None:
        """TTS 结束播放 → 恢复默认口型"""
        pass

    def on_music_start(self, track_name: str = "") -> None:
        """音乐开始播放 → 闭嘴 + 可选节奏摆动"""
        pass

    def on_music_end(self) -> None:
        """音乐结束播放 → 恢复正常"""
        pass

    def on_notification_play(self) -> None:
        """提示音播放 → 瞬时反应（jump / blink）"""
        pass

    def on_volume_change(self, volume: float) -> None:
        """音量变化 → 可用来调整桌宠音量显示"""
        pass

    def on_pause(self, audio_type: AudioType) -> None:
        """播放暂停 → 冻结当前表现"""
        pass

    def on_resume(self, audio_type: AudioType) -> None:
        """播放恢复 → 解冻表现"""
        pass


# ═══════════════════════════════════════════
# 音频类型检测器
# ═══════════════════════════════════════════

class AudioTypeDetector:
    """根据曲目信息自动推断音频类型。

    优先级：
      1. 显式标记（audioType 字段）
      2. 文件名/路径关键词
      3. 轨道模式（mode 字段）
      4. 默认 fallback → music
    """

    NOTIFICATION_KEYWORDS = [
        "notification", "beep", "click", "ding", "alert",
        "提示音", "通知", "叮", "滴",
    ]

    TTS_KEYWORDS = [
        "tts", "speech", "voice", "合成", "语音", "cosyvoice",
        "mimo_tts", "api_tts",
    ]

    def detect(self, event: AudioEvent) -> AudioType:
        """检测音频类型"""
        # 1. 已有显式标记
        if hasattr(event, 'audio_type') and event.audio_type:
            try:
                return AudioType(event.audio_type)
            except ValueError:
                pass

        # 2. 从 track_name 或 payload 推断
        name = (event.track_name or "").lower()
        payload = event.payload or {}
        mode = (payload.get("mode") or payload.get("type") or "").lower()

        for kw in self.NOTIFICATION_KEYWORDS:
            if kw in name or kw in mode:
                return AudioType.NOTIFICATION

        for kw in self.TTS_KEYWORDS:
            if kw in name or kw in mode:
                return AudioType.TTS

        # 3. 默认 → music
        return AudioType.MUSIC


# ═══════════════════════════════════════════
# 核心桥接器
# ═══════════════════════════════════════════

class PetAudioBridge:
    """AUDIO-07 桌宠音频事件桥接器。

    职责：
      1. 监听 AUDIO-05 事件总线的所有音频事件。
      2. 按音频类型分发到不同处理策略。
      3. 防止音乐/提示音触发不合适的说话嘴型。
      4. 与 PET-02 口型系统对接（通过 callbacks）。

    使用示例：
        bridge = PetAudioBridge(callbacks)
        bridge.connect()       # 订阅事件总线
        # ... 运行中自动分发 ...
        bridge.disconnect()    # 清理订阅
    """

    def __init__(self, callbacks: PetAudioCallbacks):
        self.callbacks = callbacks
        self._detector = AudioTypeDetector()
        self._subscriptions = []           # 取消订阅函数列表
        self._current_audio_type: Optional[AudioType] = None
        self._is_playing = False
        self._mouth_state = MouthState()
        self._music_muted_speaking = False  # 音乐播放时是否强制闭嘴
        self._last_event_time = 0.0
        self._debug = False

    # ── 生命周期 ──

    def connect(self) -> None:
        """订阅 AUDIO-05 事件总线。"""
        self._subscribe_all()
        logger.info("PetAudioBridge: connected to audio event bus")

    def disconnect(self) -> None:
        """取消所有订阅，恢复默认状态。"""
        for unsub in self._subscriptions:
            try:
                unsub()
            except Exception:
                pass
        self._subscriptions.clear()
        self._reset_state()
        logger.info("PetAudioBridge: disconnected")

    # ── 事件分发 ──

    def _on_audio_event(self, detail: dict) -> None:
        """AUDIO-05 事件总线的统一入口"""
        event_type = detail.get("type", "")
        audio_type_str = detail.get("audioType", "")
        payload = detail.get("payload", {})
        track_id = detail.get("trackId")
        track_name = detail.get("trackName")
        ts = detail.get("timestamp", 0)

        # 推断音频类型
        if audio_type_str:
            try:
                audio_type = AudioType(audio_type_str)
            except ValueError:
                audio_type = self._detector.detect(
                    AudioEvent(type=event_type, audio_type=audio_type_str,
                               track_id=track_id, track_name=track_name,
                               payload=payload)
                )
        else:
            audio_type = self._detector.detect(
                AudioEvent(type=event_type, audio_type="music",
                           track_id=track_id, track_name=track_name,
                           payload=payload)
            )

        event = AudioEvent(
            type=event_type,
            audio_type=audio_type,
            track_id=track_id,
            track_name=track_name,
            payload=payload,
            timestamp=ts / 1000.0 if ts > 1e9 else ts,  # ms → s
        )

        if self._debug:
            logger.debug("[PetAudioBridge] dispatch: %s | %s | %s",
                         event_type, audio_type.value, track_name)

        # 路由分发
        dispatcher = {
            "play": self._handle_play,
            "pause": self._handle_pause,
            "resume": self._handle_resume,
            "end": self._handle_end,
            "volume": self._handle_volume,
            "track-change": self._handle_track_change,
            "progress": self._handle_progress,
            "play-state": self._handle_play_state,
        }

        handler = dispatcher.get(event_type, self._handle_unknown)
        try:
            handler(event)
        except Exception as e:
            logger.error("[PetAudioBridge] handler error (%s): %s",
                         event_type, e)

    # ── 各事件处理策略 ──

    def _handle_play(self, event: AudioEvent) -> None:
        """播放开始"""
        self._is_playing = True
        self._current_audio_type = event.audio_type

        if event.audio_type == AudioType.TTS:
            # TTS: 触发口型
            emotion = event.payload.get("emotion", "neutral")
            self._mouth_state.state = "speak_open"
            self._mouth_state.emotion = emotion
            self._mouth_state.last_change = time.time()
            self._music_muted_speaking = False
            self.callbacks.on_tts_start(emotion)

        elif event.audio_type == AudioType.MUSIC:
            # 音乐: 强制闭嘴，防止嘴型
            self._force_mouth_closed()
            self._music_muted_speaking = True
            self.callbacks.on_music_start(event.track_name or "")

        elif event.audio_type == AudioType.NOTIFICATION:
            # 提示音: 瞬时反应
            self._force_mouth_closed()
            self.callbacks.on_notification_play()

    def _handle_pause(self, event: AudioEvent) -> None:
        """播放暂停"""
        audio_type = event.audio_type
        self.callbacks.on_pause(audio_type)

        if audio_type == AudioType.TTS:
            # TTS 暂停: 保持当前口型但停止推进
            pass
        elif audio_type == AudioType.MUSIC:
            # 音乐暂停: 保持闭嘴
            pass

    def _handle_resume(self, event: AudioEvent) -> None:
        """播放恢复"""
        audio_type = event.audio_type
        self.callbacks.on_resume(audio_type)

        if audio_type == AudioType.TTS:
            # TTS 恢复: 重新触发张嘴
            emotion = event.payload.get("emotion", self._mouth_state.emotion)
            self._mouth_state.state = "speak_open"
            self._mouth_state.last_change = time.time()
            self.callbacks.on_tts_start(emotion)

    def _handle_end(self, event: AudioEvent) -> None:
        """播放结束"""
        self._is_playing = False

        if event.audio_type == AudioType.TTS:
            self._restore_idle_mouth()
            self.callbacks.on_tts_end()

        elif event.audio_type == AudioType.MUSIC:
            self._music_muted_speaking = False
            self._restore_idle_mouth()
            self.callbacks.on_music_end()

        elif event.audio_type == AudioType.NOTIFICATION:
            # 提示音结束后不需要恢复（本来就是瞬时的）
            pass

        self._current_audio_type = None

    def _handle_volume(self, event: AudioEvent) -> None:
        """音量变化"""
        volume = event.payload.get("volume", 0.8)
        self.callbacks.on_volume_change(volume)

    def _handle_track_change(self, event: AudioEvent) -> None:
        """曲目切换（AUDIO-09 扩展）"""
        new_type = self._detector.detect(event)
        old_type = self._current_audio_type

        # 如果旧类型是 TTS 且正在播放，先恢复
        if old_type == AudioType.TTS and self._is_playing:
            self._restore_idle_mouth()
            self.callbacks.on_tts_end()

        # 如果新类型是音乐，强制闭嘴
        if new_type == AudioType.MUSIC:
            self._force_mouth_closed()
            self._music_muted_speaking = True

        self._current_audio_type = new_type

        track_info = event.payload.get("trackInfo", {})
        self.callbacks.on_music_start(track_info.get("name", event.track_name or ""))

    def _handle_progress(self, event: AudioEvent) -> None:
        """进度更新（可用于节奏跟随）"""
        if event.audio_type == AudioType.MUSIC:
            current_ms = event.payload.get("currentTime", 0)
            duration_ms = event.payload.get("duration", 0)
            is_playing = event.payload.get("isPlaying", False)

            if is_playing and duration_ms > 0:
                beat_phase = (current_ms % 1000) / 1000.0
                # 每拍（~500ms）轻微摆动
                if beat_phase < 0.15:
                    self._idle_wobble()

    def _handle_play_state(self, event: AudioEvent) -> None:
        """播放状态变更"""
        playing = event.payload.get("playing", False)
        if not playing and self._is_playing:
            # 外部认为播放停止了
            self._handle_end(AudioEvent(
                type="end",
                audio_type=self._current_audio_type or AudioType.MUSIC,
                track_id=event.payload.get("trackId"),
            ))

    def _handle_unknown(self, event: AudioEvent) -> None:
        """未知事件类型"""
        logger.debug("[PetAudioBridge] unknown event: %s", event.type)

    # ── 口型控制辅助 ──

    def _force_mouth_closed(self) -> None:
        """强制闭嘴（音乐/提示音播放时调用）"""
        if self._mouth_state.state != "speak_closed":
            self._mouth_state.state = "speak_closed"
            self._mouth_state.last_change = time.time()
            self.callbacks.on_tts_end()  # 复用 TTS end 来恢复闭嘴

    def _restore_idle_mouth(self) -> None:
        """恢复 idle 口型"""
        self._music_muted_speaking = False
        self._mouth_state.state = "speak_closed"
        self._mouth_state.emotion = "neutral"
        self._mouth_state.last_change = time.time()
        self.callbacks.on_tts_end()

    def _reset_state(self) -> None:
        """重置所有状态为默认值"""
        self._is_playing = False
        self._current_audio_type = None
        self._music_muted_speaking = False
        self._mouth_state = MouthState()

    def _idle_wobble(self) -> None:
        """音乐节奏下的轻微摆动（可选）"""
        # 留给后续实现：可以通过 callbacks 扩展
        pass

    # ── 订阅管理 ──

    def _subscribe_all(self) -> None:
        """订阅 AUDIO-05 事件总线的所有事件"""
        try:
            import builtins
            window = getattr(builtins, 'window', None)
            if window and hasattr(window, 'audioEventBus'):
                unsub = window.audioEventBus.on(None, self._on_audio_event)
                self._subscriptions.append(unsub)
            else:
                logger.info("[PetAudioBridge] no window.audioEventBus (non-browser env), "
                            "manual dispatch required")
        except Exception as e:
            logger.warning("[PetAudioBridge] subscribe failed: %s", e)

    # ── 调试 ──

    def enable_debug(self) -> None:
        self._debug = True
        logger.info("[PetAudioBridge] debug enabled")

    def disable_debug(self) -> None:
        self._debug = False

    def get_status(self) -> dict:
        """获取桥接器当前状态"""
        return {
            "is_playing": self._is_playing,
            "current_audio_type": self._current_audio_type.value if self._current_audio_type else None,
            "mouth_state": self._mouth_state.state,
            "mouth_emotion": self._mouth_state.emotion,
            "music_muted_speaking": self._music_muted_speaking,
            "subscriptions": len(self._subscriptions),
        }
