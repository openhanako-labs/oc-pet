"""性能修复（2026-09-14）测试：卡顿与延迟三处优化。

用户实测反馈：\"TTS 刚播报两个字就开始卡，等下两个字出来继续播报卡\"。
日志实测：合成速率 0.36x 实时（低于播放所需 1.0x）→ 缓冲必然被抽干。

三处改动：
  ① TTS 流式：left_context 25→8（解码量 2.00x→1.32x）+ 预缓冲
  ② 工具热刷新：目录未变则跳过全量重扫
  ③ 后台任务避让：对话进行中不发反思/屏幕增强
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ── ① 流式参数 ──


def test_stream_params_defaults_use_cheaper_context():
    """★ left_context 默认必须已降到 8（原 25 使解码量翻倍）。"""
    from tts_provider.qwen_tts import QwenTtsProvider
    kw = QwenTtsProvider._stream_params()
    assert kw["left_context"] == 8, f"left_context 应降到 8，实为 {kw['left_context']}"
    assert kw["chunk_frames"] == 25
    assert kw["first_chunk_frames"] == 8


def test_stream_params_reads_config(monkeypatch):
    """config 能覆盖流式参数。"""
    import config as _cfg
    monkeypatch.setattr(_cfg, "load_config", lambda: {
        "tts": {"stream": {"chunk_frames": 20, "left_context": 4}}
    })
    from tts_provider.qwen_tts import QwenTtsProvider
    kw = QwenTtsProvider._stream_params()
    assert kw["chunk_frames"] == 20
    assert kw["left_context"] == 4


def test_stream_params_ignores_bad_values(monkeypatch):
    """非法配置不能污染参数（回退默认）。"""
    import config as _cfg
    monkeypatch.setattr(_cfg, "load_config", lambda: {
        "tts": {"stream": {"chunk_frames": "abc", "left_context": -5}}
    })
    from tts_provider.qwen_tts import QwenTtsProvider
    kw = QwenTtsProvider._stream_params()
    assert kw["chunk_frames"] == 25
    assert kw["left_context"] == 8


def test_decode_cost_reduced():
    """数学验证：left_context 25→8 让每块解码量从 2.00x 降到 1.32x。"""
    def cost(chunk, ctx):
        return (chunk + ctx) / chunk
    assert cost(25, 25) == pytest.approx(2.0)
    assert cost(25, 8) == pytest.approx(1.32, abs=0.01)
    assert cost(25, 8) < cost(25, 25)


# ── 预缓冲 ──


def test_set_prebuffer_computes_bytes():
    """预缓冲按采样率/声道换算成字节。"""
    from ui.streaming_pcm_player import StreamingPcmPlayer
    p = StreamingPcmPlayer(sample_rate=24000)
    p.set_prebuffer(1.5)
    # Int16 单声道 24k → 48000 字节/秒 → 1.5s = 72000
    assert p._prebuffer_bytes == 72000


def test_set_prebuffer_zero_disables():
    from ui.streaming_pcm_player import StreamingPcmPlayer
    p = StreamingPcmPlayer(sample_rate=24000)
    p.set_prebuffer(0)
    assert p._prebuffer_bytes == 0


def test_set_prebuffer_handles_bad_input():
    from ui.streaming_pcm_player import StreamingPcmPlayer
    p = StreamingPcmPlayer(sample_rate=24000)
    p.set_prebuffer("abc")
    assert p._prebuffer_bytes == 0


def test_should_start_playback_disabled_always_true():
    """预缓冲关闭 → 立即开播（回旧行为）。"""
    from ui.streaming_pcm_player import StreamingPcmPlayer

    class _Dev:
        def is_eof(self):
            return False

    p = StreamingPcmPlayer(sample_rate=24000)
    p.set_prebuffer(0)
    assert p._should_start_playback(_Dev(), 1) is True


def test_should_start_playback_waits_until_enough():
    """★ 未攒够不开播——这是\"两个字一卡\"的修复核心。"""
    import time
    from ui.streaming_pcm_player import StreamingPcmPlayer

    class _Dev:
        def is_eof(self):
            return False

    p = StreamingPcmPlayer(sample_rate=24000)
    p.set_prebuffer(1.5, max_wait_s=60)
    p._playback_wait_from = time.monotonic()
    assert p._should_start_playback(_Dev(), 1000) is False, "未攒够不应开播"
    assert p._should_start_playback(_Dev(), p._prebuffer_bytes) is True


def test_should_start_playback_on_eof():
    """合成已结束 → 不等了（短句应直接播，否则永远没声音）。"""
    from ui.streaming_pcm_player import StreamingPcmPlayer

    class _Dev:
        def is_eof(self):
            return True

    p = StreamingPcmPlayer(sample_rate=24000)
    p.set_prebuffer(5.0, max_wait_s=60)
    assert p._should_start_playback(_Dev(), 100) is True


def test_should_start_playback_on_timeout():
    """★ 超时保护：合成极慢时不能永远不出声。"""
    import time
    from ui.streaming_pcm_player import StreamingPcmPlayer

    class _Dev:
        def is_eof(self):
            return False

    p = StreamingPcmPlayer(sample_rate=24000)
    p.set_prebuffer(30.0, max_wait_s=0.5)
    p._playback_wait_from = time.monotonic() - 10.0  # 已等 10s
    assert p._should_start_playback(_Dev(), 1000) is True


def test_prebuffer_reset_on_prepare():
    """新一句要重置等待计时。"""
    import inspect
    from ui import streaming_pcm_player as sp
    src = inspect.getsource(sp.StreamingPcmPlayer.prepare)
    assert "_playback_wait_from" in src


# ── ② 工具热刷新 ──


def test_plugins_dir_stamp_detects_change(tmp_path, monkeypatch):
    """★ 目录戳能发现新增/删除插件。"""
    from core.conversation_engine import ConversationEngine
    root = tmp_path / "plugins"
    root.mkdir()
    (root / "a").mkdir()
    (root / "a" / "manifest.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(ConversationEngine, "_plugin_roots",
                        staticmethod(lambda: [root]))

    s1 = ConversationEngine._plugins_dir_stamp()
    # 新增一个插件
    (root / "b").mkdir()
    (root / "b" / "manifest.json").write_text("{}", encoding="utf-8")
    s2 = ConversationEngine._plugins_dir_stamp()
    assert s1 != s2, "新增插件后戳应变化"


def test_plugins_dir_stamp_stable_when_unchanged(tmp_path, monkeypatch):
    """目录没动 → 戳不变（这样热刷新能跳过重扫）。"""
    from core.conversation_engine import ConversationEngine
    root = tmp_path / "plugins"
    root.mkdir()
    (root / "a").mkdir()
    (root / "a" / "manifest.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(ConversationEngine, "_plugin_roots",
                        staticmethod(lambda: [root]))
    assert ConversationEngine._plugins_dir_stamp() == \
        ConversationEngine._plugins_dir_stamp()


def test_hot_refresh_skips_when_stamp_unchanged():
    """★ 戳未变时必须跳过全量重扫（这是每 30s 白烧 CPU 的修复）。"""
    import inspect
    from core.conversation_engine import ConversationEngine
    src = inspect.getsource(ConversationEngine._hot_refresh_tools)
    assert "_plugins_dir_stamp" in src
    assert "return" in src


def test_plugins_dir_stamp_handles_missing_dirs(monkeypatch):
    """目录不存在时返回 None（不抛异常）。"""
    from core.conversation_engine import ConversationEngine
    monkeypatch.setattr(ConversationEngine, "_plugin_roots",
                        staticmethod(lambda: []))
    assert ConversationEngine._plugins_dir_stamp() is None


# ── ③ 后台任务避让 ──


def test_is_conversation_busy_detects_pending_chat():
    """等回复时算忙。"""
    from pet import PetWindow
    class _Fake:
        _pending_chat = True
    assert PetWindow._is_conversation_busy(_Fake()) is True


def test_is_conversation_busy_false_when_idle():
    from pet import PetWindow

    class _Fake:
        _pending_chat = False
        _stream_player = None
        _tts_player = None

    assert PetWindow._is_conversation_busy(_Fake()) is False


def test_is_conversation_busy_detects_tts_playing():
    """TTS 播报中算忙。"""
    from pet import PetWindow

    class _P:
        def is_playing(self):
            return True

    class _Fake:
        _pending_chat = False
        _stream_player = _P()
        _tts_player = None

    assert PetWindow._is_conversation_busy(_Fake()) is True


def test_is_conversation_busy_survives_broken_player():
    """播放器状态查询异常不能崩。"""
    from pet import PetWindow

    class _P:
        def is_playing(self):
            raise RuntimeError("player dead")

    class _Fake:
        _pending_chat = False
        _stream_player = _P()
        _tts_player = None

    assert PetWindow._is_conversation_busy(_Fake()) is False


def test_maybe_reflect_skips_when_busy():
    """★ 对话进行中不发起反思（它会吃 29s/12623 token，抢回复的 API）。"""
    import inspect
    from pet import PetWindow
    src = inspect.getsource(PetWindow._maybe_reflect)
    assert "_is_conversation_busy" in src


def test_screen_should_enrich_skips_when_busy():
    """★ 对话进行中不做屏幕 LLM 增强（抢同一条 API）。"""
    from core.perception.screen import ScreenPerception
    sp = ScreenPerception.__new__(ScreenPerception)
    sp.busy_check = lambda: True
    sp._last_enriched_scene = ""
    sp._last_enrich_at = 0.0
    sp._enrich_cooldown = 300

    class _Scene:
        scene = "coding"

    assert sp._should_enrich(_Scene(), 1e9) is False


def test_screen_enrich_works_when_not_busy():
    """不忙时正常放行（避让不能误伤）。"""
    from core.perception.screen import ScreenPerception
    sp = ScreenPerception.__new__(ScreenPerception)
    sp.busy_check = lambda: False
    sp._last_enriched_scene = ""
    sp._last_enrich_at = 0.0
    sp._enrich_cooldown = 300

    class _Scene:
        scene = "coding"

    assert sp._should_enrich(_Scene(), 1e9) is True


def test_screen_busy_check_defaults_none():
    """未注入钩子时行为不变（不避让）。"""
    from core.perception.screen import ScreenPerception
    sp = ScreenPerception(interval=120)
    assert sp.busy_check is None
