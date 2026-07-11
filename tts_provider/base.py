"""TTS 抽象接口 - 统一本地 CosyVoice 和 API 调用

实现者只需重写 synthesize() 和 preload()。
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Optional

logger = logging.getLogger(__name__)


class TTSProvider(ABC):
    """TTS 供应商抽象接口"""

    @property
    @abstractmethod
    def name(self) -> str:
        """供应商标识（如 'cosyvoice', 'api'）"""
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
    def synthesize(self, text: str, character_id: str = "", instruct: str = "") -> Optional[str]:
        """合成语音

        Args:
            text: 要合成的文本
            character_id: 角色 ID（用于音色选择）
            instruct: 情感指令（如"开心"、"温柔"）

        Returns:
            音频文件路径，失败返回 None
        """
        ...

    def get_speaker_info(self, character_id: str) -> dict:
        """获取角色音色信息（可选实现）"""
        return {}

    def cleanup(self):
        """释放资源（可选实现）"""
        pass
