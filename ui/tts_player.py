"""TTS 音频播放器 - 用于桌宠播放 Agent 合成的语音。

用法:
    player = TTSTtsPlayer()
    player.play("C:/path/to/audio.wav")
    player.stop()

分句预合成（tts.stream_mode="sentence"）用 ``enqueue()``：
逐句合成后入队，前一句播完自动接下一句（中间不触发 on_end，
口型/说话状态跨句连续）。
"""
from __future__ import annotations

import logging
import math
import os
from collections import deque

from tts_provider.word_timings import level_at as word_level_at, load_words

logger = logging.getLogger(__name__)


# ── 文件式 TTS 的口型包络（无 PCM 可测时的近似）──
#
# QMediaPlayer 只给我们播放位置，不给 PCM——无法算真实 RMS。
# 用品率约等于语速的正弦包络替代：至少让嘴**跟着声音的起止**开合，
# 而不是自由空转（自由振荡在暂停/结束时也照动，看着假）。
#
# 峰值取 0.27 是配合 avatar/live2d_renderer._update_mouth 的振幅分支
# （val = 0.08 + 0.92*sqrt(env)*1.6）：level∈[0,0.27] 映射为可见开合
# 约 0.15~0.85，正好是"明显在说话但不夸张"的范围。
_MOUTH_ENVELOPE_HZ = 5.0
_MOUTH_ENVELOPE_MAX = 0.27


def _envelope_level(seconds: float) -> float:
    """播放位置（秒）→ 口型电平（0~_MOUTH_ENVELOPE_MAX）。纯函数，便于测试。"""
    try:
        t = float(seconds)
    except (TypeError, ValueError):
        return 0.0
    if t < 0:
        return 0.0
    return _MOUTH_ENVELOPE_MAX * (0.5 + 0.5 * math.sin(2.0 * math.pi * _MOUTH_ENVELOPE_HZ * t))


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
        # 分句预合成的排队（enqueue）：当前句播完自动接下一句
        self._queue: deque = deque()
        # 词级口型：当前音频路径 + 词区间缓存（同一文件不重复读盘）
        self._current_audio_path = None
        self._words_cache_for = None
        self._words_cache = None
        # 事件回调（桌宠主程序连接这些来驱动口型/状态）
        self.on_start: callable = lambda: None       # 开始播放
        self.on_end: callable = lambda: None         # 播放结束
        self.on_error: callable = lambda msg: None   # 播放错误

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
        """播放单个音频文件（清空排队中的后续音频）。"""
        self._queue.clear()
        self._play_file(audio_path, notify_start=True)

    def enqueue(self, audio_path: str):
        """把音频追加到播放队列；当前空闲则立即播。

        分句预合成专用：逐句合成 → enqueue，前一句播完自动接下一句，
        续播时不重触发 on_start（口型/说话状态跨句连续），
        全部播完才触发一次 on_end。
        """
        if not self._enabled:
            logger.debug("TTS disabled, skipping enqueue: %s", audio_path)
            return
        if self._player is not None or self._queue:
            self._queue.append(audio_path)
            return
        self._play_file(audio_path, notify_start=True)

    def _play_file(self, audio_path: str, notify_start: bool):
        """（内部）启动一次文件播放；notify_start=False 用于分句续播。"""
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

        self._drop_player()
        try:
            self._player = QMediaPlayer()
            self._audio_output = QAudioOutput()
            self._audio_output.setVolume(self._volume)
            self._player.setAudioOutput(self._audio_output)

            file_url = QUrl.fromLocalFile(os.path.abspath(audio_path))
            self._player.setSource(file_url)

            # 词级口型：记下当前音频路径，current_level() 据此查侧车
            self._current_audio_path = os.path.abspath(audio_path)

            self._player.mediaStatusChanged.connect(self._on_status)

            self._player.play()
            if notify_start:
                self.on_start()
            logger.info("Playing TTS: %s", audio_path)
        except Exception as e:
            logger.warning("Failed to play audio: %s", e)
            self.on_error(str(e))

    def _advance_queue(self) -> bool:
        """当前文件播完：有排队则接下一句（不触发 on_end），否则收尾。

        返回 True 表示还有后续（已续播），False 表示整串播完。
        """
        if self._queue:
            nxt = self._queue.popleft()
            self._play_file(nxt, notify_start=False)
            return True
        self.stop()
        self.on_end()
        return False

    def _drop_player(self):
        """释放 QMediaPlayer/QAudioOutput（不清队列）。"""
        if self._player:
            try:
                self._player.stop()
                self._player.deleteLater()
            except Exception:
                logger.debug("tts_player: 非致命异常(已静默吞掉)", exc_info=True)
        if self._audio_output:
            try:
                self._audio_output.deleteLater()
            except Exception:
                logger.debug("tts_player: 非致命异常(已静默吞掉)", exc_info=True)
        self._player = None
        self._audio_output = None

    def stop(self):
        """停止当前播放、清空队列并释放资源"""
        self._queue.clear()
        self._drop_player()

    def position_seconds(self):
        """当前播放位置（秒）。无播放器/查询失败时返回 None。"""
        if not self._player:
            return None
        try:
            pos = self._player.position()
        except Exception:
            logger.debug("tts_player: position 查询失败", exc_info=True)
            return None
        try:
            p = float(pos)
        except (TypeError, ValueError):
            return None
        if p < 0:
            return None
        return p / 1000.0

    def current_level(self):
        """口型电平估算（0~1）；未播放时返回 None。

        文件式 TTS（Edge/CosyVoice/MiMo/API）走 QMediaPlayer，拿不到 PCM，
        无法算真实 RMS。三级策略（2026-09-16）：

        1. **时间轴**：若合成时落了 ``<音频>.words.json``（Edge 词边界）
           或 ``<音频>.segments.json``（能量分段，任何 provider 都能生成），
           按当前播放位置落在哪个区间驱动口型——有声开合、静音闭嘴。
        2. **位置包络**：无侧车时回落正弦包络（至少跟着音频起止开合）。

        未播放返回 None（而非 0.0）：0.0 会被渲染器当成"真实静音"，
        嘴会锁死在几乎闭合处；None 才让渲染器正确回退/闭嘴。
        """
        if not self.is_playing():
            return None
        pos = self.position_seconds()
        if pos is None:
            return None
        words = self._word_timings()
        if words:
            return word_level_at(words, pos)
        return _envelope_level(pos)

    def _word_timings(self):
        """当前音频的口型时间轴（``[(start_ms, end_ms), ...]``）；无则 None。

        两级来源（2026-09-16）：

        1. ``<音频>.words.json`` —— provider 原生词边界（Edge TTS）
        2. ``<音频>.segments.json`` —— 能量分段（任何 provider 都能生成，
           见 ``tts_provider/audio_timings.py``）

        两者形状相同，播放器不必区分。按路径缓存，同一文件不重复读盘。
        读失败一律 None（回落正弦包络）。
        """
        path = getattr(self, "_current_audio_path", None)
        if not path:
            return None
        if getattr(self, "_words_cache_for", None) == path:
            return getattr(self, "_words_cache", None)
        try:
            words = load_words(path)
            if not words:
                from tts_provider.audio_timings import load_segments
                words = load_segments(path)
        except Exception:
            logger.debug("口型时间轴加载失败", exc_info=True)
            words = None
        self._words_cache_for = path
        self._words_cache = words
        return words

    def _on_status(self, status):
        """媒体状态变化回调"""
        try:
            from PySide6.QtMultimedia import QMediaPlayer
        except ImportError as e:
            # 失败 = 播放状态永远无法判定 → EndOfMedia 回调永不触发 → 口型不会停
            logger.warning("tts_player: QtMultimedia 不可用，播放状态回调失效: %s", e)
            return

        if status == QMediaPlayer.EndOfMedia:
            logger.debug("TTS playback finished")
            # 分句预合成：有排队则续播下一句，全部播完才 on_end
            self._advance_queue()
        elif status == QMediaPlayer.InvalidMedia:
            logger.warning("TTS: invalid media")
            self.stop()
            self.on_error("invalid media")

    def is_playing(self) -> bool:
        """是否正在播放"""
        if not self._player:
            return False
        try:
            return self._player.playbackState() == 1
        except Exception as e:
            # 失败 = 播放状态恒为 False → 呼叫方判断错（如认为未播放而重复触发）
            logger.warning("tts_player: playbackState 查询失败: %s", e)
            return False
