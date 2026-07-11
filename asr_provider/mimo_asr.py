"""MIMO ASR - 小米 MiMo V2.5 语音识别

使用 /v1/chat/completions 接口，不是 OpenAI /audio/transcriptions 格式。
音频以 base64 data URL 放在 user 消息的 input_audio 字段中。
识别结果从 response.choices[0].message.content 获取。

模型：mimo-v2.5-asr
"""
from __future__ import annotations

import base64
import logging
import os
from pathlib import Path
from typing import Optional

import requests

from .base import ASRProvider

logger = logging.getLogger(__name__)


class MimoAsrProvider(ASRProvider):
    """小米 MIMO ASR provider（/v1/chat/completions 格式）"""

    def __init__(self):
        self._base_url = ""
        self._api_key = ""
        self._model = "mimo-v2.5-asr"
        self._language = "auto"
        self._ready = False

    def configure(self, base_url: str, api_key: str, model: str = "", language: str = ""):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        if model:
            self._model = model
        if language:
            self._language = language

    @property
    def name(self) -> str:
        return "mimo"

    @property
    def is_ready(self) -> bool:
        return self._ready

    def preload(self):
        if self._base_url and self._api_key:
            self._ready = True
            logger.info("MIMO ASR ready | url=%s | model=%s",
                        self._base_url[:40], self._model)
        else:
            logger.warning("MIMO ASR config missing (need base_url + api_key)")

    def transcribe(self, audio_path: str, language: str = "zh") -> Optional[str]:
        if not self._ready or not audio_path:
            return None

        try:
            with open(audio_path, "rb") as f:
                audio_bytes = f.read()

            # 检测 MIME 类型
            ext = Path(audio_path).suffix.lower()
            mime = "audio/wav" if ext == ".wav" else "audio/mpeg"
            audio_b64 = base64.b64encode(audio_bytes).decode("utf-8")

            lang = language if language else self._language

            payload = {
                "model": self._model,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_audio",
                                "input_audio": {
                                    "data": f"data:{mime};base64,{audio_b64}",
                                },
                            }
                        ],
                    }
                ],
                "asr_options": {"language": lang},
            }

            url = f"{self._base_url}/chat/completions"
            headers = {
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            }

            resp = requests.post(url, headers=headers, json=payload, timeout=30)
            if resp.status_code == 200:
                data = resp.json()
                text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                if text:
                    logger.info("MIMO ASR done: %s", text[:50])
                    return text.strip()
                else:
                    logger.warning("MIMO ASR: empty response")
                    return None
            else:
                logger.warning("MIMO ASR error: %d %s", resp.status_code, resp.text[:200])
                return None

        except Exception as e:
            logger.warning("MIMO ASR failed: %s", e)
            return None
