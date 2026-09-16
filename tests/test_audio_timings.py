"""音频能量分段测试（口型时间轴的通用来源）。

核心价值：**任何 provider、任何格式都能用**——不依赖引擎返回词边界。
本机有 numpy/soundfile/pydub/ffmpeg，所以只要拿到音频文件就能算。

硬不变量：解码失败/无 numpy/全静音/任何异常 → None，调用方回落正弦包络。
"""
from __future__ import annotations

import math
import os
import struct
import wave

import pytest


def _write_wav(path, samples, sr=16000):
    """写一个 16-bit 单声道 wav。samples 是 [-1,1] 的浮点列表。"""
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        data = b"".join(
            struct.pack("<h", max(-32768, min(32767, int(s * 32767)))) for s in samples
        )
        wf.writeframes(data)


def _tone(dur_s, sr=16000, freq=220.0, amp=0.6):
    return [amp * math.sin(2 * math.pi * freq * i / sr) for i in range(int(dur_s * sr))]


def _silence(dur_s, sr=16000):
    return [0.0] * int(dur_s * sr)


# ── compute_segments 纯函数 ──────────────────────────


def test_compute_segments_finds_two_speech_runs():
    """两段声音、中间静音 → 两个段。"""
    import numpy as np
    from tts_provider.audio_timings import compute_segments

    sr = 16000
    sig = _tone(0.3) + _silence(0.4) + _tone(0.3)
    segs = compute_segments(np.array(sig, dtype=np.float32), sr)
    assert len(segs) == 2, f"应识别出 2 段，实际 {segs}"
    assert segs[0][0] < 100 and segs[1][0] > 600


def test_compute_segments_all_silence_returns_empty():
    import numpy as np
    from tts_provider.audio_timings import compute_segments

    segs = compute_segments(np.zeros(16000, dtype=np.float32), 16000)
    assert segs == []


def test_compute_segments_empty_input():
    import numpy as np
    from tts_provider.audio_timings import compute_segments

    assert compute_segments(np.array([], dtype=np.float32), 16000) == []
    assert compute_segments(None, 16000) == []
    assert compute_segments(np.zeros(100, dtype=np.float32), 0) == []


def test_compute_segments_continuous_speech_is_one_run():
    """连续说话不该被切成多段。"""
    import numpy as np
    from tts_provider.audio_timings import compute_segments

    segs = compute_segments(np.array(_tone(1.0), dtype=np.float32), 16000)
    assert len(segs) == 1


def test_short_blip_is_filtered():
    """极短的气声/爆音应被丢弃。"""
    import numpy as np
    from tts_provider.audio_timings import compute_segments

    sr = 16000
    sig = _tone(0.30) + _silence(0.30) + _tone(0.02) + _silence(0.30) + _tone(0.30)
    segs = compute_segments(np.array(sig, dtype=np.float32), sr)
    assert len(segs) == 2, f"20ms 的碎片应被丢弃，实际 {segs}"


def test_threshold_is_adaptive_not_absolute():
    """同一段音频整体放大/缩小，分段结果应一致（阈值随峰值走）。"""
    import numpy as np
    from tts_provider.audio_timings import compute_segments

    sr = 16000
    base = _tone(0.3, amp=0.5) + _silence(0.3) + _tone(0.3, amp=0.5)
    loud = [min(1.0, s * 1.8) for s in base]
    quiet = [s * 0.1 for s in base]

    a = compute_segments(np.array(base, dtype=np.float32), sr)
    b = compute_segments(np.array(loud, dtype=np.float32), sr)
    c = compute_segments(np.array(quiet, dtype=np.float32), sr)
    assert len(a) == len(b) == len(c) == 2, f"自适应阈值失效: {a} {b} {c}"


def test_segments_are_ordered_and_non_overlapping():
    import numpy as np
    from tts_provider.audio_timings import compute_segments

    sr = 16000
    sig = _tone(0.2) + _silence(0.3) + _tone(0.2) + _silence(0.3) + _tone(0.2)
    segs = compute_segments(np.array(sig, dtype=np.float32), sr)
    for (s1, e1), (s2, e2) in zip(segs, segs[1:]):
        assert e1 <= s2, "段不得重叠"
        assert e1 > s1 and e2 > s2, "段时长必须为正"


# ── 文件级：analyze_file ─────────────────────────────


def test_analyze_file_reads_wav(tmp_path):
    from tts_provider.audio_timings import analyze_file

    p = tmp_path / "a.wav"
    _write_wav(p, _tone(0.3) + _silence(0.4) + _tone(0.3))
    segs = analyze_file(str(p))
    assert segs and len(segs) == 2


def test_analyze_file_missing_returns_none(tmp_path):
    from tts_provider.audio_timings import analyze_file

    assert analyze_file(str(tmp_path / "nope.wav")) is None
    assert analyze_file("") is None


def test_analyze_file_garbage_returns_none(tmp_path):
    """非音频文件不能让调用方炸——返回 None。"""
    from tts_provider.audio_timings import analyze_file

    p = tmp_path / "junk.wav"
    p.write_bytes(b"this is definitely not audio")
    assert analyze_file(str(p)) is None


def test_analyze_file_silent_wav_returns_none(tmp_path):
    from tts_provider.audio_timings import analyze_file

    p = tmp_path / "silent.wav"
    _write_wav(p, _silence(0.5))
    assert analyze_file(str(p)) is None


# ── 侧车读写 ─────────────────────────────────────────


def test_save_load_roundtrip(tmp_path):
    from tts_provider.audio_timings import load_segments, save_segments

    audio = str(tmp_path / "x.wav")
    open(audio, "wb").write(b"\x00" * 16)
    saved = save_segments(audio, [(0, 300), (600, 900)])
    assert saved and os.path.exists(saved)
    assert load_segments(audio) == [(0, 300), (600, 900)]


def test_save_segments_drops_invalid(tmp_path):
    from tts_provider.audio_timings import load_segments, save_segments

    audio = str(tmp_path / "y.wav")
    open(audio, "wb").write(b"\x00" * 8)
    save_segments(audio, [(0, 300), (500, 500), (-5, 100), ("a", 10)])
    assert load_segments(audio) == [(0, 300)]


def test_save_segments_empty_returns_none(tmp_path):
    from tts_provider.audio_timings import save_segments

    audio = str(tmp_path / "z.wav")
    assert save_segments(audio, []) is None
    assert save_segments(audio, None) is None


def test_load_segments_missing_and_corrupt(tmp_path):
    from tts_provider.audio_timings import load_segments, segments_path

    audio = str(tmp_path / "c.wav")
    assert load_segments(audio) is None
    open(segments_path(audio), "w", encoding="utf-8").write("{broken")
    assert load_segments(audio) is None


# ── ensure_timings 统一入口 ──────────────────────────


def test_ensure_timings_prefers_native_words(tmp_path):
    """★ 有原生词边界时走词边界，不做能量分析。"""
    from tts_provider.audio_timings import ensure_timings
    from tts_provider.word_timings import load_words, sidecar_path

    p = tmp_path / "w.wav"
    _write_wav(p, _tone(0.4))
    out = ensure_timings(str(p), [{"o": 0, "d": 200}, {"o": 300, "d": 100}])
    assert out == sidecar_path(str(p))
    assert load_words(str(p)) == [(0, 200), (300, 400)]


def test_ensure_timings_falls_back_to_energy(tmp_path):
    """★ 无词边界时自动算能量分段——这就是"全引擎通用"的落点。"""
    from tts_provider.audio_timings import ensure_timings, load_segments, segments_path

    p = tmp_path / "e.wav"
    _write_wav(p, _tone(0.3) + _silence(0.4) + _tone(0.3))
    out = ensure_timings(str(p))
    assert out == segments_path(str(p))
    segs = load_segments(str(p))
    assert segs and len(segs) == 2


def test_ensure_timings_is_idempotent(tmp_path):
    """已有侧车不重复分析（第二次应返回同一路径且不改文件）。"""
    from tts_provider.audio_timings import ensure_timings

    p = tmp_path / "i.wav"
    _write_wav(p, _tone(0.3))
    first = ensure_timings(str(p))
    assert first
    mtime = os.path.getmtime(first)
    second = ensure_timings(str(p))
    assert second == first
    assert os.path.getmtime(first) == mtime


def test_ensure_timings_missing_file_returns_none(tmp_path):
    from tts_provider.audio_timings import ensure_timings

    assert ensure_timings(str(tmp_path / "gone.wav")) is None
    assert ensure_timings("") is None


# ── 播放器接线 ───────────────────────────────────────


def test_player_reads_segments_sidecar(tmp_path):
    """★ 播放器必须能吃 .segments.json（否则通用化只做了一半）。"""
    from tts_provider.audio_timings import save_segments
    from ui.tts_player import TTSTtsPlayer

    audio = str(tmp_path / "p.wav")
    open(audio, "wb").write(b"\x00" * 8)
    save_segments(audio, [(0, 200), (800, 1000)])

    p = TTSTtsPlayer()
    p._current_audio_path = audio
    assert p._word_timings() == [(0, 200), (800, 1000)]

    p.is_playing = lambda: True
    p.position_seconds = lambda: 0.5          # 段间空隙
    assert p.current_level() == 0.0
    p.position_seconds = lambda: 0.1          # 段内
    assert p.current_level() > 0.0


def test_player_prefers_words_over_segments(tmp_path):
    """两种侧车都在时，词边界优先（更精确）。"""
    from tts_provider.audio_timings import save_segments
    from tts_provider.word_timings import save_words
    from ui.tts_player import TTSTtsPlayer

    audio = str(tmp_path / "both.wav")
    open(audio, "wb").write(b"\x00" * 8)
    save_segments(audio, [(0, 900)])
    save_words(audio, [{"o": 0, "d": 100}])

    p = TTSTtsPlayer()
    p._current_audio_path = audio
    assert p._word_timings() == [(0, 100)], "词边界应优先于能量分段"


def test_providers_call_ensure_timings():
    """★ 接线检查：四个文件式 provider 都必须调用 ensure_timings。"""
    import inspect

    from tts_provider import api_tts, cosyvoice, edge_tts, mimo_tts

    for mod, cls in (
        (edge_tts, edge_tts.EdgeTtsProvider),
        (mimo_tts, mimo_tts.MimoTtsProvider),
        (api_tts, api_tts.ApiTtsProvider),
        (cosyvoice, cosyvoice.CosyVoiceProvider),
    ):
        src = inspect.getsource(cls.synthesize)
        assert "ensure_timings" in src, f"{cls.__name__} 未接入口型时间轴"
