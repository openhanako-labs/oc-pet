"""本地 Whisper ASR - 从 voice_input.py 提取

懒加载 base 模型，首次调用时加载（约 1GB VRAM）。
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from .base import ASRProvider

logger = logging.getLogger(__name__)

# 确保 ffmpeg 可用
try:
    import imageio_ffmpeg
    _ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    os.environ.setdefault("FFMPEG_BINARY", _ffmpeg)
    _ffmpeg_dir = os.path.dirname(_ffmpeg)
    if _ffmpeg_dir not in os.environ.get("PATH", ""):
        os.environ["PATH"] = _ffmpeg_dir + os.pathsep + os.environ.get("PATH", "")
except Exception:
    pass


class WhisperLocalProvider(ASRProvider):
    """本地 Whisper ASR"""

    _model = None
    _loading = False
    _loaded = False

    @property
    def name(self) -> str:
        return "whisper_local"

    @property
    def is_ready(self) -> bool:
        return WhisperLocalProvider._loaded and WhisperLocalProvider._model is not None

    def preload(self):
        if WhisperLocalProvider._loaded:
            return
        if WhisperLocalProvider._loading:
            return
        WhisperLocalProvider._loading = True
        try:
            import whisper
            logger.info("Whisper 模型加载中... (base)")
            WhisperLocalProvider._model = whisper.load_model("base")
            WhisperLocalProvider._loaded = True
            logger.info("Whisper 模型就绪")
        except Exception as e:
            logger.error("Whisper 加载失败: %s", e)
        finally:
            WhisperLocalProvider._loading = False

    def transcribe(self, audio_path: str, language: str = "zh") -> Optional[str]:
        if not self.is_ready:
            self.preload()
        if not WhisperLocalProvider._model:
            return None
        try:
            result = WhisperLocalProvider._model.transcribe(audio_path, language=language)
            text = result.get("text", "").strip()
            logger.info("ASR result: %s", text[:50])
            return text if text else None
        except Exception as e:
            logger.error("ASR failed: %s", e)
            return None
