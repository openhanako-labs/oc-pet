"""接线测试：ffmpeg 桥接 + TTS worker 输出可见性 + 感知降级可见性

覆盖 2026-09-10 任务 3 的 ③④⑤：
- core/ffmpeg_bridge.py：两处重复探测合并为单一幂等入口
- tts_provider/cosyvoice._pump_stderr：按来源判定而非关键词猜
- core/perception/controller：无日志 return 改为可见 + 去重
"""
from __future__ import annotations

import logging
import sys
import types

import pytest

from core import ffmpeg_bridge


# ══════════════════════════════════════════════════════════════
#  ④ ffmpeg 桥接
# ══════════════════════════════════════════════════════════════

@pytest.fixture(autouse=True)
def _reset_bridge():
    ffmpeg_bridge.reset_for_tests()
    yield
    ffmpeg_bridge.reset_for_tests()


def test_ensure_ffmpeg_is_idempotent(monkeypatch):
    """重复调用只探测一次——原实现两处 import 各自 setdefault，后者静默失效。"""
    calls = []
    fake = types.ModuleType("imageio_ffmpeg")

    def _get():
        calls.append(1)
        return sys.executable  # 任意存在的路径即可

    fake.get_ffmpeg_exe = _get
    monkeypatch.setitem(sys.modules, "imageio_ffmpeg", fake)

    first = ffmpeg_bridge.ensure_ffmpeg()
    second = ffmpeg_bridge.ensure_ffmpeg()

    assert first == second == sys.executable
    assert len(calls) == 1, "只应探测一次"


def test_ensure_ffmpeg_sets_env_even_if_already_set(monkeypatch):
    """原实现用 setdefault：若已被别人写入，本次结果被丢弃。现为直接赋值。"""
    fake = types.ModuleType("imageio_ffmpeg")
    fake.get_ffmpeg_exe = lambda: sys.executable
    monkeypatch.setitem(sys.modules, "imageio_ffmpeg", fake)
    monkeypatch.setenv("FFMPEG_BINARY", "旧值")

    ffmpeg_bridge.ensure_ffmpeg()

    import os
    assert os.environ["FFMPEG_BINARY"] == sys.executable


def test_failure_is_visible_and_queryable(monkeypatch, caplog):
    """探测失败原本 logger.debug（INFO 下不可见）→ 现在 warning + 可查询。"""
    fake = types.ModuleType("imageio_ffmpeg")

    def _boom():
        raise RuntimeError("找不到 ffmpeg 二进制")

    fake.get_ffmpeg_exe = _boom
    monkeypatch.setitem(sys.modules, "imageio_ffmpeg", fake)

    with caplog.at_level(logging.WARNING, logger="core.ffmpeg_bridge"):
        result = ffmpeg_bridge.ensure_ffmpeg()

    assert result is None
    assert "找不到 ffmpeg 二进制" in ffmpeg_bridge.last_error()
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1


def test_failure_is_not_retried_and_not_respammed(monkeypatch, caplog):
    fake = types.ModuleType("imageio_ffmpeg")
    calls = []

    def _boom():
        calls.append(1)
        raise RuntimeError("nope")

    fake.get_ffmpeg_exe = _boom
    monkeypatch.setitem(sys.modules, "imageio_ffmpeg", fake)

    with caplog.at_level(logging.WARNING, logger="core.ffmpeg_bridge"):
        for _ in range(5):
            ffmpeg_bridge.ensure_ffmpeg()

    assert len(calls) == 1, "失败也不该每轮重试"
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


def test_is_available_reflects_state(monkeypatch):
    fake = types.ModuleType("imageio_ffmpeg")
    fake.get_ffmpeg_exe = lambda: sys.executable
    monkeypatch.setitem(sys.modules, "imageio_ffmpeg", fake)

    assert ffmpeg_bridge.is_available() is True


# ══════════════════════════════════════════════════════════════
#  ③ TTS worker stderr 可见性
# ══════════════════════════════════════════════════════════════

class _FakeProc:
    def __init__(self, lines):
        self.stderr = iter(lines)


def _provider_with_stderr(lines):
    from tts_provider.cosyvoice import CosyVoiceProvider
    p = CosyVoiceProvider.__new__(CosyVoiceProvider)
    p._proc = _FakeProc(lines)
    return p


def test_worker_log_lines_are_promoted_to_info(caplog):
    """worker 自己的 _log 输出必须可见——不带关键词的失败描述也算。"""
    p = _provider_with_stderr([
        "[cosyvoice-worker] instruct2 produced no audio, falling back to zero_shot\n",
        "[cosyvoice-worker] zero-shot produced no audio, falling back to SFT\n",
    ])

    with caplog.at_level(logging.INFO, logger="tts_provider.cosyvoice"):
        p._pump_stderr()

    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert len(messages) == 2, "worker 输出一律可见"
    assert any("produced no audio" in m for m in messages)


def test_third_party_noise_stays_at_debug(caplog):
    p = _provider_with_stderr(["Some torch warning about deprecated API\n"])

    with caplog.at_level(logging.DEBUG, logger="tts_provider.cosyvoice"):
        p._pump_stderr()

    info = [r for r in caplog.records if r.levelno == logging.INFO]
    assert info == [], "第三方噪声不应提到 info"


def test_keyword_lines_still_promoted(caplog):
    p = _provider_with_stderr(["CUDA provider ready\n"])

    with caplog.at_level(logging.INFO, logger="tts_provider.cosyvoice"):
        p._pump_stderr()

    assert any("CUDA" in r.getMessage() for r in caplog.records)


def test_pump_breakage_is_visible(caplog):
    """stderr 停止排空 → worker 卡死，绝不能静默。"""
    class _Broken:
        @property
        def stderr(self):
            raise RuntimeError("pipe broken")

    from tts_provider.cosyvoice import CosyVoiceProvider
    p = CosyVoiceProvider.__new__(CosyVoiceProvider)
    p._proc = _Broken()

    with caplog.at_level(logging.WARNING, logger="tts_provider.cosyvoice"):
        p._pump_stderr()

    assert any(r.levelno == logging.WARNING for r in caplog.records)


# ══════════════════════════════════════════════════════════════
#  ⑤ 感知降级可见性
# ══════════════════════════════════════════════════════════════

def _controller():
    from core.perception.controller import PerceptionController
    return PerceptionController.__new__(PerceptionController)


def test_note_degraded_dedups(caplog):
    c = _controller()
    err = RuntimeError("HanakoContext 读取失败")

    with caplog.at_level(logging.WARNING, logger="core.perception.controller"):
        for _ in range(10):
            c._note_degraded("get_session_context", err)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, "同名只报一次，不随对话轮次刷屏"
    assert "get_session_context" in warnings[0].getMessage()


def test_note_degraded_reports_distinct_names(caplog):
    c = _controller()

    with caplog.at_level(logging.WARNING, logger="core.perception.controller"):
        c._note_degraded("a", RuntimeError("x"))
        c._note_degraded("b", RuntimeError("y"))

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2


def test_session_context_methods_do_not_swallow_silently(monkeypatch, caplog):
    """三个方法原本 `except Exception: return \"\"` 完全无日志。"""
    import core.hanako_context as hc

    class _Boom:
        def __init__(self, *a, **k):
            raise RuntimeError("上下文不可用")

    monkeypatch.setattr(hc, "HanakoContext", _Boom)

    c = _controller()
    c._character_id = "test"

    with caplog.at_level(logging.WARNING, logger="core.perception.controller"):
        assert c.get_session_context() == ""
        assert c.get_cross_session_context() == ""

    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "get_session_context" in messages
    assert "get_cross_session_context" in messages
