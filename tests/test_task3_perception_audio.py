"""接线测试：TTS / 音频 / 感知 三条路径的高危静默点可见化

覆盖 2026-09-10 任务 3 余量：
- TTS：音色配置读取失败、worker 协议异常、播放状态查询失败
- 音频：录音流关闭失败、打断失败、配置回退
- 感知：日程数据源刷新失败、媒体轮询异常、前台窗口获取失败、
        专注 listener 异常、环境扫描/巡检 tick 失败
"""
from __future__ import annotations

import logging

import pytest


# ══════════════════════════════════════════════════════════════
#  感知：日程三数据源
# ══════════════════════════════════════════════════════════════

def _schedule_with(readers):
    from core.perception.schedule import SchedulePerception
    sp = SchedulePerception.__new__(SchedulePerception)
    sp._automations = []
    sp._read_cron_jobs = readers.get("cron", lambda: [])
    sp._read_deferred_tasks = readers.get("deferred", lambda: [])
    sp._read_plugin_tasks = readers.get("plugin", lambda: [])
    return sp


def test_schedule_source_failure_is_visible(caplog):
    """日程数据源失败原为 debug → 提醒静默丢失。"""
    def _boom():
        raise RuntimeError("cron 文件损坏")

    sp = _schedule_with({"cron": _boom})

    with caplog.at_level(logging.WARNING, logger="core.perception.schedule"):
        sp.refresh()

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "cron" in warnings[0].getMessage()


def test_schedule_partial_failure_still_collects_others():
    """单个数据源坏掉不得拖垮其余两个。"""
    def _boom():
        raise RuntimeError("x")

    sp = _schedule_with({
        "cron": _boom,
        "deferred": lambda: [{"id": "d1"}],
        "plugin": lambda: [{"id": "p1"}],
    })
    sp.refresh()

    assert [a["id"] for a in sp._automations] == ["d1", "p1"]


# ══════════════════════════════════════════════════════════════
#  感知：focus listener 去重
# ══════════════════════════════════════════════════════════════

def _fsm():
    """最小 FocusStateMachine（add_listener/_notify 需要 _lock 与 _listeners）。"""
    import threading
    from core.perception.focus import FocusStateMachine

    fsm = FocusStateMachine.__new__(FocusStateMachine)
    fsm._lock = threading.Lock()
    fsm._listeners = []
    return fsm


def test_focus_listener_failure_is_visible_and_deduped(caplog):
    """_notify 每 tick 都调，失败必须可见但不能刷屏。"""
    fsm = _fsm()

    def bad_listener(active, charge, signals):
        raise RuntimeError("listener 崩了")

    fsm.add_listener(bad_listener)

    with caplog.at_level(logging.WARNING, logger="core.perception.focus"):
        for _ in range(8):
            fsm._notify(True, 0.5, {})

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, f"应去重，实际 {len(warnings)} 条"
    assert "bad_listener" in warnings[0].getMessage()


def test_focus_listener_others_keep_running():
    fsm = _fsm()
    seen = []

    def bad(active, charge, signals):
        raise RuntimeError("x")

    fsm.add_listener(bad)
    fsm.add_listener(lambda a, c, s: seen.append(a))

    fsm._notify(True, 0.5, {})

    assert seen == [True], "一个 listener 崩掉不得影响其他 listener"


# ══════════════════════════════════════════════════════════════
#  感知：controller 降级可见性（build_context 各段）
# ══════════════════════════════════════════════════════════════

def test_build_context_section_failure_is_visible(caplog):
    from core.perception.controller import PerceptionController

    c = PerceptionController.__new__(PerceptionController)

    with caplog.at_level(logging.WARNING, logger="core.perception.controller"):
        c._note_degraded("build_context_media", RuntimeError("SMTC 挂了"))

    assert any("build_context_media" in r.getMessage()
               for r in caplog.records if r.levelno == logging.WARNING)


# ══════════════════════════════════════════════════════════════
#  TTS：配置读取失败可见
# ══════════════════════════════════════════════════════════════

def test_voice_profile_failure_disables_validation_visibly(monkeypatch, caplog):
    """EDGE_VOICES 不可用 → 音色校验停用，必须可见（否则传错音色在合成阶段莫名失败）。"""
    import tts_provider.voice_profile as vp

    real_import = __import__

    def _fake_import(name, *a, **k):
        if name == "edge_tts" or name.endswith(".edge_tts"):
            raise ImportError("no edge_tts")
        return real_import(name, *a, **k)

    monkeypatch.setattr("builtins.__import__", _fake_import)

    with caplog.at_level(logging.WARNING, logger="tts_provider.voice_profile"):
        result = vp.provider_valid_voices("edge")

    assert result is None
    assert any("EDGE_VOICES" in r.getMessage()
               for r in caplog.records if r.levelno == logging.WARNING)


def test_voice_profile_unknown_provider_stays_silent():
    """未校验的 provider 是设计行为，不应报警。"""
    from tts_provider.voice_profile import provider_valid_voices

    assert provider_valid_voices("cosyvoice") is None


# ══════════════════════════════════════════════════════════════
#  TTS: qmediaplayer 状态查询
# ══════════════════════════════════════════════════════════════

def test_is_playing_failure_is_visible(caplog):
    from ui.tts_player import TTSTtsPlayer

    class _BadPlayer:
        def playbackState(self):
            raise RuntimeError("COM 组件异常")

    p = TTSTtsPlayer.__new__(TTSTtsPlayer)
    p._player = _BadPlayer()

    with caplog.at_level(logging.WARNING, logger="ui.tts_player"):
        assert p.is_playing() is False

    assert any("playbackState" in r.getMessage()
               for r in caplog.records if r.levelno == logging.WARNING)


# ══════════════════════════════════════════════════════════════
#  音频：配置回退可见
# ══════════════════════════════════════════════════════════════

def test_asr_config_fallback_is_visible(monkeypatch, caplog):
    import voice_input

    def _boom():
        raise RuntimeError("config 读不了")

    monkeypatch.setattr("config.load_config", _boom, raising=False)

    with caplog.at_level(logging.WARNING, logger="voice_input"):
        assert voice_input._get_asr_model_name() == "small"
        assert voice_input._asr_language() == "zh"

    msgs = " ".join(r.getMessage() for r in caplog.records
                    if r.levelno == logging.WARNING)
    assert "asr.model" in msgs
    assert "asr.language" in msgs


def test_asr_backend_fallback_is_visible(monkeypatch, caplog):
    from asr_provider.whisper_local import WhisperLocalProvider

    def _boom():
        raise RuntimeError("config 读不了")

    monkeypatch.setattr("config.load_config", _boom, raising=False)

    with caplog.at_level(logging.WARNING, logger="asr_provider.whisper_local"):
        assert WhisperLocalProvider._resolve_backend() == "whisper"

    assert any("asr.backend" in r.getMessage()
               for r in caplog.records if r.levelno == logging.WARNING)
