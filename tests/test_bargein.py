"""barge-in 测试（2026-09-19）：持续监听模式下用户插话即停 TTS。

背景：持续监听此前**完全不会打断 TTS**——按住说话有 barge-in
（`_toggle_voice`），持续监听整条路径零次 TTS 停止。本次补两个点：
  ① `_maybe_bargein()`：音频回调帧级判断，连续语音够长 → 停嘴
  ② `_do_continuous_asr`：ASR 完成、文本即将发送前 → 停嘴

覆盖：
  - 判据三要素（连续帧数 / 冷却 / 确实在说话）
  - 帧计数在静音与"未说话"时归零
  - 配置开关与下限保护
  - 音频线程约束：不碰 Qt（只 emit 信号）
  - 改动点 ② 的接线
"""
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pet_mixins import chat_mixin as cm  # noqa: E402


# ── 假实例 ─────────────────────────────────────────────


class _FakeRenderer:
    """渲染器 stub：只提供 _speaking（纯 Python 布尔）。"""

    def __init__(self, speaking: bool = False):
        self._speaking = speaking


class _FakeSignal:
    """信号 stub：记录 emit 次数。"""

    def __init__(self):
        self.emits = 0

    def emit(self, *a, **kw):
        self.emits += 1


class _FakeEngine:
    def __init__(self):
        self.interrupts: list[str] = []

    def interrupt(self, reason: str = ""):
        self.interrupts.append(reason)
        return "interrupted"


class _FakePet(cm.ChatMixin):
    """最小 stub：继承 ChatMixin（拿到真实 _maybe_bargein），
    只补齐 PetWindow 提供的鸭子类型外部依赖。"""

    def __init__(self, speaking: bool = False):
        self._renderer = _FakeRenderer(speaking)
        self.tts_stop_signal = _FakeSignal()
        self._engine = _FakeEngine()
        self._bargein_frames = 0
        self._bargein_last_emit = 0.0


@pytest.fixture(autouse=True)
def _reset_cfg_cache():
    """每个用例前后清空配置缓存（否则首个用例的读取结果会串味）。"""
    cm._bargein_cfg_cache = None
    yield
    cm._bargein_cfg_cache = None


def _speak_frames(pet, n: int):
    """投喂 n 帧“在说话”。"""
    for _ in range(n):
        pet._maybe_bargein()


# ── 判据 ①：连续帧数 ────────────────────────────────────


def test_bargein_fires_after_threshold_frames():
    """连续 15 帧（默认阈值）→ 恰好 emit 一次。"""
    pet = _FakePet(speaking=True)
    _speak_frames(pet, 15)
    assert pet.tts_stop_signal.emits == 1
    assert pet._engine.interrupts == ["voice_bargein"]


def test_bargein_does_not_fire_below_threshold():
    """14 帧（差一帧）→ 不 emit。"""
    pet = _FakePet(speaking=True)
    _speak_frames(pet, 14)
    assert pet.tts_stop_signal.emits == 0
    assert pet._engine.interrupts == []


def test_bargein_fires_only_once_within_cooldown():
    """冷却期内不重复 emit（默认 1.0s）。"""
    pet = _FakePet(speaking=True)
    _speak_frames(pet, 15)          # 第 1 次
    assert pet.tts_stop_signal.emits == 1
    _speak_frames(pet, 30)          # 冷却内再来 30 帧
    assert pet.tts_stop_signal.emits == 1, "冷却期内不应重复打断"


def test_bargein_fires_again_after_cooldown():
    """冷却过后可再次打断。"""
    pet = _FakePet(speaking=True)
    _speak_frames(pet, 15)
    assert pet.tts_stop_signal.emits == 1
    # 手动把上次打断时刻推回过去，模拟冷却已过
    pet._bargein_last_emit = time.monotonic() - 10.0
    _speak_frames(pet, 15)
    assert pet.tts_stop_signal.emits == 2


# ── 判据 ②：只在“桌宠正在说话”时打断 ───────────────────


def test_no_bargein_when_not_speaking():
    """桌宠没在说话 → 不打断（用户只是在自言自语/环境音）。"""
    pet = _FakePet(speaking=False)
    _speak_frames(pet, 50)
    assert pet.tts_stop_signal.emits == 0
    assert pet._engine.interrupts == []


def test_frame_counter_resets_when_not_speaking():
    """未说话时计数归零——避免“用户持续说话，桌宠一开口就被打断”。"""
    pet = _FakePet(speaking=False)
    _speak_frames(pet, 14)          # 攒了 14 帧但没在说话
    assert pet._bargein_frames == 0, "未说话时不应累计帧数"
    # 桌宠开始说话，只需再 15 帧（而非 1 帧）就触发
    pet._renderer._speaking = True
    _speak_frames(pet, 14)
    assert pet.tts_stop_signal.emits == 0
    _speak_frames(pet, 1)
    assert pet.tts_stop_signal.emits == 1


def test_renderer_missing_is_safe():
    """无渲染器（未加载/加载失败）→ 不打断、不抛异常。"""
    pet = _FakePet(speaking=True)
    pet._renderer = None
    _speak_frames(pet, 50)
    assert pet.tts_stop_signal.emits == 0


# ── 判据 ③：静音归零 ────────────────────────────────────


def test_silence_resets_counter():
    """静音一帧 → 计数归零（“连续语音”要求不间断）。"""
    pet = _FakePet(speaking=True)
    _speak_frames(pet, 10)
    assert pet._bargein_frames == 10
    pet._bargein_frames = 0         # 模拟 _on_voice_vad 的静音分支
    _speak_frames(pet, 10)
    assert pet.tts_stop_signal.emits == 0, "静音后应重新计数"


def test_short_pause_breaks_continuity():
    """★ 短停顿（未到 1.3s 句尾）也必须断连续——否则断续语音会被当成一次。

    回归保护：初版把重置只放在“无语音段”最内层分支，
    导致用户换气（短停顿）时计数被保留。
    """
    import inspect
    src = inspect.getsource(cm.ChatMixin._on_voice_vad)
    # 静音分支的第一条语句应是重置
    idx_else = src.find("self._bargein_frames = 0")
    idx_started = src.find("if self._voice_continuous_started:", idx_else)
    assert idx_else != -1, "静音分支必须重置计数"
    assert idx_started != -1
    assert idx_else < idx_started, "重置必须在该 else 的开头，不能嵌在“无语音段”里"


# ── 配置开关与下限保护 ─────────────────────────────────


def test_config_disabled_suppresses_bargein(monkeypatch):
    """asr.bargein.enabled=false → 任何帧数都不 emit。"""
    monkeypatch.setattr(cm, "_bargein_cfg_cache", (False, 15, 1.0))
    pet = _FakePet(speaking=True)
    _speak_frames(pet, 100)
    assert pet.tts_stop_signal.emits == 0


def test_config_frames_override(monkeypatch):
    """自定义帧数生效。"""
    monkeypatch.setattr(cm, "_bargein_cfg_cache", (True, 5, 0.0))
    pet = _FakePet(speaking=True)
    _speak_frames(pet, 5)
    assert pet.tts_stop_signal.emits == 1


def test_config_frames_lower_bound():
    """帧数下限保护：配 1 会被抬到 3（防视频人声误打断）。"""
    from config import load_config
    cm._bargein_cfg_cache = None
    # 直接验证解析逻辑：构造一个 frames=1 的配置
    class _Cfg(dict):
        pass
    monkeypatch_cfg = {"asr": {"bargein": {"enabled": True, "frames": 1}}}
    import config as config_mod
    orig = config_mod.load_config
    try:
        config_mod.load_config = lambda: monkeypatch_cfg
        enabled, frames, cooldown = cm._bargein_config()
        assert frames >= 3, "帧数下限应抬到 3"
        assert enabled is True
    finally:
        config_mod.load_config = orig
        cm._bargein_cfg_cache = None


def test_config_missing_uses_defaults():
    """配置缺失 → 回退默认（开启 + 15 帧 + 1.0s）。"""
    import config as config_mod
    orig = config_mod.load_config
    try:
        config_mod.load_config = lambda: {}
        cm._bargein_cfg_cache = None
        enabled, frames, cooldown = cm._bargein_config()
        assert enabled is True
        assert frames == cm._BARGEIN_FRAMES
        assert cooldown == cm._BARGEIN_COOLDOWN_S
    finally:
        config_mod.load_config = orig
        cm._bargein_cfg_cache = None


# ── 音频线程约束 ───────────────────────────────────────


def test_bargein_does_not_touch_qt_players():
    """★ barge-in 只 emit 信号，绝不直接调播放器（Qt/COM 跨线程会崩）。

    这是本改动最关键的约束：`_maybe_bargein` 跑在音频回调线程，
    而 `_tts_player.stop()` / `is_playing()` 都会碰 Qt 内部锁。

    注意：只看**可执行代码**——注释里会提到这些名字（解释为什么不能用）。
    """
    import inspect
    import re as _re
    from pet_mixins.chat_mixin import _strip_comments
    src = inspect.getsource(cm.ChatMixin._maybe_bargein)
    # 去空白，让 token 序列与源码写法可比对
    code = _re.sub(r"\s+", "", _strip_comments(src))
    assert "self.tts_stop_signal.emit()" in code, "必须经信号回主线程"
    assert "_tts_player" not in code, "不得直接引用 _tts_player"
    assert "_stream_player" not in code, "不得直接引用 _stream_player"
    assert "is_playing" not in code, "不得调 is_playing()（碰 Qt 内部锁）"


def test_bargein_reads_renderer_speaking_not_qt():
    """★ “是否在说话”取自 renderer._speaking（纯 Python 布尔）。"""
    import inspect
    src = inspect.getsource(cm.ChatMixin._maybe_bargein)
    assert "_speaking" in src


def test_bargein_config_is_cached():
    """★ 配置必须缓存：本函数每帧（约 32ms）调用，不能每帧读盘。"""
    import inspect
    src = inspect.getsource(cm._bargein_config)
    assert "_bargein_cfg_cache" in src, "必须有进程级缓存"
    # 验证缓存真的生效：第一次读取后改 load_config 也不影响结果
    import config as config_mod
    orig = config_mod.load_config
    try:
        config_mod.load_config = lambda: {"asr": {"bargein": {"frames": 7}}}
        cm._bargein_cfg_cache = None
        _, f1, _ = cm._bargein_config()
        config_mod.load_config = lambda: {"asr": {"bargein": {"frames": 99}}}
        _, f2, _ = cm._bargein_config()
        assert f1 == f2 == 7, "第二次读取应命中缓存"
    finally:
        config_mod.load_config = orig
        cm._bargein_cfg_cache = None


# ── 改动点 ②：ASR 完成后清理 ────────────────────────────


def test_continuous_asr_stops_tts_before_send():
    """★ ASR 完成、文本即将发送前必须停 TTS（与按住说话模式对齐）。

    否则指令已发出、桌宠还在说上一段 → 两个声音叠着播。
    """
    import inspect
    src = inspect.getsource(cm.ChatMixin._on_voice_vad)
    # 定位 _do_continuous_asr 闭包
    assert "_do_continuous_asr" in src
    # emit 必须出现在 eng.send 之前
    idx_emit = src.find("self.tts_stop_signal.emit()")
    idx_send = src.find("eng.send(text, character=self._current_char)")
    assert idx_emit != -1, "持续监听 ASR 后必须停 TTS"
    assert idx_send != -1, "发送调用应仍在"
    assert idx_emit < idx_send, "停 TTS 必须在发送文本之前"


def test_hold_to_talk_still_has_bargein():
    """按住说话的 barge-in 未被破坏（回归保护）。"""
    import inspect
    src = inspect.getsource(cm.ChatMixin._toggle_voice)
    assert 'interrupt(reason="voice_start")' in src
    assert "self._tts_player.stop()" in src
