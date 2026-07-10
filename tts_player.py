"""TTS 音频播放器 - 用于桌宠播放 Agent 合成的语音。

用法:
    player = TTSTtsPlayer()
    player.play("C:/path/to/audio.wav")
    player.stop()
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


class TTSTtsPlayer:
    """音频播放器封装，兼容 PySide6 6.11+ 新 API。

    PySide6 6.11 移除了 QMediaPlayer.setVolume()，
    改用 QAudioOutput.setVolume()。
    """

    def __init__(self):
        self._player = None
        self._audio_output = None
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
        if self._audio_output:
            self._audio_output.setVolume(self._volume)

    def play(self, audio_path: str):
        """播放音频文件。"""
        if not self._enabled:
            logger.debug("TTS disabled, skipping: %s", audio_path)
            return

        if not audio_path or not os.path.exists(audio_path):
            logger.warning("Audio file not found: %s", audio_path)
            return

        try:
            from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
            from PySide6.QtCore import QUrl
        except ImportError as e:
            logger.warning("QMediaPlayer not available: %s", e)
            return

        self.stop()

        try:
            self._player = QMediaPlayer()
            self._audio_output = QAudioOutput()
            self._audio_output.setVolume(self._volume)
            self._player.setAudioOutput(self._audio_output)

            file_url = QUrl.fromLocalFile(os.path.abspath(audio_path))
            self._player.setSource(file_url)

            self._player.mediaStatusChanged.connect(self._on_status)

            self._player.play()
            logger.info("Playing TTS: %s", audio_path)
        except Exception as e:
            logger.warning("Failed to play audio: %s", e)

    def stop(self):
        """停止当前播放并释放资源"""
        if self._player:
            try:
                self._player.stop()
                self._player.deleteLater()
            except Exception:
                pass
        if self._audio_output:
            try:
                self._audio_output.deleteLater()
            except Exception:
                pass
        self._player = None
        self._audio_output = None

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
            return self._player.playbackState() == 1
        except Exception:
            return False