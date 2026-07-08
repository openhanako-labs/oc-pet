"""CosyVoice TTS 封装 - 从桥接守护调用

使用零样本克隆模式，根据角色 ID 选择参考音频。
speaker_refs.json 定义了每个角色的参考音频和文本。

用法:
    tts = CosyVoiceTTS()
    audio_path = tts.synthesize("你好世界", "ophelia")
    # -> "/path/to/output.wav"
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
import hashlib
from pathlib import Path

logger = logging.getLogger(__name__)

# ── 路径常量 ──

COSYVOICE_DIR = Path("W:/Games/Hanako/Work/projects/cosyvoice-tts")
CLI_PATH = COSYVOICE_DIR / "cosyvoice_cli.py"
SPEAKER_REFS = COSYVOICE_DIR / "speaker_refs.json"
OUTPUT_DIR = Path.home() / ".hanako" / "pets" / "tts_cache"

# Python 解释器（CosyVoice 需要自己的 venv 或系统 Python + torch）
PYTHON = sys.executable


class CosyVoiceTTS:
    """CosyVoice TTS 封装。

    通过子进程调用 cosyvoice_cli.py 生成语音。
    首次调用较慢（模型加载），后续调用复用模型。
    """

    def __init__(self):
        self._speaker_refs = self._load_speaker_refs()
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    def _load_speaker_refs(self) -> dict:
        """加载 speaker_refs.json"""
        try:
            if SPEAKER_REFS.exists():
                return json.loads(SPEAKER_REFS.read_text("utf-8"))
        except Exception as e:
            logger.warning("Failed to load speaker_refs: %s", e)
        return {}

    def get_speaker_info(self, character_id: str) -> dict | None:
        """获取角色的参考音频信息"""
        return self._speaker_refs.get(character_id)

    def synthesize(self, text: str, character_id: str = "ophelia",
                   instruct: str = "") -> str | None:
        """合成语音

        Args:
            text: 要合成的文本（建议 < 200 字）
            character_id: 角色 ID（对应 speaker_refs.json 的 key）
            instruct: 可选情感指令（开心/温柔/低沉等）

        Returns:
            生成的音频文件路径，失败返回 None
        """
        if not text or not text.strip():
            return None

        # 截断超长文本（CosyVoice 处理 500 字以内较好）
        text = text.strip()[:500]

        # 获取参考音频
        spk_info = self._speaker_refs.get(character_id)
        if not spk_info:
            logger.warning("No speaker ref for: %s, using default", character_id)
            # 降级：用默认 SFT 模式
            return self._synthesize_sft(text, instruct)

        ref_audio = spk_info.get("ref_audio", "")
        ref_text = spk_info.get("ref_text", "")

        if not ref_audio or not os.path.exists(ref_audio):
            logger.warning("Ref audio not found: %s", ref_audio)
            return self._synthesize_sft(text, instruct)

        # 生成输出路径（用文本 hash 避免重复生成）
        text_hash = hashlib.md5(f"{character_id}:{text}".encode()).hexdigest()[:12]
        output_path = OUTPUT_DIR / f"{character_id}_{text_hash}.wav"

        # 如果已存在，直接复用
        if output_path.exists():
            logger.info("TTS cache hit: %s", output_path.name)
            return str(output_path)

        # 调用 CLI
        cmd = [
            PYTHON,
            str(CLI_PATH),
            "--text", text,
            "--ref-audio", ref_audio,
            "--ref-text", ref_text,
            "-o", str(output_path),
        ]
        if instruct:
            cmd.extend(["--instruct", instruct])

        logger.info("TTS synthesizing: %s -> %s", text[:30], output_path.name)

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60,
                cwd=str(COSYVOICE_DIR),
            )

            if result.returncode == 0:
                # CLI 最后会输出文件路径
                lines = result.stdout.strip().split("\n")
                path = lines[-1].strip() if lines else ""
                if path and os.path.exists(path):
                    logger.info("TTS done: %s", path)
                    return path
                # fallback：检查预期输出路径
                if output_path.exists():
                    return str(output_path)
                logger.warning("TTS CLI succeeded but no output file")
                return None
            else:
                logger.warning("TTS CLI failed: %s", result.stderr[-200:] if result.stderr else "unknown")
                return None
        except subprocess.TimeoutExpired:
            logger.warning("TTS timed out (60s)")
            return None
        except Exception as e:
            logger.warning("TTS error: %s", e)
            return None

    def _synthesize_sft(self, text: str, instruct: str = "") -> str | None:
        """降级：使用 SFT 模式（无参考音频）"""
        text_hash = hashlib.md5(f"sft:{text}".encode()).hexdigest()[:12]
        output_path = OUTPUT_DIR / f"sft_{text_hash}.wav"

        if output_path.exists():
            return str(output_path)

        cmd = [
            PYTHON,
            str(CLI_PATH),
            "--text", text,
            "--spk", "中文女",
            "-o", str(output_path),
        ]
        if instruct:
            cmd.extend(["--instruct", instruct])

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60,
                cwd=str(COSYVOICE_DIR),
            )
            if result.returncode == 0 and output_path.exists():
                return str(output_path)
            return None
        except Exception:
            return None
