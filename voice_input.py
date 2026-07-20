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

# 确保 ffmpeg 可用（whisper 依赖）
try:
    import imageio_ffmpeg
    _ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    os.environ.setdefault('FFMPEG_BINARY', _ffmpeg)
    # 把 ffmpeg 目录加到 PATH
    _ffmpeg_dir = os.path.dirname(_ffmpeg)
    if _ffmpeg_dir not in os.environ.get('PATH', ''):
        os.environ['PATH'] = _ffmpeg_dir + os.pathsep + os.environ.get('PATH', '')
except Exception:
    pass
import threading
import time
import wave

import numpy as np

logger = logging.getLogger(__name__)

# 延迟导入 whisper（加载慢）
_whisper_model = None
_whisper_loading = False


def _get_whisper_model():
    """懒加载 Whisper 模型"""
    global _whisper_model, _whisper_loading
    if _whisper_model is not None:
        return _whisper_model
    if _whisper_loading:
        return None
    _whisper_loading = True
    try:
        import whisper
        logger.info("Whisper 模型加载中... (base)")
        _whisper_model = whisper.load_model("base")
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

    def __init__(self, asr_provider=None):
        self._asr = asr_provider
        self._recording = False
        self._audio_data: list[np.ndarray] = []
        self._stream = None
        self._on_status: callable = lambda msg: None  # 状态回调

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
            self._stream = sd.Stream(
                samplerate=self.SAMPLE_RATE,
                channels=self.CHANNELS,
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
                pass
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
            text = self._asr.transcribe(tmp_path, language="zh")
            logger.info("ASR returned: '%s'", text[:50] if text else '(empty)')
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
                pass
            self._stream = None
        self._audio_data = []
        self._on_status("")

    def _audio_callback(self, indata, outdata, frames, time_info, status):
        """sounddevice 回调"""
        if self._recording:
            self._audio_data.append(indata.copy())

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
            pass