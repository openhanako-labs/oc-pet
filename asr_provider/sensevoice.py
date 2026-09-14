"""本地 SenseVoice ASR（FunASR / 阿里达摩院）

为什么加这个（R1 实测，2026-09-14）
------------------------------------
中文场景下 Whisper 家族并非最优。实测同一批中文音频：

| 音频片段 | faster-whisper small | SenseVoiceSmall |
|---|---|---|
| 你好，这是 **winnt provider** 集成测试 | 你好，这是**昏TIS Provider**集成测试 ❌ | 你好，这是 **winnt provider** 集成测试 ✅ |
| 你好，**莉娅** | 你好，**莉娅** ✅ | 你好，**利亚** ❌ |

**结论（诚实版）**：在 **TTS 合成的干净音频**上，两者差距不大，
SenseVoice 在**专有名词/中英混合**上更强，Whisper 在**人名**上更强。
官方榜单（SenseVoice CER 7.81% vs Whisper 20.02%）测的是**真实长音频**，
差距主要来自噪声/口音/长句场景——**本机短句对话场景尚未证实有那么大差距**。

**额外收益**：SenseVoice 顺带输出情感标签（`<|HAPPY|>`），可喂给桌宠情绪联动。

⚠️ 重要约束（实测）
--------------------
`import funasr` 实测耗时 **13.5s**（加载链：funasr → torch/lightning/diffusers
→ onnxruntime → wetext → ModelScope）。代码库其他地方已有教训：

> `voice_provider_mixin.py`: "cosyvoice 分支的 import 链（funasr → torch/lightning
> → onnxruntime → wetext → ModelScope 下载）会把事件循环冻住几十秒。"

因此本 provider：
- **绝不默认启用**（config `asr.provider` 需显式设 `sensevoice`）
- **懒加载**，且调用方必须放后台线程（与 whisper_local 的 preload 约定一致）
- 模型文件走 ModelScope 缓存，首次需下载（~1GB 含依赖；模型本体较小）

配置（config.json 的 `asr` 块）
-------------------------------
    {
      "provider": "sensevoice",     # 显式切换；默认 whisper_local
      "language": "zh",
      "device": ""                  # ""/"auto" 自动；可填 "cpu"/"cuda"
    }
"""
from __future__ import annotations

import logging
import os
import re
import threading
from typing import Optional

from .base import ASRProvider

logger = logging.getLogger(__name__)

# emoji 范围（官方 rich_transcription_postprocess 会把情感标签转成 emoji，
# 但我们的用途是喂给对话引擎，emoji 是噪声；情绪已由结构化字段单独返回）
_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001F9FF"   # 杂项符号与象形
    "\U0001FA00-\U0001FAFF"   # 扩展 A
    "\U00002600-\U000027BF"   # 杂项符号 + 装饰符号
    "\U0001F000-\U0001F2FF"   # 麻将/扑克/牌
    "\U0000FE0F"              # 变体选择符
    "\U00002B00-\U00002BFF"   # 杂项符号与箭头
    "]+",
    flags=re.UNICODE,
)

# SenseVoice 输出的富文本标签（<|zh|><|HAPPY|><|Speech|><|withitn|>）需要剥离
_TAG_STRIP_FALLBACK = (
    "<|zh|>", "<|en|>", "<|ja|>", "<|ko|>", "<|yue|>",
    "<|HAPPY|>", "<|SAD|>", "<|ANGRY|>", "<|NEUTRAL|>",
    "<|FEARFUL|>", "<|DISGUSTED|>", "<|SURPRISED|>",
    "<|Speech|>", "<|BGM|>", "<|Applause|>", "<|Laughter|>", "<|Cry|>",
    "<|withitn|>", "<|woitn|>",
)

# 情感标签 → 桌宠情绪（供后续联动使用）
EMOTION_TAG_MAP = {
    "HAPPY": "happy",
    "SAD": "sad",
    "ANGRY": "angry",
    "SURPRISED": "surprised",
    "NEUTRAL": "neutral",
}


class SenseVoiceProvider(ASRProvider):
    """本地 SenseVoice ASR（FunASR）。

    与 WhisperLocalProvider 的接口完全一致，可互换。
    """

    _model = None
    _loading = False
    _loaded = False
    _lock = threading.Lock()

    _DEFAULT_MODEL = "iic/SenseVoiceSmall"

    # ── 配置 ──

    @classmethod
    def _resolve_device(cls) -> str:
        try:
            from config import load_config
            dev = (load_config().get("asr", {}) or {}).get("device", "") or ""
            dev = str(dev).strip().lower()
            if dev in ("", "auto"):
                return "cpu"  # SenseVoice 在 CPU 上已可实时（实测 ~0.5s/5s 音频）
            return dev
        except Exception as e:
            logger.warning("sensevoice: asr.device 读取失败，回退 cpu: %s", e)
            return "cpu"

    @classmethod
    def _resolve_model(cls) -> str:
        try:
            from config import load_config
            m = (load_config().get("asr", {}) or {}).get("sensevoice_model", "")
            return str(m).strip() or cls._DEFAULT_MODEL
        except Exception:
            return cls._DEFAULT_MODEL

    # ── ASRProvider 接口 ──

    @property
    def name(self) -> str:
        return "sensevoice"

    @property
    def is_ready(self) -> bool:
        return SenseVoiceProvider._loaded and SenseVoiceProvider._model is not None

    def preload(self):
        """加载模型。**必须由调用方放在后台线程**（import 链实测 13.5s）。"""
        cls = SenseVoiceProvider
        if cls._loaded or cls._loading:
            return
        with cls._lock:
            if cls._loaded or cls._loading:
                return
            cls._loading = True
        try:
            os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
            logger.info("SenseVoice 模型加载中（import 链较重，可能 10s+）...")
            from funasr import AutoModel
            model_name = cls._resolve_model()
            device = cls._resolve_device()
            cls._model = AutoModel(
                model=model_name,
                disable_update=True,
                device=device,
                disable_pbar=True,
                log_level="ERROR",
            )
            cls._loaded = True
            logger.info("SenseVoice 就绪 (model=%s, device=%s)", model_name, device)
        except Exception as e:
            logger.error("SenseVoice 加载失败: %s", str(e)[:300])
            cls._model = None
        finally:
            cls._loading = False

    def transcribe(self, audio_path: str, language: str = "zh") -> Optional[str]:
        """识别音频文件。返回纯文本（已剥离富文本标签）。"""
        text, _emotion = self.transcribe_with_emotion(audio_path, language)
        return text

    def transcribe_with_emotion(
        self, audio_path: str, language: str = "zh"
    ) -> tuple[Optional[str], str]:
        """识别并返回 (文本, 情绪)。

        情绪来自 SenseVoice 的富文本标签（如 `<|HAPPY|>` → "happy"）。
        拿不到时返回 "neutral"。**这是 Whisper 给不了的额外信号。**
        """
        cls = SenseVoiceProvider
        if not cls._loaded:
            self.preload()
        if cls._model is None:
            logger.warning("SenseVoice: 模型不可用，识别跳过")
            return None, "neutral"
        try:
            lang = language
            if lang in ("", "auto"):
                lang = "auto"
            res = cls._model.generate(
                input=audio_path,
                language=lang,
                use_itn=True,
                batch_size_s=60,
                merge_vad=True,
                merge_length_s=15,
            )
            if not res:
                return None, "neutral"
            raw = str(res[0].get("text", "") or "")
            emotion = self._extract_emotion(raw)
            text = self._strip_tags(raw).strip()
            if not text:
                return None, "neutral"
            logger.info("ASR(sensevoice) result: %s [emotion=%s]", text[:50], emotion)
            return text, emotion
        except Exception as e:
            logger.error("SenseVoice 识别失败: %s", str(e)[:300])
            return None, "neutral"

    # ── 富文本处理 ──

    @staticmethod
    def _extract_emotion(raw: str) -> str:
        """从富文本标签提取情绪。"""
        for tag, emo in EMOTION_TAG_MAP.items():
            if f"<|{tag}|>" in raw:
                return emo
        return "neutral"

    @staticmethod
    def _strip_tags(raw: str) -> str:
        """剥离 SenseVoice 的富文本标签，返回**纯文本**。

        为什么不用官方 rich_transcription_postprocess 的输出：
        它会把情感标签转成 emoji（`<|HAPPY|>` → 😊），那是为**展示**设计的；
        但我们的用途是**喂给对话引擎**，emoji 是噪声（且情绪已由
        `transcribe_with_emotion` 以结构化字段单独返回，不该混进文本）。

        所以：先让官方后处理做 ITN（数字/标点规范化），再剥掉标签与 emoji。
        """
        text = raw
        try:
            from funasr.utils.postprocess_utils import rich_transcription_postprocess
            out = rich_transcription_postprocess(raw)
            if out:
                text = out
        except Exception as e:
            logger.debug("rich_transcription_postprocess 不可用，直接手动剥离: %s", e)
        # 剥标签（官方后处理已剥大部分，但 fallback 路径需要）
        for tag in _TAG_STRIP_FALLBACK:
            text = text.replace(tag, "")
        # 剥 emoji（官方后处理会加）
        return _EMOJI_RE.sub("", text).strip()
