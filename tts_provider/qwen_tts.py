"""Qwen3-TTS provider —— 0.6B / 1.7B 双尺寸，低延迟 TTS。

设计说明
--------
Qwen3-TTS 官方提供两条接入路径：

1. **serve 模式（默认，推荐）**：桌宠调 Qwen3-TTS 服务端的 OpenAI 兼容
   ``/v1/audio/speech`` 接口。Qwen3-TTS 服务独立进程跑，桌宠进程不加载
   3GB 模型、不占显存、不干扰 Qt 事件循环。适合多桌宠共用同一个服务。

2. **local 模式（可选）**：桌宠进程直接 import ``qwen_tts`` 库在本地推理。
   模型加载放在 ``preload()`` 里，由 ``voice_provider_mixin`` 保证在后台
   线程执行（跟 CosyVoice 的懒加载机制对齐）。仅当不想维护独立服务进程
   时使用；有 GIL 争用风险，跟 CosyVoice 子进程版的设计取舍相反。

音色来源（复用现有 voice_profile 机制）
---------------------------------------
桌宠现有的 ``tts.voices = {"miku": "ophelia"}`` 是"角色 → 音色名"映射，
保持不变。Qwen3-TTS 需要把音色名映射到参考音频路径，新增字段：

    "tts": {
      "provider": "qwen",
      "voices": { "miku": "ophelia" },
      "qwen_voice_refs": {
        "ophelia": "W:/.../references/ophelia.wav",
        "rebecca": "W:/.../references/rebecca.wav"
      }
    }

如果某个音色名不在 ``qwen_voice_refs`` 里，则回退到 Qwen3-TTS 预置音色
（vivian / ryan / aiden）。想强制用预置音色，直接给 ``voices`` 配预置名。

emotion / instruct 参数保留，语义与 CosyVoice 对齐：
- ``emotion``：情绪标签（happy / sad / calm ...），HTTP 模式通过额外字段传递
- ``instruct``：自然语言情感指令（"用温柔的声音"），HTTP 模式传 instructions 字段

依赖
----
- serve 模式：只需要 requests（桌宠已装）
- local 模式：需要 ``pip install qwen-tts`` 或对应包，见 README

⚠️ Qwen3-TTS 官方 Python API 可能随版本迭代变化；local 模式的调用签名
按最合理的接口预留，实际接入时可能需要微调 ``_synth_local``。serve 模式
走标准 OpenAI 兼容层，稳定得多。
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


def _resolve_output_dir() -> Path:
    """定位 TTS 缓存目录；HOME 不可用（极少见的环境）时兜底到临时目录。"""
    try:
        return Path.home() / ".hanako" / "pets" / "tts_cache"
    except Exception:
        logger.debug("qwen_tts: 非致命异常(已静默吞掉)", exc_info=True)
    try:
        import tempfile
        return Path(tempfile.gettempdir()) / "hanako" / "pets" / "tts_cache"
    except Exception:
        return Path(__file__).resolve().parent / ".tts_cache"


OUTPUT_DIR = _resolve_output_dir()

# Qwen3-TTS 官方预置音色（vllm-omni 文档确认）——没配参考音频时兜底
QWEN_PRESET_VOICES = frozenset({"vivian", "ryan", "aiden"})

# 模式开关：serve（默认，走 HTTP）/ local（进程内推理）
MODES = frozenset({"serve", "local"})

# 默认模型名（Qwen3-TTS 0.6B 12Hz 变体）
DEFAULT_MODEL = "qwen3-tts-12hz-0.6b"

# HTTP 请求超时（秒）——Qwen3-TTS 97ms 首包 + 合成时间，30s 足够长文本
HTTP_TIMEOUT = 30.0

# 单次合成文本上限（跟 api_tts.py / mimo_tts.py 对齐）
MAX_TEXT_LEN = 1024


def load_voice_refs(config: dict) -> dict:
    """从 tts 配置读取 Qwen3-TTS 音色参考音频映射。

    支持的配置字段（按优先级从高到低合并）：
      1. ``tts.qwen_voice_refs``     —— 专用字段（首选）
      2. ``tts.voice_refs``          —— 通用名（未来其他 provider 可复用）
      3. 环境变量 ``QWEN_TTS_VOICE_REFS_JSON`` —— 兜底（JSON 字符串）

    值支持两种形式：
      - 字符串（纯路径）：``"ophelia": "W:/.../ophelia.wav"``
      - 字典（路径 + 参考转写）：``"ophelia": {"path": "W:/.../ophelia.wav",
        "text": "参考音频转写文本"}``。``text`` 字段能显著提高克隆质量，
        对应 vllm-omni 的 ``ref_text`` 参数。

    返回 ``{voice_name: <str | dict>}`` 映射，具体格式交给调用方解析。
    """
    refs: dict = {}
    if not isinstance(config, dict):
        return refs

    def _put(name: str, value):
        """写入一条参考音频记录（接受 str 或 dict）。"""
        if isinstance(value, str) and value.strip():
            refs[name] = value.strip()
        elif isinstance(value, dict):
            path = value.get("path", "") or value.get("ref_audio", "") or value.get("file", "")
            text = value.get("text", "") or value.get("ref_text", "")
            if isinstance(path, str) and path.strip():
                refs[name] = {"path": path.strip(), "text": (text or "").strip()}

    tts_cfg = config.get("tts", {}) or {}

    # 1) tts.qwen_voice_refs
    qwen_refs = tts_cfg.get("qwen_voice_refs")
    if isinstance(qwen_refs, dict):
        for k, v in qwen_refs.items():
            _put(k, v)

    # 2) tts.voice_refs
    voice_refs = tts_cfg.get("voice_refs")
    if isinstance(voice_refs, dict):
        for k, v in voice_refs.items():
            if k not in refs:
                _put(k, v)

    # 3) 环境变量兜底
    env_json = os.environ.get("QWEN_TTS_VOICE_REFS_JSON", "").strip()
    if env_json:
        try:
            parsed = json.loads(env_json)
            if isinstance(parsed, dict):
                for k, v in parsed.items():
                    if k not in refs:
                        _put(k, v)
            else:
                logger.warning("QWEN_TTS_VOICE_REFS_JSON 不是 dict，忽略")
        except Exception as e:
            logger.warning("QWEN_TTS_VOICE_REFS_JSON 解析失败: %s", e)

    return refs


class QwenTtsProvider(TTSProvider):
    """Qwen3-TTS provider（0.6B / 1.7B）。"""

    def __init__(
        self,
        voice_refs: Optional[dict] = None,
        mode: str = "",
        default_voice: str = "",
    ):
        self._voice_refs = dict(voice_refs or {})
        # 模式优先级：参数 > 环境变量 > 自动检测
        mode_arg = (mode or os.environ.get("QWEN_TTS_MODE", "")).strip().lower()
        if mode_arg and mode_arg in MODES:
            self._mode = mode_arg
        else:
            # 自动检测：找本地模型目录，有就 local
            local_dir = r"W:\Games\Hanako\Work\projects\qwen-tts\models\Qwen3-TTS-12Hz-0.6B-Base"
            if os.path.isdir(local_dir):
                self._mode = "local"
                logger.info("Qwen TTS: 自动检测到本地模型，使用 local 模式 | %s", local_dir)
            else:
                self._mode = "serve"

        self._default_voice = (
            default_voice
            or os.environ.get("QWEN_TTS_DEFAULT_VOICE", "vivian")
        ).strip()

        self._model_name = os.environ.get("QWEN_TTS_MODEL", DEFAULT_MODEL).strip()
        self._local_model_path = os.environ.get("QWEN_TTS_LOCAL_MODEL", "").strip()

        self._base_url = ""
        self._api_key = ""
        self._local_model = None

        self._ready = False
        self._auth_error = False  # 命中 401/403 后禁用，避免反复请求刷屏

    # ── 对外接口 ─────────────────────────────────────────

    @property
    def name(self) -> str:
        return "qwen"

    @property
    def is_ready(self) -> bool:
        return self._ready and not self._auth_error

    def configure(
        self,
        base_url: str = "",
        api_key: str = "",
        model: str = "",
        voice_refs: Optional[dict] = None,
        default_voice: str = "",
        mode: str = "",
    ) -> None:
        """运行时改配置（保留但当前实现不主动走这条路径，主要通过构造参数）。"""
        if base_url:
            self._base_url = base_url.rstrip("/")
        if api_key:
            self._api_key = api_key
        if model:
            self._model_name = model
        if voice_refs is not None:
            self._voice_refs.update(voice_refs)
        if default_voice:
            self._default_voice = default_voice
        if mode and mode.lower() in MODES:
            self._mode = mode.lower()

    def preload(self) -> None:
        """预加载模型 / 检查 API 连通性。

        ⚠️ 必须在后台线程调用（local 模式下 import qwen_tts + 加载模型会耗时
        数十秒并短暂独占 GIL，参考 CosyVoice 子进程版的设计理由）。
        """
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

        if self._auth_error:
            return

        if self._mode == "serve":
            self._preload_serve()
        else:
            self._preload_local()

    def synthesize(
        self,
        text: str,
        character_id: str = "",
        instruct: str = "",
        voice: str = "",
        emotion: str = "",
    ) -> Optional[str]:
        """合成语音，返回本地音频文件路径；失败返回 None。

        Args:
            text: 要合成的文本
            character_id: 角色 ID（用于日志，实际音色由 voice 决定）
            instruct: 情感指令（如"温柔一点"）
            voice: 音色名——先查 voice_refs 映射到参考音频，不在表里就当预置音色名
            emotion: 情绪标签（happy / sad / calm ...）

        Returns:
            音频文件路径，失败返回 None
        """
        if not text or not text.strip() or not self.is_ready:
            return None

        text = text.strip()[:MAX_TEXT_LEN]

        # 解析音色：voice_refs 命中 → 参考音频路径（+ 可选转写文本）；否则 → 预置音色名兜底
        ref_entry = self._voice_refs.get(voice, "")
        if isinstance(ref_entry, dict):
            ref_audio = ref_entry.get("path", "")
            ref_text = ref_entry.get("text", "")
        else:
            ref_audio = ref_entry or ""
            ref_text = ""
        eff_preset = voice if voice in QWEN_PRESET_VOICES else self._default_voice

        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

        # 缓存键包含所有会影响输出音质的字段
        cache_key = f"qwen:{eff_preset or ref_audio}:{ref_text or ''}:{emotion or ''}:{instruct or ''}:{text}"
        text_hash = hashlib.md5(cache_key.encode()).hexdigest()[:12]
        output_path = OUTPUT_DIR / f"qwen_{text_hash}.wav"

        if output_path.exists():
            logger.info("Qwen TTS cache hit: %s", output_path.name)
            return str(output_path)

        try:
            if self._mode == "serve":
                audio_bytes = self._synth_serve(text, eff_preset, emotion, instruct, ref_audio, ref_text)
            else:
                audio_bytes = self._synth_local(text, ref_audio, eff_preset, emotion, instruct, ref_text=ref_text)
        except Exception as e:
            logger.warning("Qwen TTS 合成异常 (%s): %s", self._mode, e)
            return None

        if not audio_bytes:
            return None

        try:
            output_path.write_bytes(audio_bytes)
            logger.info(
                "Qwen TTS done [%s]: %s (%d bytes, char=%s, voice=%s)",
                self._mode, output_path.name, len(audio_bytes), character_id or "?",
                voice or eff_preset or "?",
            )
            return str(output_path)
        except Exception as e:
            logger.warning("Qwen TTS 写缓存失败: %s", e)
            return None

    def get_speaker_info(self, character_id: str) -> dict:
        return {
            "mode": self._mode,
            "model": self._model_name,
            "voice_refs": list(self._voice_refs.keys()),
            "default_voice": self._default_voice,
        }

    def cleanup(self) -> None:
        """释放资源。serve 模式无需清理，local 模式释放模型句柄。"""
        if self._mode == "local" and self._local_model is not None:
            try:
                del self._local_model
            except Exception:
                logger.debug("qwen_tts: 非致命异常(已静默吞掉)", exc_info=True)
            self._local_model = None
            self._ready = False

    # ── serve 模式实现 ───────────────────────────────────

    def _preload_serve(self) -> None:
        """serve 模式：从 .env / catalog 读 base_url 与 api_key。"""
        # 优先读 Qwen 专用环境变量；否则回退到通用 TTS_BASE_URL（跟 ApiTtsProvider 共用）
        base_url = os.environ.get("QWEN_TTS_BASE_URL", "").strip() or \
                   os.environ.get("TTS_BASE_URL", "").strip()
        api_key = os.environ.get("QWEN_TTS_API_KEY", "").strip() or \
                  os.environ.get("TTS_API_KEY", "").strip()

        # 本地 Qwen3-TTS 服务通常不需要 key，允许占位（仅在本机地址且未提供 key 时才填占位，避免覆盖外部 API 的真实 key）
        if not api_key and (base_url.startswith("http://127.0.0.1") or base_url.startswith("http://localhost")):
            api_key = "not-needed"

        if base_url:
            self._base_url = base_url.rstrip("/")
            self._api_key = api_key
            self._ready = True
            logger.info(
                "Qwen TTS [serve] ready | url=%s | model=%s",
                self._base_url[:40], self._model_name,
            )
        else:
            logger.warning(
                "Qwen TTS [serve] 缺少 base_url；请设置 QWEN_TTS_BASE_URL 或 TTS_BASE_URL，"
                "或把 QWEN_TTS_MODE 改为 local"
            )

    def _synth_serve(
        self,
        text: str,
        voice: str,
        emotion: str,
        instruct: str,
        ref_audio_path: str = "",
        ref_text: str = "",
    ) -> Optional[bytes]:
        """调用 Qwen3-TTS 服务端 OpenAI 兼容 /v1/audio/speech 接口。

        支持两种合成模式：
          - **CustomVoice（默认）**：使用服务端预置音色（vivian / ryan / aiden）
          - **Base（voice cloning）**：传入 ``ref_audio_path`` 时自动切换，
            服务端从参考音频克隆音色。对应 vllm-omni 的 ``task_type: "Base"``。

        字段名参照 vllm-omni 文档（https://docs.vllm.ai/projects/vllm-omni）：
          ``input`` / ``voice`` / ``response_format`` / ``task_type`` /
          ``instructions`` / ``ref_audio``（file:// URI 或 base64） / ``ref_text``
        """
        import requests

        base = self._base_url
        # 自动补 /v1 前缀（跟 api_tts.py 逻辑一致，兼容 base_url 配 /v1 或不配）
        if not base.endswith("/v1") and "/v1/" not in base:
            base = base + "/v1"
        url = base + "/audio/speech"

        payload: dict = {
            "model": self._model_name,
            "input": text,
            "voice": voice,
            "response_format": "wav",
        }

        # Voice cloning 模式：提供 ref_audio_path 时切换 task_type 为 Base
        if ref_audio_path:
            payload["task_type"] = "Base"
            ref_p = Path(ref_audio_path)
            if ref_p.exists():
                # vllm-omni 支持 file:// URI（Windows 路径会自动转成 file:///C:/...）
                payload["ref_audio"] = ref_p.resolve().as_uri()
            else:
                logger.warning(
                    "Qwen TTS [serve] 参考音频不存在，回退到预置音色: %s",
                    ref_audio_path,
                )
            if ref_text:
                payload["ref_text"] = ref_text

        # 情感 / instruct 控制（vllm-omni 用 instructions 字段）
        if emotion or instruct:
            parts = []
            if emotion:
                parts.append(f"情绪：{emotion}")
            if instruct:
                parts.append(instruct)
            payload["instructions"] = "。".join(parts)
            # 兼容其他服务端实现（Qwen3-TTS 官方 demo 等）可能接受的字段名
            payload["emotion"] = emotion
            payload["instruct"] = instruct

        headers = {
            "Authorization": f"Bearer {self._api_key or 'not-needed'}",
            "Content-Type": "application/json",
        }

        resp = requests.post(url, headers=headers, json=payload, timeout=HTTP_TIMEOUT)

        if resp.status_code == 200:
            return resp.content

        if resp.status_code in (401, 403):
            logger.warning(
                "Qwen TTS auth error (%d): API Key 无效或需配置 QWEN_TTS_API_KEY。"
                "本地服务可设占位值 'not-needed'，或切换为 local 模式。",
                resp.status_code,
            )
            self._auth_error = True
            self._ready = False
            return None

        logger.warning(
            "Qwen TTS [serve] error: %d %s",
            resp.status_code, resp.text[:200],
        )
        return None

    # ── local 模式实现 ───────────────────────────────────

    def _preload_local(self) -> None:
        """local 模式：进程内加载 Qwen3-TTS 模型。

        ⚠️ 这个调用会 import qwen_tts + 从磁盘/HF 拉权重 + 加载到显存，
        耗时数十秒并短暂独占 GIL。必须从后台线程调用（由
        voice_provider_mixin._maybe_reload_tts_provider 保证）。

        依赖：
          - qwen_tts 库（GitHub: QwenLM/Qwen3-TTS，ModelScope: Qwen/Qwen3-TTS）
          - transformers 4.57.3（Qwen3-TTS 官方 config 指定版本）
          - torch 2.x + CUDA

        ⚠️ 坑：Windows 上 torch 2.11 + float16 + do_sample=True 会触发 CUDA
        device-side assert。必须用 bfloat16 + do_sample=False。
        """
        # 把 Qwen3-TTS 仓库加到 sys.path（如果还没装）
        import sys
        repo_path = os.environ.get("QWEN_TTS_REPO_PATH", r"W:\Games\Hanako\Work\projects\qwen-tts\code")
        if os.path.isdir(repo_path) and repo_path not in sys.path:
            sys.path.insert(0, repo_path)

        try:
            from qwen_tts import Qwen3TTSModel
        except ImportError as e:
            logger.warning(
                "Qwen TTS [local] 加载失败: 缺少 qwen_tts 库。"
                "请先克隆仓库到 %s，或 pip install qwen3-tts（需 Python 3.13+）。"
                "详细错误: %s",
                repo_path, e,
            )
            return

        try:
            import torch
            model_path = self._local_model_path or self._model_name

            # 如果 model_path 不是本地目录，尝试自动查找本地模型目录
            if not os.path.isdir(model_path):
                # 优先查找 qwen-tts 项目目录
                project_dir = r"W:\Games\Hanako\Work\projects\qwen-tts\models"
                if os.path.isdir(project_dir):
                    for entry in os.listdir(project_dir):
                        entry_path = os.path.join(project_dir, entry)
                        if os.path.isdir(entry_path) and os.path.isfile(os.path.join(entry_path, "config.json")):
                            model_path = entry_path
                            logger.info("Qwen TTS [local] 使用项目模型: %s", model_path)
                            break

                # 兜底：查找 ModelScope 缓存
                if not os.path.isdir(model_path):
                    cache_root = os.environ.get("MODELSCOPE_CACHE", os.path.expanduser(r"~\.cache\modelscope"))
                    hub_dir = os.path.join(cache_root, "hub")
                    if os.path.isdir(hub_dir):
                        for root, dirs, files in os.walk(hub_dir):
                            if "config.json" not in files:
                                continue
                            path_lower = root.lower()
                            if "qwen3" in path_lower and "tts" in path_lower:
                                model_path = root
                                logger.info("Qwen TTS [local] 使用 ModelScope 缓存: %s", model_path)
                                break

            self._local_model = Qwen3TTSModel.from_pretrained(
                model_path,
                device_map="cuda",
                dtype=torch.bfloat16,
            )
            self._ready = True
            logger.info("Qwen TTS [local] ready | model=%s | VRAM=%.2f GB",
                        model_path, torch.cuda.memory_allocated() / 1e9 if torch.cuda.is_available() else 0)
        except Exception as e:
            logger.warning("Qwen TTS [local] 模型加载失败: %s", e)
            self._local_model = None
            self._ready = False

    def _synth_local(
        self,
        text: str,
        ref_audio: str,
        preset_voice: str,
        emotion: str,
        instruct: str,
        ref_text: str = "",
        language: str = "Chinese",
    ) -> Optional[bytes]:
        """进程内调用 Qwen3-TTS 合成音频。

        使用 Qwen3TTSModel.generate_voice_clone（Base 模型）。

        ⚠️ Windows + torch 2.11 必须用 do_sample=False，否则 CUDA assert。
        """
        if self._local_model is None:
            return None

        if not ref_audio:
            # Base 模型必须走 voice clone；没参考音频时回退失败
            logger.warning("Qwen TTS [local] Base 模型需要 ref_audio，未提供")
            return None

        try:
            wavs, sr = self._local_model.generate_voice_clone(
                text=text,
                language=language,
                ref_audio=ref_audio,
                ref_text=ref_text or None,
                x_vector_only_mode=(not bool(ref_text)),
                do_sample=False,  # ⚠️ Windows + torch 2.11 兼容性问题
            )
            if not wavs or len(wavs) == 0 or wavs[0].size == 0:
                return None
            # wavs[0] 是 np.ndarray（单声道），sr 是采样率
            return _audio_to_wav_bytes(wavs[0], sr)
        except Exception as e:
            logger.warning("Qwen TTS [local] 合成失败: %s", e)
            return None

    # ── 流式合成（local 模式）─────────────────────────────

    def supports_streaming(self) -> bool:
        """流式合成仅在 local 模式可用（serve 模式由服务端决定，暂不支持）。"""
        return self._mode == "local"

    def synth_stream(
        self,
        text: str,
        ref_audio: str,
        ref_text: str = "",
        language: str = "Chinese",
        chunk_frames: int = 25,
        first_chunk_frames: int = 8,
        left_context: int = 25,
    ):
        """流式合成：逐块 yield PCM 字节（Int16 LE 单声道）。

        实现原理：talker 是逐帧自回归的，每生成一帧就有 16 个 codec code。
        用 ``functools.wraps`` 包一层 ``talker.forward`` 截获每帧 codes，
        攒够 ``chunk_frames`` 帧就用 speech tokenizer decoder 解成音频，
        带上 ``left_context`` 帧的左侧上下文保证拼接处平滑。

        首块用 ``first_chunk_frames``（更小）换取更低的 TTFF，
        后续用 ``chunk_frames`` 降低解码开销。

        Yields:
            bytes: PCM Int16 little-endian 单声道（采样率见 ``stream_sample_rate``）

        Raises:
            RuntimeError: 模型未就绪 / 缺 ref_audio / 非 local 模式
        """
        if self._local_model is None:
            raise RuntimeError("Qwen TTS [local] 模型未就绪")
        if self._mode != "local":
            raise RuntimeError(f"Qwen TTS 流式合成仅支持 local 模式（当前 {self._mode}）")
        if not ref_audio:
            raise RuntimeError("Qwen TTS [local] 流式合成需要 ref_audio")

        import functools
        import threading
        import queue as _queue

        import torch

        model = self._local_model
        outer = model.model
        talker = outer.talker
        decoder = outer.speech_tokenizer.model.decoder
        total_upsample = decoder.total_upsample
        eos_id = talker.config.codec_eos_token_id

        # 复用已缓存的 prompt（同一 ref_audio 只算一次 speaker embedding）
        prompt = self._get_clone_prompt(ref_audio, ref_text)
        pdict = model._prompt_items_to_voice_clone_prompt(prompt)
        input_ids = model._tokenize_texts([model._build_assistant_text(text)])
        ref_ids = None
        if ref_text:
            ref_ids = [model._tokenize_texts([model._build_ref_text(ref_text)])[0]]
        gen_kwargs = model._merge_generate_kwargs(do_sample=False)

        frames_q: "_queue.Queue" = _queue.Queue()
        err_box: list = []
        orig_forward = talker.forward

        @functools.wraps(orig_forward)
        def _hooked(*args, **kwargs):
            out = orig_forward(*args, **kwargs)
            hs = getattr(out, "hidden_states", None)
            if isinstance(hs, tuple) and len(hs) == 2 and hs[1] is not None:
                frames_q.put(hs[1].detach().clone())
            return out

        def _worker():
            try:
                talker.forward = _hooked
                if hasattr(talker, "rope_deltas"):
                    talker.rope_deltas = None
                outer.generate(
                    input_ids=input_ids,
                    ref_ids=ref_ids,
                    voice_clone_prompt=pdict,
                    languages=[language],
                    **gen_kwargs,
                )
            except Exception as e:  # noqa: BLE001
                err_box.append(e)
            finally:
                talker.forward = orig_forward
                frames_q.put(None)

        th = threading.Thread(target=_worker, daemon=True, name="QwenTTSGen")
        th.start()

        all_frames: list = []
        emitted = 0

        def _decode(start: int, end: int) -> bytes:
            lo = max(0, start - left_context)
            ctx = start - lo
            codes = torch.cat(all_frames[lo:end], dim=0)      # (T, 16)
            codes = codes.transpose(0, 1).unsqueeze(0)        # (1, 16, T)
            with torch.no_grad():
                wav = decoder(codes)
            wav = wav[..., ctx * total_upsample:]
            arr = wav.squeeze().float().cpu().numpy()
            return _float_to_pcm16(arr)

        try:
            target = first_chunk_frames
            while True:
                item = frames_q.get()
                if item is None:
                    break
                if item[0, 0].item() == eos_id:
                    break
                all_frames.append(item)
                if len(all_frames) - emitted >= target:
                    yield _decode(emitted, len(all_frames))
                    emitted = len(all_frames)
                    target = chunk_frames  # 首块之后恢复正常块大小
            if emitted < len(all_frames):
                yield _decode(emitted, len(all_frames))
        finally:
            th.join(timeout=5)

        if err_box:
            raise RuntimeError(f"Qwen TTS 流式生成失败: {err_box[0]}")

    def _get_clone_prompt(self, ref_audio: str, ref_text: str):
        """带缓存的 voice clone prompt（同一参考音频只算一次 embedding）。"""
        key = (ref_audio, ref_text or "")
        cache = getattr(self, "_prompt_cache", None)
        if cache is None:
            cache = self._prompt_cache = {}
        if key not in cache:
            cache.clear()  # 只保留最近一份，避免显存堆积
            cache[key] = self._local_model.create_voice_clone_prompt(
                ref_audio=ref_audio,
                ref_text=ref_text or None,
                x_vector_only_mode=(not bool(ref_text)),
            )
        return cache[key]

    @property
    def stream_sample_rate(self) -> int:
        """流式 PCM 的采样率。"""
        return 24000

    def can_stream(self, voice: str = "") -> bool:
        """给定音色能否走流式合成（需 local 模式 + 已就绪 + 该音色有参考音频）。"""
        if self._mode != "local" or not self._ready:
            return False
        ref_audio, _ = self._resolve_ref(voice)
        return bool(ref_audio)

    def _resolve_ref(self, voice: str) -> tuple:
        """音色名 → (ref_audio, ref_text)。不在 refs 里则返回 ("", "")。"""
        entry = self._voice_refs.get(voice, "")
        if isinstance(entry, dict):
            return entry.get("path", "") or "", entry.get("text", "") or ""
        return (entry or ""), ""

    def synthesize_stream(
        self,
        text: str,
        voice: str = "",
        chunk_frames: int = 25,
        first_chunk_frames: int = 8,
    ):
        """流式合成入口：内部解析 voice → ref_audio，逐块 yield PCM 字节。

        Yields:
            bytes: PCM Int16 LE 单声道

        Raises:
            RuntimeError: 不可流式（非 local / 未就绪 / 音色无参考音频）
        """
        ref_audio, ref_text = self._resolve_ref(voice)
        if not ref_audio:
            raise RuntimeError(f"音色 {voice!r} 无参考音频，无法流式合成")
        yield from self.synth_stream(
            text,
            ref_audio=ref_audio,
            ref_text=ref_text,
            chunk_frames=chunk_frames,
            first_chunk_frames=first_chunk_frames,
        )


def _float_to_pcm16(arr) -> bytes:
    """float32 [-1,1] → Int16 LE bytes（单声道）。"""
    import numpy as np
    a = np.asarray(arr, dtype=np.float32).reshape(-1)
    return np.clip(a * 32767.0, -32768, 32767).astype(np.int16).tobytes()


def _audio_to_wav_bytes(audio, sample_rate: int = 24000) -> bytes:
    """把 numpy 音频数组转成 wav 字节。

    Args:
        audio: np.ndarray（单声道，float32 或 int16）
        sample_rate: 采样率（Qwen3-TTS 输出 24000 Hz）
    """
    if isinstance(audio, (bytes, bytearray)):
        return bytes(audio)

    try:
        import numpy as np
        import io
        import wave

        arr = np.asarray(audio)
        if arr.ndim > 1:
            arr = arr.reshape(-1)  # 单声道化

        # 归一化到 int16 PCM
        if arr.dtype == np.float32:
            arr = np.clip(arr * 32767.0, -32768, 32767).astype(np.int16)
        elif arr.dtype == np.float64:
            arr = np.clip(arr * 32767.0, -32768, 32767).astype(np.int16)
        elif arr.dtype not in (np.int16, np.int32):
            amax = float(np.max(np.abs(arr))) if arr.size else 0.0
            if amax > 0:
                arr = arr / amax
            arr = np.clip(arr * 32767.0, -32768, 32767).astype(np.int16)

        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sample_rate)
            w.writeframes(arr.tobytes())
        return buf.getvalue()
    except Exception as e:
        logger.warning("_audio_to_wav_bytes 失败: %s", e)
        return b""
