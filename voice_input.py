"""语音输入 - 麦克风录音 + Whisper ASR 转文字

按住说话模式：点击"说话"开始录音，再点击停止。
也支持静默自动停止（VAD 简化版：检测音量低于阈值 N 秒）。

用法:
    vi = VoiceInput()
    vi.start()           # 开始录音
    text = vi.stop()     # 停止录音，返回识别文本
"""
from __future__ import annotations

import logging
import os
import tempfile

# 确保 ffmpeg 可用（whisper 依赖）。
# 2026-09-10: 原为本地 import 期探测（与 asr_provider/whisper_local.py 逐字重复，
# 且 setdefault 让 import 顺序决定 PATH）。现统一走 core.ffmpeg_bridge 单一入口。
#
# 注意：此处不得使用模块级 logger —— 它定义在下方（原实现因此有个真 bug：
# 一旦 imageio_ffmpeg 导入失败，except 分支引用未定义的 logger 会抛 NameError，
# 直接导致整个模块 import 失败、语音功能静默消失）。
try:
    from core.ffmpeg_bridge import ensure_ffmpeg
    ensure_ffmpeg()
except Exception as e:  # 桥接模块自身不可用属极端情况，不阻断本模块导入
    logging.getLogger(__name__).warning("voice_input: ffmpeg 桥接不可用: %s", e)
import threading
import time
import wave

import numpy as np

logger = logging.getLogger(__name__)

# 延迟导入 whisper（加载慢）
_whisper_model = None
_whisper_loading = False


def _get_asr_model_name() -> str:
    """读取配置中的 whisper 模型尺寸（默认 small，中文识别明显好于 base）。"""
    try:
        from config import load_config
        cfg = load_config()
        return cfg.get("asr", {}).get("model", "small") or "small"
    except Exception:
        return "small"


def _asr_language() -> str:
    """读取配置中的 ASR 语言（默认 zh；设 auto 则 Whisper 自动检测多语言）。"""
    try:
        from config import load_config
        cfg = load_config()
        return cfg.get("asr", {}).get("language", "zh") or "zh"
    except Exception:
        return "zh"


def _get_whisper_model():
    """懒加载 Whisper 模型（尊重 config 的 asr.backend）。

    之前硬编码 openai-whisper，导致 whisper_local 复用后 _backend 仍是
    "whisper"——faster-whisper 的 VAD 过滤/置信度永远不生效。
    现在按 config 加载，并给模型打 _fw_backend 标记供复用方识别。
    faster-whisper 下载失败（网络/SSL）时自动回退 openai-whisper（本地 .pt），
    保证语音识别不挂。
    """
    global _whisper_model, _whisper_loading
    if _whisper_model is not None:
        return _whisper_model
    if _whisper_loading:
        return None
    # HuggingFace 国内走镜像（HuggingFace 直连 SSL/超时是常见问题）
    os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    _whisper_loading = True
    try:
        size = _get_asr_model_name()
        backend = "whisper"
        try:
            from config import load_config
            backend = (load_config().get("asr", {}).get("backend") or "whisper").lower()
        except Exception:
            logger.debug("voice_input: 非致命异常(已静默吞掉)", exc_info=True)
        logger.info("Whisper 模型加载中... (%s, backend=%s)", size, backend)
        if backend == "faster_whisper":
            try:
                from faster_whisper import WhisperModel
                import torch
                compute = "int8_float16" if torch.cuda.is_available() else "int8"
                _whisper_model = WhisperModel(size, device="auto", compute_type=compute)
                _whisper_model._fw_backend = True  # 供 whisper_local 复用方识别
                logger.info("Whisper 模型就绪 (faster-whisper, compute=%s)", compute)
            except Exception as e:
                logger.warning("faster-whisper 加载失败，回退 openai-whisper: %s", str(e)[:240])
                import whisper
                _whisper_model = whisper.load_model(size)
                logger.info("Whisper 模型就绪 (openai-whisper fallback)")
        else:
            import whisper
            _whisper_model = whisper.load_model(size)
            logger.info("Whisper 模型就绪")
    except Exception as e:
            # Whisper 是可选依赖，缺失时静默降级
            logger.info("Whisper 不可用（可选依赖未安装）: %s", e)
            _whisper_model = None
    finally:
        _whisper_loading = False
    return _whisper_model


def preload_whisper():
    """预加载 Whisper 模型"""
    t = threading.Thread(target=_get_whisper_model, daemon=True)
    t.start()


class VoiceInput:
    """麦克风录音 + Whisper ASR。

    录音流程：
    1. start() -> sounddevice 开始采集
    2. stop() -> 停止采集 -> 保存 wav -> Whisper 转写 -> 返回文本
    """

    SAMPLE_RATE = 16000
    CHANNELS = 1
    DTYPE = np.float32

    def __init__(self, asr_provider=None, device=None):
        """
        Args:
            asr_provider: ASR provider 实例
            device: 录音设备 ID（sounddevice 索引或子串），空/None 用系统默认。
                    2026-08-22 新增：解决默认设备不是麦克风导致录不到人声的问题。
        """
        self._asr = asr_provider
        self._device = device
        self._recording = False
        self._audio_data: list[np.ndarray] = []
        self._stream = None
        self._on_status: callable = lambda msg: None  # 状态回调
        # 持续监听模式
        self._silent_energy_threshold = 0.012  # RMS 能量阈值（低于此视为静音）
        self._latest_energy = 0.0  # 最近音频块的 RMS 能量
        self._vad_speech_started = False  # 持续监听下是否检测到语音开始
        self._vad_speech_audio: list = []  # 语音段缓存
        self._vad_silent_frames = 0  # 连续静音帧计数
        self._vad_callback: callable = None  # 语音段结束回调

        # 解析 device 字段（空字符串 → None → 系统默认）
        if device is None or (isinstance(device, str) and not device.strip()):
            self._device_idx = None
        else:
            # 支持两种写法：纯数字索引（"1"）或设备名子串（"麦克风 (Realtek"）
            if isinstance(device, str) and device.strip().isdigit():
                self._device_idx = int(device.strip())
            else:
                self._device_idx = device  # 交给 sounddevice 按名称/索引解析

    @property
    def is_recording(self) -> bool:
        return self._recording

    def start(self) -> bool:
        """开始录音"""
        if self._recording:
            return False

        try:
            import sounddevice as sd
        except ImportError:
            logger.info("sounddevice not available")
            self._on_status("录音模块不可用")
            return False

        self._audio_data = []
        self._recording = True
        self._on_status("正在录音... 再点一次停止")

        try:
            # 指定录音设备：空/None → 系统默认；否则用配置的设备索引/名称
            self._stream = sd.Stream(
                samplerate=self.SAMPLE_RATE,
                channels=self.CHANNELS,
                device=self._device_idx,
                dtype=self.DTYPE,
                callback=self._audio_callback,
            )
            self._stream.start()
            logger.info("Recording started")
            return True
        except Exception as e:
            logger.info("Failed to start recording: %s", e)
            self._recording = False
            self._on_status("录音启动失败: " + str(e))
            return False

    def stop(self) -> str:
        """停止录音，返回识别文本

        Returns:
            识别的文本，失败返回空字符串
        """
        if not self._recording:
            return ""

        self._recording = False
        self._on_status("识别中...")

        if self._stream:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                logger.debug("voice_input: 非致命异常(已静默吞掉)", exc_info=True)
            self._stream = None

        if not self._audio_data:
            self._on_status("未录到声音")
            return ""

        # 合并音频
        audio = np.concatenate(self._audio_data, axis=0)
        audio = audio.flatten()

        # 太短不算
        duration = len(audio) / self.SAMPLE_RATE
        if duration < 0.3:
            self._on_status("录音太短")
            return ""

        logger.info("Recording stopped: %.1fs", duration)

        # 保存临时文件
        tmp_path = os.path.join(tempfile.gettempdir(), f"pet_voice_{int(time.time())}.wav")
        self._save_wav(audio, tmp_path)

        # ASR 识别
        self._on_status("语音识别中...")
        logger.info("ASR provider: %s, ready=%s", 
                    getattr(self._asr, 'name', type(self._asr).__name__) if self._asr else 'None',
                    getattr(self._asr, 'is_ready', None) if self._asr else None)

        if not self._asr:
            self._on_status("ASR 模型未加载")
            self._cleanup(tmp_path)
            return ""

        try:
            logger.info("Calling ASR transcribe: %s", tmp_path)
            text = self._asr.transcribe(tmp_path, language=_asr_language())
            logger.info("ASR returned: '%s'", text[:50] if text else '(empty)')
            self._on_status("")
            self._cleanup(tmp_path)
            return text or ""
        except Exception as e:
            logger.error("ASR failed: %s", e, exc_info=True)
            self._on_status("识别失败")
            self._cleanup(tmp_path)
            return ""

    def transcribe_audio(self, audio: np.ndarray) -> str:
        """对给定的音频数据做 ASR（持续监听模式用，不依赖录音状态）。

        Args:
            audio: 1D float32 音频（16kHz 单声道）

        Returns:
            识别文本，失败返回空字符串
        """
        audio = np.asarray(audio).flatten()
        if len(audio) < int(self.SAMPLE_RATE * 0.3):
            self._on_status("录音太短")
            return ""
        tmp_path = os.path.join(tempfile.gettempdir(), f"pet_voice_cont_{int(time.time() * 1000)}.wav")
        self._save_wav(audio, tmp_path)
        self._on_status("语音识别中...")
        if not self._asr:
            self._on_status("ASR 模型未加载")
            self._cleanup(tmp_path)
            return ""
        try:
            text = self._asr.transcribe(tmp_path, language=_asr_language())
            self._on_status("")
            self._cleanup(tmp_path)
            return text or ""
        except Exception as e:
            logger.error("ASR failed: %s", e, exc_info=True)
            self._on_status("识别失败")
            self._cleanup(tmp_path)
            return ""

    def cancel(self):
        """取消录音（不识别）"""
        self._recording = False
        if self._stream:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                logger.debug("voice_input: 非致命异常(已静默吞掉)", exc_info=True)
            self._stream = None
        self._audio_data = []
        self._on_status("")

    def _audio_callback(self, indata, outdata, frames, time_info, status):
        """sounddevice 回调"""
        if self._recording:
            self._audio_data.append(indata.copy())
        # 持续监听：计算能量 + VAD 检测
        chunk = indata.copy()
        rms = np.sqrt(np.mean(chunk ** 2))
        self._latest_energy = rms
        if self._vad_callback is not None:
            self._vad_callback(chunk, rms)

    def peek_energy(self) -> float:
        """返回最近音频块的 RMS 能量（用于持续监听检测）。"""
        return self._latest_energy

    def set_vad_callback(self, cb):
        """设置持续监听模式下的 VAD 回调（每帧收到音频数据时调用）。"""
        self._vad_callback = cb

    def _save_wav(self, audio: np.ndarray, path: str):
        """保存为 wav 文件"""
        # 转为 int16
        audio_int16 = (audio * 32767).astype(np.int16)
        with wave.open(path, "w") as wf:
            wf.setnchannels(self.CHANNELS)
            wf.setsampwidth(2)
            wf.setframerate(self.SAMPLE_RATE)
            wf.writeframes(audio_int16.tobytes())

    def _cleanup(self, path: str):
        """删除临时文件"""
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            logger.debug("voice_input: 非致命异常(已静默吞掉)", exc_info=True)
