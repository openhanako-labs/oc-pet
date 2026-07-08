"""TTS 音频播放器 — 用于桌宠播放 Agent 合成的语音。

用法:
    player = TTSTtsPlayer()
    player.play("C:/path/to/audio.wav")
    player.stop()
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# QMediaPlayer 需要 QApplication 实例，此处推迟导入到 play() 调用时


class TTSTtsPlayer:
    """简单的音频播放器封装，使用 QMediaPlayer。

    要求：调用前必须有 QApplication 实例在运行。
    """

    def __init__(self):
        self._player = None
        self._media = None
        self._enabled = True
        self._volume: float = 0.8

    @property
    def enabled(self) -> bool:
        return self._enabled

    def enable(self):
        self._enabled = True

    def disable(self):
        self._enabled = False
        self.stop()

    def set_volume(self, vol: float):
        """设置音量 0.0~1.0"""
        self._volume = max(0.0, min(1.0, vol))
        if self._player:
            self._player.setVolume(int(self._volume * 100))

    def play(self, audio_path: str):
        """播放音频文件。

        Args:
            audio_path: 音频文件路径（wav/mp3/ogg 等）
        """
        if not self._enabled:
            logger.debug("TTS disabled, skipping: %s", audio_path)
            return

        if not audio_path or not os.path.exists(audio_path):
            logger.warning("Audio file not found: %s", audio_path)
            return

        # 延迟导入 PySide6（确保 QApplication 已创建）
        try:
            from PySide6.QtMultimedia import QMediaPlayer
            from PySide6.QtCore import QUrl
        except ImportError as e:
            logger.warning("QMediaPlayer not available: %s", e)
            return

        # 停止当前播放
        self.stop()

        try:
            self._player = QMediaPlayer()
            file_url = QUrl.fromLocalFile(os.path.abspath(audio_path))
            self._player.setSource(file_url)
            self._player.setVolume(int(self._volume * 100))

            # 播放结束时清理
            self._player.mediaStatusChanged.connect(self._on_status)

            self._player.play()
            logger.info("Playing TTS: %s", audio_path)
        except Exception as e:
            logger.warning("Failed to play audio: %s", e)

    def stop(self):
        """停止当前播放"""
        if self._player and self._player.playbackState() != 0:  # 0 = StoppedState
            try:
                self._player.stop()
            except Exception:
                pass
        self._player = None
        self._media = None

    def _on_status(self, status):
        """媒体状态变化回调"""
        try:
            from PySide6.QtMultimedia import QMediaPlayer
        except ImportError:
            return

        if status == QMediaPlayer.EndOfMedia:
            logger.debug("TTS playback finished")
            self.stop()
        elif status == QMediaPlayer.InvalidMedia:
            logger.warning("TTS: invalid media")
            self.stop()

    def is_playing(self) -> bool:
        """是否正在播放"""
        if not self._player:
            return False
        try:
            return self._player.playbackState() == 1  # 1 = PlayingState
        except Exception:
            return False