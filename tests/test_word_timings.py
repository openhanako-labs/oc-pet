"""词级口型（TTS 侧车时间戳）测试。

覆盖：
- 侧车读写往返、损坏/缺失/空数据的容错
- level_at 纯函数：词内开合、词间闭嘴、越界归零
- EdgeTtsProvider 合成时落盘侧车（mock edge_tts）
- TTSTtsPlayer.current_level 优先词级、无侧车回落包络
"""
from __future__ import annotations

import json
import math
import os

import pytest


# ── word_timings 纯函数 ──────────────────────────────


def test_sidecar_path_convention():
    from tts_provider.word_timings import sidecar_path
    assert sidecar_path("a.mp3").endswith("a.mp3.words.json")
    assert sidecar_path("C:/x/y.wav") == "C:/x/y.wav.words.json"


def test_save_and_load_roundtrip(tmp_path):
    from tts_provider.word_timings import save_words, load_words
    audio = str(tmp_path / "a.mp3")
    open(audio, "wb").write(b"\x00" * 16)
    saved = save_words(audio, [
        {"o": 0, "d": 400, "t": "你好"},
        {"o": 500, "d": 300, "t": "世界"},
    ])
    assert saved and os.path.exists(saved)
    words = load_words(audio)
    assert words == [(0, 400), (500, 800)]


def test_save_words_sorts_and_drops_invalid(tmp_path):
    from tts_provider.word_timings import save_words, load_words
    audio = str(tmp_path / "b.mp3")
    open(audio, "wb").write(b"\x00" * 8)
    save_words(audio, [
        {"o": 900, "d": 100},
        {"o": 0, "d": 200},
        {"o": 5, "d": 0},        # 时长 0 → 丢
        {"o": -3, "d": 50},      # 负偏移 → 丢
        {"o": "x", "d": 50},     # 非数值 → 丢
        "not-a-dict",            # 非字典 → 丢
    ])
    assert load_words(audio) == [(0, 200), (900, 1000)]


def test_save_words_empty_returns_none(tmp_path):
    from tts_provider.word_timings import save_words
    audio = str(tmp_path / "c.mp3")
    assert save_words(audio, []) is None
    assert save_words(audio, [{"o": 1, "d": 0}]) is None


def test_load_words_missing_file_returns_none(tmp_path):
    from tts_provider.word_timings import load_words
    assert load_words(str(tmp_path / "nope.mp3")) is None


def test_load_words_corrupt_json_returns_none(tmp_path):
    from tts_provider.word_timings import load_words, sidecar_path
    audio = str(tmp_path / "d.mp3")
    open(sidecar_path(audio), "w", encoding="utf-8").write("{not json")
    assert load_words(audio) is None


def test_load_words_wrong_shape_returns_none(tmp_path):
    from tts_provider.word_timings import load_words, sidecar_path
    audio = str(tmp_path / "e.mp3")
    with open(sidecar_path(audio), "w", encoding="utf-8") as f:
        json.dump({"words": "nope"}, f)
    assert load_words(audio) is None


def test_level_at_inside_word_is_positive():
    from tts_provider.word_timings import level_at
    words = [(0, 1000)]
    lv = level_at(words, 0.25)
    assert 0.0 <= lv <= 0.27
    assert lv > 0.0


def test_level_at_between_words_is_zero():
    """★ 关键：词间空隙必须归零——这正是词级口型比正弦包络强的地方。"""
    from tts_provider.word_timings import level_at
    words = [(0, 200), (800, 1000)]
    assert level_at(words, 0.5) == 0.0    # 落在 200~800ms 空隙
    assert level_at(words, 0.9) > 0.0     # 落在第二个词内


def test_level_at_out_of_range_is_zero():
    from tts_provider.word_timings import level_at
    words = [(100, 200)]
    assert level_at(words, -0.1) == 0.0
    assert level_at(words, 0.05) == 0.0   # 第一个词之前
    assert level_at(words, 5.0) == 0.0    # 最后一个词之后


def test_level_at_empty_and_bad_input():
    from tts_provider.word_timings import level_at
    assert level_at([], 0.5) == 0.0
    assert level_at(None, 0.5) == 0.0
    assert level_at([(0, 100)], None) == 0.0
    assert level_at([(0, 100)], "bad") == 0.0


def test_level_at_within_envelope_max():
    """词内电平不得超过渲染器预期的包络上限（0.27）。"""
    from tts_provider.word_timings import level_at
    words = [(0, 500)]
    vals = [level_at(words, i / 200.0) for i in range(100)]
    assert max(vals) <= 0.27 + 1e-9
    assert min(vals) >= 0.0


# ── EdgeTtsProvider 落盘侧车 ─────────────────────────


def test_edge_provider_writes_sidecar(tmp_path, monkeypatch):
    """★ 合成时应把 WordBoundary 落盘为 <mp3>.words.json。"""
    import sys
    import types

    from tts_provider import edge_tts as edge_mod

    class _FakeCommunicate:
        def __init__(self, text, voice, rate=None, pitch=None, volume=None, boundary=None):
            assert boundary == "WordBoundary", "必须请求词边界"
            self._text = text

        async def stream(self):
            yield {"type": "audio", "data": b"\x00" * 32}
            yield {"type": "WordBoundary", "offset": 1000000, "duration": 4750000, "text": "你好"}
            yield {"type": "WordBoundary", "offset": 8125000, "duration": 1250000, "text": "我"}

    fake = types.ModuleType("edge_tts")
    fake.Communicate = _FakeCommunicate
    monkeypatch.setitem(sys.modules, "edge_tts", fake)

    out_dir = tmp_path / "cache"
    monkeypatch.setattr(edge_mod, "OUTPUT_DIR", out_dir)

    prov = edge_mod.EdgeTtsProvider(voice="zh-CN-XiaoxiaoNeural")
    path = prov.synthesize("你好我")
    assert path and os.path.exists(path)

    from tts_provider.word_timings import load_words
    words = load_words(path)
    assert words == [(100, 575), (812, 937)]


def test_edge_provider_survives_sidecar_failure(tmp_path, monkeypatch):
    """侧车写入失败不能影响出声——音频文件照样产出。"""
    import sys
    import types

    from tts_provider import edge_tts as edge_mod

    class _FakeCommunicate:
        def __init__(self, text, voice, rate=None, pitch=None, volume=None, boundary=None):
            pass

        async def stream(self):
            yield {"type": "audio", "data": b"\x11" * 64}

    fake = types.ModuleType("edge_tts")
    fake.Communicate = _FakeCommunicate
    monkeypatch.setitem(sys.modules, "edge_tts", fake)
    monkeypatch.setattr(edge_mod, "OUTPUT_DIR", tmp_path / "c2")

    prov = edge_mod.EdgeTtsProvider()
    path = prov.synthesize("无词边界")
    assert path and os.path.exists(path)
    assert os.path.getsize(path) == 64


# ── 播放器侧：词级优先，回落包络 ─────────────────────


def _make_player_stub(audio_path, position_s):
    """构造一个绕过 Qt 的 TTSTtsPlayer，只测 current_level 的分支。"""
    from ui.tts_player import TTSTtsPlayer
    p = TTSTtsPlayer()
    p._current_audio_path = audio_path
    p.is_playing = lambda: True
    p.position_seconds = lambda: position_s
    return p


def test_player_prefers_word_timings(tmp_path):
    from tts_provider.word_timings import save_words
    from ui.tts_player import TTSTtsPlayer
    audio = str(tmp_path / "p.mp3")
    open(audio, "wb").write(b"\x00" * 8)
    save_words(audio, [{"o": 0, "d": 200}, {"o": 800, "d": 200}])

    p = _make_player_stub(audio, 0.5)   # 落在词间空隙
    assert p.current_level() == 0.0, "有侧车时应走词级（词间闭嘴）"

    p2 = _make_player_stub(audio, 0.1)  # 落在第一个词内
    assert p2.current_level() > 0.0


def test_player_falls_back_to_envelope_without_sidecar(tmp_path):
    from ui.tts_player import TTSTtsPlayer, _envelope_level
    audio = str(tmp_path / "q.mp3")
    open(audio, "wb").write(b"\x00" * 8)

    p = _make_player_stub(audio, 0.12)
    assert p.current_level() == pytest.approx(_envelope_level(0.12))


def test_player_returns_none_when_not_playing():
    from ui.tts_player import TTSTtsPlayer
    p = TTSTtsPlayer()
    p.is_playing = lambda: False
    assert p.current_level() is None


def test_player_returns_none_without_position():
    from ui.tts_player import TTSTtsPlayer
    p = TTSTtsPlayer()
    p.is_playing = lambda: True
    p.position_seconds = lambda: None
    assert p.current_level() is None


def test_player_caches_word_timings(tmp_path):
    """同一音频不重复读盘。"""
    from tts_provider.word_timings import save_words
    audio = str(tmp_path / "r.mp3")
    open(audio, "wb").write(b"\x00" * 8)
    save_words(audio, [{"o": 0, "d": 100}])

    p = _make_player_stub(audio, 0.05)
    first = p._word_timings()
    assert first == [(0, 100)]
    # 删掉侧车后仍返回缓存值（证明没重新读盘）
    os.remove(f"{audio}.words.json")
    assert p._word_timings() == first


def test_play_file_records_audio_path(tmp_path):
    """★ 接线检查：_play_file 必须记下路径，否则 current_level 永远查不到侧车。"""
    import inspect
    from ui.tts_player import TTSTtsPlayer
    src = inspect.getsource(TTSTtsPlayer._play_file)
    assert "_current_audio_path" in src, \
        "不记录音频路径 → 词级口型永远不生效"
