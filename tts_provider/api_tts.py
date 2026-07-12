"""API TTS - OpenAI 兼容 /audio/speech 接口

配置在 .env 文件：
  TTS_BASE_URL=https://api.openai.com/v1
  TTS_API_KEY=sk-...
  TTS_MODEL=tts-1
  TTS_VOICE=alloy
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Optional

import requests

from .base import TTSProvider
from env_config import get_tts_api_config

logger = logging.getLogger(__name__)

OUTPUT_DIR = Path.home() / ".hanako" / "pets" / "tts_cache"



class ApiTtsProvider(TTSProvider):
    """API TTS - OpenAI 兼容格式"""

    def __init__(self):
        self._cfg = get_tts_api_config()
        self._ready = False

    @property
    def name(self) -> str:
        return "api"

    @property
    def is_ready(self) -> bool:
        return self._ready

    def preload(self):
        """检查配置是否完整"""
        if self._cfg.get("base_url") and self._cfg.get("api_key"):
            self._ready = True
            logger.info("API TTS ready | url=%s | model=%s | voice=%s",
                        self._cfg["base_url"][:30], self._cfg.get("model", "?"), self._cfg.get("voice", "?"))
        else:
            logger.warning("API TTS config missing (need base_url + api_key in data/api_config.json)")

    def synthesize(self, text: str, character_id: str = "", instruct: str = "") -> Optional[str]:
        if not text or not text.strip() or not self._ready:
            return None

        text = text.strip()[:500]
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

        # 缓存
        voice = self._cfg.get("voice", "alloy")
        text_hash = hashlib.md5(f"api:{voice}:{text}".encode()).hexdigest()[:12]
        output_path = OUTPUT_DIR / f"api_{text_hash}.wav"

        if output_path.exists():
            logger.info("TTS cache hit: %s", output_path.name)
            return str(output_path)

        # 调用 API
        base = self._cfg["base_url"].rstrip("/")
        # 自动补 /v1 前缀
        if not base.endswith('/v1') and '/v1/' not in base:
            base += '/v1'
        url = base + "/audio/speech"
        headers = {
            "Authorization": f"Bearer {self._cfg['api_key']}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self._cfg.get("model", "tts-1"),
            "input": text,
            "voice": voice,
            "response_format": self._cfg.get("format", "wav"),
        }

        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=30)

            if resp.status_code == 200:
                # 直接写音频内容
                output_path.write_bytes(resp.content)
                logger.info("API TTS done: %s (%d bytes)", output_path.name, len(resp.content))
                return str(output_path)
            else:
                logger.warning("API TTS error: %d %s", resp.status_code, resp.text[:200])
                return None
        except Exception as e:
            logger.warning("API TTS failed: %s", e)
            return None
