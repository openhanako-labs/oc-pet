"""MIMO TTS - 小米 MiMo V2.5 TTS

使用 /v1/chat/completions 接口，不是 OpenAI /audio/speech 格式。
待合成文本放在 assistant 消息中，音频从 response 的 message.audio.data (base64) 获取。

音色：mimo_default / default_zh / default_en
模型：mimo-v2.5-tts
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
from pathlib import Path
from typing import Optional

import requests

from .base import TTSProvider

logger = logging.getLogger(__name__)

OUTPUT_DIR = Path.home() / ".hanako" / "pets" / "tts_cache"

# MIMO 预置音色
MIMO_VOICES = ["mimo_default", "default_zh", "default_en"]


class MimoTtsProvider(TTSProvider):
    """小米 MIMO TTS provider（/v1/chat/completions 格式）"""

    def __init__(self):
        self._base_url = ""
        self._api_key = ""
        self._model = "mimo-v2.5-tts"
        self._voice = "default_zh"
        self._ready = False

    def configure(self, base_url: str, api_key: str, model: str = "", voice: str = ""):
        """手动配置（从 .env 或 settings 读取）"""
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        if model:
            self._model = model
        if voice:
            self._voice = voice

    @property
    def name(self) -> str:
        return "mimo"

    @property
    def is_ready(self) -> bool:
        return self._ready

    def preload(self):
        if self._base_url and self._api_key:
            self._ready = True
            logger.info("MIMO TTS ready | url=%s | model=%s | voice=%s",
                        self._base_url[:40], self._model, self._voice)
        else:
            logger.warning("MIMO TTS config missing (need base_url + api_key)")

    def synthesize(self, text: str, character_id: str = "", instruct: str = "") -> Optional[str]:
        if not text or not text.strip() or not self._ready:
            return None

        text = text.strip()[:500]
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

        # 缓存
        cache_key = f"mimo:{self._voice}:{self._model}:{text}"
        text_hash = hashlib.md5(cache_key.encode()).hexdigest()[:12]
        output_path = OUTPUT_DIR / f"mimo_{text_hash}.wav"

        if output_path.exists():
            logger.info("TTS cache hit: %s", output_path.name)
            return str(output_path)

        # 构建请求 — MIMO 格式：文本放 assistant，风格放 user
        messages = []
        if instruct:
            messages.append({"role": "user", "content": instruct})
        messages.append({"role": "assistant", "content": text})

        payload = {
            "model": self._model,
            "messages": messages,
            "audio": {
                "format": "wav",
                "voice": self._voice,
            },
        }

        url = f"{self._base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=30)
            if resp.status_code == 200:
                data = resp.json()
                audio_data = data.get("choices", [{}])[0].get("message", {}).get("audio", {}).get("data", "")
                if audio_data:
                    audio_bytes = base64.b64decode(audio_data)
                    output_path.write_bytes(audio_bytes)
                    logger.info("MIMO TTS done: %s (%d bytes)", output_path.name, len(audio_bytes))
                    return str(output_path)
                else:
                    logger.warning("MIMO TTS: no audio.data in response")
                    return None
            else:
                logger.warning("MIMO TTS error: %d %s", resp.status_code, resp.text[:200])
                return None
        except Exception as e:
            logger.warning("MIMO TTS failed: %s", e)
            return None

    def get_speaker_info(self, character_id: str) -> dict:
        return {"voice": self._voice, "model": self._model}
