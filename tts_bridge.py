"""CosyVoice TTS 常驻服务 - bridge 进程内加载模型，不走子进程

首次调用加载模型（~20s），后续调用 2-3 秒。
随 bridge 启动而加载，随 bridge 关闭而释放。

用法:
    tts = CosyVoiceService()
    tts.load()  # 显式加载模型（也可懒加载）
    audio_path = tts.synthesize("你好", "ophelia")
"""
from __future__ import annotations

import json
import logging
import os
import sys
import hashlib
import time
from pathlib import Path

logger = logging.getLogger(__name__)

COSYVOICE_DIR = Path("W:/Games/Hanako/Work/projects/cosyvoice-tts")
OUTPUT_DIR = Path.home() / ".hanako" / "pets" / "tts_cache"


class CosyVoiceService:
    """CosyVoice 常驻 TTS 服务。

    在 bridge 进程内直接加载 CosyVoice 模型。
    首次加载 ~20s，后续合成 2-3s。
    随 bridge 启停。
    """

    def __init__(self):
        self._speaker_refs = self._load_speaker_refs()
        self._model = None  # CosyVoice 实例（懒加载）
        self._loaded = False
        self._loading = False
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    def _load_speaker_refs(self) -> dict:
        """加载 speaker_refs.json"""
        path = COSYVOICE_DIR / "speaker_refs.json"
        try:
            if path.exists():
                return json.loads(path.read_text("utf-8"))
        except Exception as e:
            logger.warning("Failed to load speaker_refs: %s", e)
        return {}

    def get_speaker_info(self, character_id: str) -> dict | None:
        return self._speaker_refs.get(character_id)

    def preload(self):
        """预加载模型（bridge 启动时调用）"""
        if self._loaded or self._loading:
            return
        self._load_model()

    def _load_model(self):
        """加载 CosyVoice 模型到内存"""
        self._loading = True
        try:
            src_dir = str(COSYVOICE_DIR / "src")
            matcha_dir = str(COSYVOICE_DIR / "src" / "third_party" / "Matcha-TTS")
            if src_dir not in sys.path:
                sys.path.insert(0, src_dir)
            if matcha_dir not in sys.path:
                sys.path.insert(0, matcha_dir)

            from cosyvoice.cli.cosyvoice import CosyVoice2
            import torch

            model_dir = str(COSYVOICE_DIR / "models" / "CosyVoice2-0.5B")
            logger.info("CosyVoice 模型加载中... (%s)", model_dir)
            t0 = time.time()
            self._model = CosyVoice2(model_dir=model_dir, load_jit=False, fp16=torch.cuda.is_available())
            elapsed = time.time() - t0
            self._loaded = True
            logger.info("CosyVoice 模型就绪 | 耗时 %.1fs | CUDA=%s", elapsed, torch.cuda.is_available())
        except Exception as e:
            logger.error("CosyVoice 加载失败: %s", e)
            self._loaded = False
        finally:
            self._loading = False

    def synthesize(self, text: str, character_id: str = "ophelia",
                   instruct: str = "") -> str | None:
        """合成语音

        Args:
            text: 要合成的文本（< 500 字）
            character_id: 角色 ID（对应 speaker_refs.json）
            instruct: 可选情感指令（开心/难过/生气等）

        Returns:
            音频文件路径，失败返回 None
        """
        if not text or not text.strip():
            return None

        text = text.strip()[:500]

        # 懒加载
        if not self._loaded:
            self._load_model()
        if not self._model:
            return None

        # 生成输出路径
        text_hash = hashlib.md5(f"{character_id}:{text}".encode()).hexdigest()[:12]
        output_path = OUTPUT_DIR / f"{character_id}_{text_hash}.wav"

        # 缓存命中
        if output_path.exists():
            logger.info("TTS cache hit: %s", output_path.name)
            return str(output_path)

        # 获取参考音频
        spk_info = self._speaker_refs.get(character_id)
        if not spk_info or not spk_info.get("ref_audio"):
            logger.warning("No speaker ref for: %s", character_id)
            return self._synthesize_sft(text, instruct)

        ref_audio = spk_info["ref_audio"]
        ref_text = spk_info.get("ref_text", "")

        if not os.path.exists(ref_audio):
            logger.warning("Ref audio not found: %s", ref_audio)
            return self._synthesize_sft(text, instruct)

        # 合成
        try:
            import soundfile as sf
            import torch

            logger.info("TTS synthesizing: %s", text[:30])
            t0 = time.time()

            logger.info("TTS zero-shot | ref=%s | text=%s", ref_audio[-30:], text[:30])
            result = self._model.inference_zero_shot(
                text, ref_text, ref_audio, stream=False
            )

            count = 0
            for item in result:
                count += 1
                audio = item['tts_speech']
                arr = audio.squeeze().cpu().numpy()
                sf.write(str(output_path), arr, self._model.sample_rate)
                elapsed = time.time() - t0
                logger.info("TTS done: %s (%.1fs) items=%d", output_path.name, elapsed, count)
                return str(output_path)

            logger.warning("TTS: no output from model (zero-shot returned empty)")
            # 降级到 SFT
            logger.info("Falling back to SFT mode")
            return self._synthesize_sft(text, instruct)
        except Exception as e:
            logger.error("TTS synthesis failed: %s", e)
            return None

    def _synthesize_sft(self, text: str, instruct: str = "") -> str | None:
        """降级：SFT 模式"""
        try:
            import soundfile as sf

            text_hash = hashlib.md5(f"sft:{text}".encode()).hexdigest()[:12]
            output_path = OUTPUT_DIR / f"sft_{text_hash}.wav"

            if output_path.exists():
                return str(output_path)

            spk_list = list(self._model.frontend.spk2info.keys())
            if not spk_list:
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
                return str(output_path)

            return None
        except Exception as e:
            logger.error("SFT fallback failed: %s", e)
            return None

    @property
    def is_ready(self) -> bool:
        return self._loaded
