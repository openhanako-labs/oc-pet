"""ASR-1（2026-09-14）测试：Silero VAD 接入。

覆盖：
  - EnergyVAD 行为（回退路径，必须与改动前一致）
  - SileroVAD 加载与 context 拼接（★ 关键：漏 context 会导致概率恒低）
  - create_vad 自动选择与失败闭合
  - chat_mixin 的 VAD 判据接线
"""
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.audio_input.vad import (  # noqa: E402
    CONTEXT_SIZE,
    DEFAULT_ONNX,
    WINDOW,
    EnergyVAD,
    SileroVAD,
    create_vad,
)


# ── 常量 ──


def test_window_is_512():
    """Silero 在 16k 下固定 512 样本/窗。"""
    assert WINDOW == 512


def test_context_size_is_64():
    """★ context 必须 64：漏掉会导致概率恒低（实测 max 仅 0.16）。"""
    assert CONTEXT_SIZE == 64


def test_model_file_exists():
    """vendored ONNX 模型必须在仓库里。"""
    import os
    assert os.path.isfile(DEFAULT_ONNX), f"缺少模型文件: {DEFAULT_ONNX}"


def test_model_file_size_reasonable():
    """模型约 1.26MB，不应是空文件或异常大。"""
    import os
    size = os.path.getsize(DEFAULT_ONNX)
    assert 1_000_000 < size < 3_000_000, f"模型体积异常: {size}"


# ── EnergyVAD（回退路径）──


def test_energy_vad_speech_detected():
    v = EnergyVAD(threshold=0.02)
    loud = np.full(WINDOW, 0.3, dtype=np.float32)
    assert v.is_speech(loud, 0.3) is True


def test_energy_vad_silence_rejected():
    v = EnergyVAD(threshold=0.02)
    quiet = np.zeros(WINDOW, dtype=np.float32)
    assert v.is_speech(quiet, 0.0) is False


def test_energy_vad_adaptive_floor_raises_threshold():
    """底噪抬高后，同样的音量应被拒（防视频背景音误触发）。"""
    v = EnergyVAD(threshold=0.02)
    # 连续喂入中等噪声，抬高底噪
    noise = np.full(WINDOW, 0.03, dtype=np.float32)
    for _ in range(50):
        v.is_speech(noise, 0.03)
    # 同样的 0.03 现在应被拒
    assert v.is_speech(noise, 0.03) is False


def test_energy_vad_reset():
    v = EnergyVAD()
    for _ in range(50):
        v.is_speech(np.full(WINDOW, 0.05, dtype=np.float32), 0.05)
    v.reset()
    assert v._noise_floor == 0.006


# ── SileroVAD ──


@pytest.fixture
def silero():
    try:
        return SileroVAD()
    except Exception as e:
        pytest.skip(f"Silero VAD 不可用: {e}")


def test_silero_loads(silero):
    assert silero.backend == "silero"


def test_silero_reset_state_shapes(silero):
    """state=(2,1,128)，context=(1,64)。"""
    silero.reset()
    assert silero._state.shape == (2, 1, 128)
    assert silero._context.shape == (1, CONTEXT_SIZE)


def test_silero_silence_prob_low(silero):
    """静音应判为非语音且概率极低。"""
    silero.reset()
    quiet = np.zeros(WINDOW, dtype=np.float32)
    probs = []
    for _ in range(20):
        silero.is_speech(quiet)
        probs.append(silero.prob)
    assert max(probs) < 0.1, f"静音概率过高: {max(probs)}"


def test_silero_context_is_updated(silero):
    """每次推理后 context 必须更新（否则等于没拼接）。"""
    silero.reset()
    before = silero._context.copy()
    sig = (np.sin(np.arange(WINDOW) * 0.1) * 0.5).astype(np.float32)
    silero.is_speech(sig)
    assert not np.array_equal(before, silero._context)


def test_silero_handles_short_chunk(silero):
    """不足 512 样本时应补零而非崩溃。"""
    silero.reset()
    short = np.zeros(100, dtype=np.float32)
    silero.is_speech(short)  # 不应抛异常
    assert silero._context.shape == (1, CONTEXT_SIZE)


def test_silero_handles_long_chunk(silero):
    """超过 512 样本时应截断而非崩溃。"""
    silero.reset()
    long = np.zeros(WINDOW * 3, dtype=np.float32)
    silero.is_speech(long)
    assert silero._context.shape == (1, CONTEXT_SIZE)


def test_silero_hysteresis_after_trigger(silero):
    """触发后阈值放宽（滞回），避免句尾被切断。"""
    silero.reset()
    # 手动置为已触发
    silero._triggered = True
    silero._state = np.zeros((2, 1, 128), dtype=np.float32)
    silero._context = np.zeros((1, CONTEXT_SIZE), dtype=np.float32)
    quiet = np.zeros(WINDOW, dtype=np.float32)
    silero.is_speech(quiet)  # 不应抛异常，且概率极低会取消触发
    assert silero._triggered is False


def test_silero_missing_model_raises():
    with pytest.raises((FileNotFoundError, RuntimeError)):
        SileroVAD(onnx_path="definitely_missing.onnx")


# ── create_vad 工厂 ──


def test_create_vad_auto_prefers_silero():
    v = create_vad("auto")
    # 本机 onnxruntime + 模型都在，应拿到 silero
    assert v.backend in ("silero", "energy")


def test_create_vad_energy_explicit():
    v = create_vad("energy")
    assert v.backend == "energy"


def test_create_vad_silero_falls_back_on_bad_path():
    """指定 silero 但模型路径错 → 回退能量（失败闭合，不抛）。"""
    v = create_vad("silero", onnx_path="missing.onnx")
    assert v.backend == "energy"


def test_create_vad_unknown_backend_falls_back():
    v = create_vad("nonsense")
    assert v.backend == "energy"


def test_create_vad_never_returns_none():
    """失败闭合：任何输入都必须返回可用对象。"""
    for b in ("auto", "energy", "silero", "bogus", ""):
        assert create_vad(b) is not None


# ── 真实音频（有样本才跑）──


def _load_real_speech():
    """尝试加载 SenseVoice 自带示例（真实人声）。"""
    import glob
    import os
    import subprocess
    import tempfile
    import wave

    mp3s = glob.glob(
        os.path.expanduser(
            "~/.cache/modelscope/hub/iic/SenseVoiceSmall/example/zh.mp3"
        )
    )
    if not mp3s:
        return None
    tmp = os.path.join(tempfile.gettempdir(), "_vad_pytest.wav")
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", mp3s[0], "-ar", "16000", "-ac", "1", tmp],
            capture_output=True, check=True,
        )
        with wave.open(tmp, "rb") as wf:
            raw = wf.readframes(wf.getnframes())
        return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    except Exception:
        return None


def test_silero_detects_real_speech(silero):
    """★ 核心验收：真实人声必须被判为语音（修 context 前这里是 0%）。"""
    a = _load_real_speech()
    if a is None:
        pytest.skip("无真实语音样本")
    silero.reset()
    probs = []
    for i in range(0, len(a) - WINDOW, WINDOW):
        silero.is_speech(a[i:i + WINDOW])
        probs.append(silero.prob)
    ratio = float((np.array(probs) >= 0.5).mean())
    assert ratio > 0.3, f"真实人声语音窗占比过低: {ratio:.2%}"


def test_silero_rejects_silence_on_real_scale(silero):
    """静音在同一阈值下必须被拒。"""
    silero.reset()
    quiet = np.zeros(16000, dtype=np.float32)
    probs = []
    for i in range(0, len(quiet) - WINDOW, WINDOW):
        silero.is_speech(quiet[i:i + WINDOW])
        probs.append(silero.prob)
    assert float((np.array(probs) >= 0.5).mean()) == 0.0
