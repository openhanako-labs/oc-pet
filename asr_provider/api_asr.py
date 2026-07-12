"""API ASR - OpenAI 兼容 /audio/transcriptions 接口

配置在 data/api_config.json:
{
  "asr": {
    "base_url": "https://api.openai.com/v1",
    "api_key": "sk-...",
    "model": "whisper-1"
  }
}
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

import requests

from .base import ASRProvider
from env_config import get_asr_api_config

logger = logging.getLogger(__name__)


class ApiAsrProvider(ASRProvider):
    """API ASR - OpenAI 兼容格式"""

    def __init__(self):
        self._cfg = get_asr_api_config()
        self._ready = False

    @property
    def name(self) -> str:
        return "api"

    @property
    def is_ready(self) -> bool:
        return self._ready

    def preload(self):
        if self._cfg.get("base_url") and self._cfg.get("api_key"):
            self._ready = True
            logger.info("API ASR ready | url=%s | model=%s",
                        self._cfg["base_url"][:30], self._cfg.get("model", "?"))
        else:
            logger.warning("API ASR config missing (need base_url + api_key in data/api_config.json)")

    def transcribe(self, audio_path: str, language: str = "zh") -> Optional[str]:
        if not self._ready or not os.path.exists(audio_path):
            return None

        url = self._cfg["base_url"].rstrip("/") + "/audio/transcriptions"
        headers = {
            "Authorization": f"Bearer {self._cfg['api_key']}",
        }

        try:
            with open(audio_path, "rb") as f:
                files = {"file": ("audio.wav", f, "audio/wav")}
                data = {
                    "model": self._cfg.get("model", "whisper-1"),
                    "language": language,
                }
                resp = requests.post(url, headers=headers, files=files, data=data, timeout=30)

            if resp.status_code == 200:
                text = resp.json().get("text", "").strip()
                logger.info("API ASR result: %s", text[:50])
                return text if text else None
            else:
                logger.warning("API ASR error: %d %s", resp.status_code, resp.text[:200])
                return None
        except Exception as e:
            logger.warning("API ASR failed: %s", e)
            return None
