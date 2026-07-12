"""CosyVoice 本地 TTS - 从 tts_bridge.py 提取

保留原有的零样本克隆 + SFT 降级 + MD5 缓存逻辑。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Optional

from .base import TTSProvider

logger = logging.getLogger(__name__)

OUTPUT_DIR = Path.home() / ".hanako" / "pets" / "tts_cache"
COSYVOICE_DIR = Path("W:/Games/Hanako/Work/projects/cosyvoice-tts")
SPEAKER_REFS = COSYVOICE_DIR / "speaker_refs.json"


class CosyVoiceProvider(TTSProvider):
    """本地 CosyVoice2 TTS"""

    def __init__(self):
        self._model = None
        self._loaded = False
        self._speaker_refs: dict = {}

    @property
    def name(self) -> str:
        return "cosyvoice"

    @property
    def is_ready(self) -> bool:
        return self._loaded and self._model is not None

    def preload(self):
        if self._loaded:
            return
        try:
            import sys
            src_dir = str(COSYVOICE_DIR / "src")
            third_party_dir = str(COSYVOICE_DIR / "src" / "third_party" / "Matcha-TTS")
            for d in [src_dir, third_party_dir]:
                if d not in sys.path:
                    sys.path.insert(0, d)
            from cosyvoice.cli.cosyvoice import CosyVoice2
            model_path = str(COSYVOICE_DIR / "models" / "CosyVoice2-0.5B")
            self._model = CosyVoice2(model_path)
            self._loaded = True

            if SPEAKER_REFS.exists():
                self._speaker_refs = json.loads(SPEAKER_REFS.read_text("utf-8"))

            logger.info("CosyVoice 模型就绪")
        except Exception as e:
            # CosyVoice 是可选依赖（需要 torch/hyperpyyaml 等），
            # 缺失时静默降级，不报 ERROR
            logger.info("CosyVoice 不可用（可选依赖未安装）: %s", e)
            self._loaded = False

    def get_speaker_info(self, character_id: str) -> dict:
        return self._speaker_refs.get(character_id, {})

    def synthesize(self, text: str, character_id: str = "", instruct: str = "") -> Optional[str]:
        if not text or not text.strip() or not self.is_ready:
            return None

        text = text.strip()[:500]
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

        text_hash = hashlib.md5(f"{character_id}:{text}".encode()).hexdigest()[:12]
        output_path = OUTPUT_DIR / f"{character_id}_{text_hash}.wav"

        if output_path.exists():
            logger.info("TTS cache hit: %s", output_path.name)
            return str(output_path)

        spk_info = self._speaker_refs.get(character_id)
        if spk_info and spk_info.get("ref_audio"):
            return self._synthesize_zero_shot(text, character_id, instruct, output_path)
        else:
            return self._synthesize_sft(text, instruct, output_path)

    def _synthesize_zero_shot(self, text, character_id, instruct, output_path):
        ref_audio = self._speaker_refs[character_id]["ref_audio"]
        ref_text = self._speaker_refs[character_id].get("ref_text", "")
        if not os.path.exists(ref_audio):
            logger.warning("Ref audio not found: %s", ref_audio)
            return self._synthesize_sft(text, instruct, output_path)

        try:
            import soundfile as sf
            result = self._model.inference_zero_shot(
                text, ref_text, ref_audio, stream=False
            )
            for item in result:
                audio = item['tts_speech']
                arr = audio.squeeze().cpu().numpy()
                sf.write(str(output_path), arr, self._model.sample_rate)
                logger.info("TTS done: %s", output_path.name)
                return str(output_path)
            logger.warning("TTS: no output from model (zero-shot returned empty)")
            return self._synthesize_sft(text, instruct, output_path)
        except Exception as e:
            logger.warning("TTS zero-shot error: %s", e)
            return self._synthesize_sft(text, instruct, output_path)

    def _synthesize_sft(self, text, instruct, output_path):
        try:
            import soundfile as sf
            # 动态获取可用 speaker
            spk_list = list(self._model.frontend.spk2info.keys())
            if not spk_list:
                logger.warning("TTS: no SFT speakers available")
                return None
            spk = spk_list[0]
            if instruct:
                result = self._model.inference_instruct(text, spk, instruct, stream=False)
            else:
                result = self._model.inference_sft(text, spk, stream=False)
            for item in result:
                audio = item['tts_speech']
                arr = audio.squeeze().cpu().numpy()
                sf.write(str(output_path), arr, self._model.sample_rate)
                logger.info("TTS done (SFT): %s", output_path.name)
                return str(output_path)
            logger.warning("TTS: no output from SFT model")
            return None
        except Exception as e:
            logger.warning("TTS SFT error: %s", e)
            return None
