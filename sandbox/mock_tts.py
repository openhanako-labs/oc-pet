"""Mock TTS 适配器 - 替代 TTSProvider，不合成任何音频。

用法：
    from sandbox.mock_tts import MockTTSProvider
    tts = MockTTSProvider()
    # 接口与 TTSProvider 一致，synthesize() 返回空字符串
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("sandbox.tts")


class MockTTSProvider:
    """模拟 TTS，接口与 TTSProvider 一致。

    synthesize() 立即返回空字符串（不生成音频文件）。
    用于在沙盒模式下跳过 TTS 合成。
    """

    def __init__(self):
        self._ready = True
        self._call_count = 0
        logger.info("MockTTS 初始化 | 即时就绪，不合成音频")

    @property
    def name(self) -> str:
        return "mock"

    @property
    def is_ready(self) -> bool:
        return self._ready

    def preload(self):
        """预加载 - mock 模式下立即就绪"""
        self._ready = True
        logger.info("MockTTS preload 完成（瞬时）")

    def synthesize(self, text: str, character_id: str = "",
                   instruct: str = "") -> Optional[str]:
        """合成语音 - 返回空字符串，跳过音频播放"""
        self._call_count += 1
        logger.info(
            "MockTTS synthesize #%d | text=%s | char=%s | instruct=%s -> (skip)",
            self._call_count, text[:30], character_id, instruct
        )
        return ""  # 空字符串 = 不播放音频

    def get_speaker_info(self, character_id: str) -> dict:
        return {"mock": True, "character": character_id}

    def cleanup(self):
        pass

    @property
    def stats(self) -> dict:
        return {"calls": self._call_count}
