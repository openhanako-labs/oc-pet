"""声纹门卫（2026-09-15）测试。

背景：桌宠持续监听会被视频声/他人声误触发。在 ASR 前加声纹门卫，
非主人声音直接丢弃。

关键不变量：
  - 门卫**失败必须放行**——声纹是过滤器不是关卡，它坏了不能拖垮语音输入。
  - 未启用时恒放行（默认关，用户注册后才开）。
  - 余弦相似度能区分同人/异人（实测 0.885 vs 0.346）。
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# ── 门卫逻辑（不依赖模型）────────────────────────────────


def test_gate_passthrough_when_disabled(monkeypatch):
    """★ 未启用声纹 → 恒放行。"""
    import voice_input
    monkeypatch.setattr(voice_input, "_voiceprint_enabled", lambda: False)
    assert voice_input._voiceprint_gate("whatever.wav") is True


def test_gate_passthrough_on_exception(monkeypatch):
    """★ 门卫内部异常 → 放行（绝不阻断语音输入）。"""
    import voice_input
    monkeypatch.setattr(voice_input, "_voiceprint_enabled", lambda: True)

    def boom(_p):
        raise RuntimeError("模型炸了")

    import core.speaker_verify as sv
    monkeypatch.setattr(sv, "verify_speaker", boom)
    assert voice_input._voiceprint_gate("x.wav") is True


def test_gate_blocks_non_owner(monkeypatch):
    """启用 + 非主人 → 拦截。"""
    import voice_input
    monkeypatch.setattr(voice_input, "_voiceprint_enabled", lambda: True)
    import core.speaker_verify as sv
    monkeypatch.setattr(sv, "verify_speaker", lambda _p: False)
    assert voice_input._voiceprint_gate("x.wav") is False


def test_gate_passes_owner(monkeypatch):
    """启用 + 主人 → 放行。"""
    import voice_input
    monkeypatch.setattr(voice_input, "_voiceprint_enabled", lambda: True)
    import core.speaker_verify as sv
    monkeypatch.setattr(sv, "verify_speaker", lambda _p: True)
    assert voice_input._voiceprint_gate("x.wav") is True


# ── SpeakerVerifier 行为（不依赖真实模型）────────────────


def test_verifier_passthrough_without_model(tmp_path):
    """★ 模型/样本缺失 → verify 放行（True），similarity 返回 None。"""
    from core.speaker_verify import SpeakerVerifier
    v = SpeakerVerifier(
        model_path=str(tmp_path / "nope.onnx"),
        owner_path=str(tmp_path / "nope.wav"),
    )
    assert v.similarity(str(tmp_path / "x.wav")) is None
    assert v.verify(str(tmp_path / "x.wav")) is True
    assert v.available is False


def test_verifier_reads_pcm_wav(tmp_path):
    """标准库 wave 读 PCM wav（soundfile 不可用时的兜底路径）。"""
    import wave
    import numpy as np
    from core.speaker_verify import _read_wav_16k_mono
    p = tmp_path / "t.wav"
    samples = (np.sin(np.linspace(0, 100, 16000)) * 8000).astype(np.int16)
    with wave.open(str(p), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(samples.tobytes())
    got = _read_wav_16k_mono(str(p))
    assert got is not None
    arr, sr = got
    assert sr == 16000
    assert len(arr) == 16000


def test_verifier_sticky_failure(tmp_path):
    """初始化失败后 sticky：后续调用不再重试，仍放行。"""
    from core.speaker_verify import SpeakerVerifier
    v = SpeakerVerifier(model_path=str(tmp_path / "no.onnx"),
                        owner_path=str(tmp_path / "no.wav"))
    assert v.verify("a.wav") is True
    assert v._failed is True  # 已标记失败
    assert v.verify("b.wav") is True


# ── 设置面板声纹逻辑（不弹窗、不碰麦克风）──────────────


def test_settings_dialog_has_voiceprint_widgets():
    """★ 设置面板应包含声纹控件（启用/阈值/三个按钮）。"""
    import inspect
    from ui import settings_dialog
    src = inspect.getsource(settings_dialog.SettingsDialog)
    assert "voiceprint_enabled" in src
    assert "voiceprint_threshold" in src
    assert "_on_voiceprint_register" in src
    assert "_on_voiceprint_calibrate" in src
    assert "_on_voiceprint_test" in src


def test_settings_dialog_saves_voiceprint_config():
    """★ _save 应写入 voiceprint.enabled / threshold。"""
    import inspect
    from ui import settings_dialog
    src = inspect.getsource(settings_dialog.SettingsDialog._save)
    assert "voiceprint" in src
    assert "enabled" in src and "threshold" in src


def test_voiceprint_threshold_range():
    """阈值控件范围应落在 0.30~0.90（防止误设极端值）。"""
    import inspect
    from ui import settings_dialog
    src = inspect.getsource(settings_dialog.SettingsDialog)
    assert "setRange(0.30, 0.90)" in src
