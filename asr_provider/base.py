"""ASR 抽象接口 - 统一本地 Whisper 和 API 调用"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Optional

logger = logging.getLogger(__name__)


class ASRProvider(ABC):
    """语音识别供应商抽象接口"""

    @property
    @abstractmethod
    def name(self) -> str:
        """供应商标识（如 'whisper_local', 'api'）"""
        ...

    @property
    @abstractmethod
    def is_ready(self) -> bool:
        """是否已加载就绪"""
        ...

    @abstractmethod
    def preload(self):
        """预加载模型/检查 API 连通性"""
        ...

    @abstractmethod
    def transcribe(self, audio_path: str, language: str = "zh") -> Optional[str]:
        """识别音频文件

        Args:
            audio_path: WAV 文件路径
            language: 语言代码

        Returns:
            识别文本，失败返回 None
        """
        ...
