"""本地 Whisper ASR - 从 voice_input.py 提取

懒加载 base 模型，首次调用时加载（约 1GB VRAM）。
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from .base import ASRProvider

logger = logging.getLogger(__name__)

# 确保 ffmpeg 可用。
# 2026-09-10: 原为本地 import 期探测（与 voice_input.py 逐字重复，且 setdefault
# 使先后 import 顺序决定 FFMPEG_BINARY）。现统一走 core.ffmpeg_bridge 单一入口。
try:
    from core.ffmpeg_bridge import ensure_ffmpeg
    ensure_ffmpeg()
except Exception as e:
    logger.warning("whisper_local: ffmpeg 桥接不可用: %s", e)


class WhisperLocalProvider(ASRProvider):
    """本地 Whisper ASR"""

    _model = None
    _loading = False
    _loaded = False
    _backend = "whisper"   # whisper | faster_whisper
    _MODEL_SIZE = "small"  # whisper 模型尺寸：base≈中文差, small≈可用, medium≈好（更重）
    _has_cuda_result: Optional[bool] = None  # 模块级缓存：进程内 CUDA 可用性不变

    @classmethod
    def _resolve_model_size(cls) -> str:
        """从配置读取模型尺寸（asr.model），默认 small。"""
        try:
            from config import load_config
            cfg = load_config()
            return cfg.get("asr", {}).get("model", cls._MODEL_SIZE) or cls._MODEL_SIZE
        except Exception:
            return cls._MODEL_SIZE

    @property
    def name(self) -> str:
        return "whisper_local"

    @property
    def is_ready(self) -> bool:
        return WhisperLocalProvider._loaded and WhisperLocalProvider._model is not None

    def preload(self):
        if WhisperLocalProvider._loaded:
            return
        if WhisperLocalProvider._loading:
            return
        WhisperLocalProvider._loading = True
        try:
            backend = WhisperLocalProvider._resolve_backend()
            size = WhisperLocalProvider._resolve_model_size()
            logger.info("Whisper 模型加载中... (%s, backend=%s)", size, backend)
            if backend == "faster_whisper":
                # faster-whisper: CTranslate2 引擎，同精度快 4 倍、省一半内存，
                # 中英混合识别更准；模型仍叫 small/medium/large-v3 等
                try:
                    from faster_whisper import WhisperModel
                except ImportError:
                    logger.warning(
                        "faster-whisper 未安装，回退 openai-whisper。"
                        "如想启用请手动安装: pip install faster-whisper"
                    )
                    backend = "whisper"
                else:
                    import os as _os
                    _os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
                    compute = "int8" if not WhisperLocalProvider._has_cuda() else "int8_float16"
                    try:
                        WhisperLocalProvider._model = WhisperModel(
                            size, device="auto", compute_type=compute,
                        )
                        WhisperLocalProvider._backend = "faster_whisper"
                        logger.info("faster-whisper 初始化完成 (compute=%s)", compute)
                    except Exception as _e:
                        logger.warning("faster-whisper 下载/初始化失败，回退 openai-whisper: %s", str(_e)[:240])
                        backend = "whisper"
                        WhisperLocalProvider._model = None
            if WhisperLocalProvider._model is None:
                import whisper
                WhisperLocalProvider._model = whisper.load_model(size)
                WhisperLocalProvider._backend = "whisper"
            WhisperLocalProvider._loaded = True
            logger.info("Whisper 模型就绪 (%s, backend=%s)", size, backend)
        except Exception as e:
            logger.error("Whisper 加载失败: %s", e)
            WhisperLocalProvider._model = None
        finally:
            WhisperLocalProvider._loading = False

    @staticmethod
    def _has_cuda() -> bool:
        # 结果缓存到模块级：torch import 很重（秒级），且进程内 CUDA 可用性不会变化
        if WhisperLocalProvider._has_cuda_result is None:
            try:
                import torch
                WhisperLocalProvider._has_cuda_result = torch.cuda.is_available()
            except Exception:
                WhisperLocalProvider._has_cuda_result = False
        return WhisperLocalProvider._has_cuda_result

    @classmethod
    def _resolve_backend(cls) -> str:
        """读取配置中的本地 ASR 后端（whisper / faster_whisper），默认 whisper。"""
        try:
            from config import load_config
            cfg = load_config()
            return (cfg.get("asr", {}).get("backend") or "whisper").lower()
        except Exception:
            return "whisper"

    def transcribe(self, audio_path: str, language: str = "zh") -> Optional[str]:
        """识别音频文件

        Args:
            audio_path: WAV 文件路径
            language: 语言代码（空=自动，传给 whisper 时 None 触发自动检测）
        """
        # 空/未传时支持自动语言检测（Whisper 可识别 90+ 语言，不限于中文）
        if language == "auto":
            language = None
        if not WhisperLocalProvider._model:
            # 尝试复用 voice_input 已加载的全局模型
            try:
                import voice_input
                if hasattr(voice_input, '_whisper_model') and voice_input._whisper_model is not None:
                    model = voice_input._whisper_model
                    WhisperLocalProvider._model = model
                    WhisperLocalProvider._loaded = True
                    # 同步 backend 标记：voice_input 按 config 可能加载了 faster-whisper 模型，
                    # 复用后必须用它那套 transcribe 分支（VAD/置信度），否则静默退回 openai 路径
                    if getattr(model, "_fw_backend", False):
                        WhisperLocalProvider._backend = "faster_whisper"
                    else:
                        WhisperLocalProvider._backend = "whisper"
                    logger.info("Reused voice_input global Whisper model (backend=%s)",
                                WhisperLocalProvider._backend)
            except Exception as e:
                logger.debug("Could not import voice_input: %s", e)
                pass
        logger.info("WhisperLocal.transcribe: model=%s, loaded=%s, loading=%s",
                    WhisperLocalProvider._model is not None,
                    WhisperLocalProvider._loaded,
                    WhisperLocalProvider._loading)
        if not self.is_ready:
            logger.info("WhisperLocal: not ready, calling preload()")
            self.preload()
        if not WhisperLocalProvider._model:
            logger.warning("WhisperLocal: model still None after preload")
            return None
        try:
            # initial_prompt：给模型一个中文语境提示，显著改善中文识别（
            # 之前“那天到說我媽”这类错字就是 base 模型+无语境提示的通病）。
            # 自动语言模式（language=None）下不注入，避免偏置。
            _kw = {}
            if language is not None:
                _kw["initial_prompt"] = "以下是普通话的句子，请用简体中文转写。"
            backend = WhisperLocalProvider._backend
            if backend == "faster_whisper":
                # faster-whisper: transcribe 返回 (segments生成器, info)，需拼接
                segments, info = WhisperLocalProvider._model.transcribe(
                    audio_path,
                    language=language,
                    beam_size=5,
                    vad_filter=True,  # Silero VAD：过滤音乐/环境背景音，只识别真人语音段
                    **_kw,
                )
                parts = []
                avg_lp = 0.0
                no_speech = 1.0
                _n = 0
                for s in segments:
                    parts.append(s.text.strip())
                    _n += 1
                    avg_lp += float(getattr(s, "avg_logprob", -1.0) or -1.0)
                    if _n == 1:
                        no_speech = float(getattr(s, "no_speech_prob", 1.0) or 1.0)
                text = "".join(parts).strip()
                avg_lp = avg_lp / max(_n, 1)
                try:
                    logger.info("ASR backend=faster_whisper lang=%s conf=%.2f avg_lp=%.2f no_speech=%.2f",
                                getattr(info, "language", "?"),
                                getattr(info, "language_probability", 0.0) or 0.0,
                                avg_lp, no_speech)
                except Exception:
                    logger.debug("whisper_local: 非致命异常(已静默吞掉)", exc_info=True)
            else:
                # openai-whisper: 直接返回 dict
                result = WhisperLocalProvider._model.transcribe(
                    audio_path,
                    language=language,
                    **_kw,
                )
                text = result.get("text", "").strip()

            # ── 噪声防御 ──
            # ① initial_prompt 回显：对空/静音音频，whisper 可能把提示词本身当结果输出
            #    （实测出现过“请用简体中文转写。”被当成用户说的话）。
            _PROMPT_HINT = "请用简体中文转写"
            if text and _PROMPT_HINT in text:
                logger.info("ASR 空音频误识别（initial_prompt 回显），丢弃")
                return None
            # ② 置信度：整段平均 logprob 太低（多为环境噪音——直播/B站视频声被识别）
            #    → 丢弃；faster-whisper 才有这两个分数，openai 分支跳过。
            if backend == "faster_whisper" and text:
                if avg_lp < -1.2 or no_speech > 0.9:
                    logger.info("ASR 低置信度丢弃 (avg_lp=%.2f no_speech=%.2f)", avg_lp, no_speech)
                    return None
            logger.info("ASR result: %s", text[:50])
            return text if text else None
        except Exception as e:
            logger.error("ASR failed: %s", e)
            return None
