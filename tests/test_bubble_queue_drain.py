"""回归测试：气泡排队出队（2026-09-19 BugFix）。

背景
----
``pet.py::_clear_hanako_bubble`` 原实现在 ``while`` 循环体内 ``return``：

    while self._pending_bubbles:
        text, emotion, priority = self._pending_bubbles.pop(0)
        if text:
            self._show_bubble(text, emotion=emotion, priority=priority)
            return          # ← 每次只弹一条

而 ``_pending_bubbles`` 全仓只有这一个消费者，于是同时排入 3 条低优先级
通知时只有第 1 条会显示，另外两条要等下一次 ``hide_bubble`` 才轮到。

表现为「通知丢失」与「顺序错乱」。

修法不能是简单删掉 ``return``：``_show_bubble_impl`` 在气泡可见时会把
``_bubble_priority`` 抬到当前条的优先级，剩余低优先级条目随即被**重新入队**，
``while`` 永不结束 —— 死循环。

正确做法（现实现）：整体取出队列 → 清空 → 按优先级降序尝试显示 →
只要有一条真正上屏就停。

本测试用 stub 复现 PetWindow 的相关契约，不依赖 Qt，可在 CI 里跑。
"""
from __future__ import annotations

import importlib.util
import os
import sys
import types

import pytest


def _load_bubble_mixin_module():
    """直接加载 bubble_mixin.py，避免 import pet.py（后者依赖 PySide6/Qt）。"""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(here, "pet_mixins", "bubble_mixin.py")
    if not os.path.exists(path):
        pytest.skip(f"bubble_mixin.py 不存在: {path}")

    # 给 PySide6.QtCore.QThread 打桩：_show_bubble 用它做线程判定
    # ⚠ 必须用 monkeypatch 式的「用完即恢复」：直接 setdefault 会把假的
    # PySide6 永久留在 sys.modules 里，污染同批次的其他测试
    # （如 test_signal_contract 需要真的 PySide6.QtWidgets）。
    saved = {k: sys.modules.get(k) for k in ("PySide6", "PySide6.QtCore")}
    had = {k: (k in sys.modules) for k in saved}

    pyside = types.ModuleType("PySide6")
    pyside.__path__ = []          # 让它是个 package，避免掩盖真模块的子模块
    qtcore = types.ModuleType("PySide6.QtCore")

    class _QThread:
        _main = object()

        @classmethod
        def currentThread(cls):
            return cls._main

    qtcore.QThread = _QThread
    pyside.QtCore = qtcore
    sys.modules["PySide6"] = pyside
    sys.modules["PySide6.QtCore"] = qtcore

    try:
        spec = importlib.util.spec_from_file_location("_bubble_mixin_under_test", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        # 恢复原状：不管加载成功与否，都不能把假模块留在 sys.modules
        for k, was_present in had.items():
            if was_present:
                sys.modules[k] = saved[k]
            else:
                sys.modules.pop(k, None)


class _Bubble:
    """最小气泡 stub：只记录可见性与文本。"""
    def __init__(self):
        self.visible = False
        self.text = ""

    def set_text(self, t, bright=False):
        self.text = t

    def show(self):
        self.visible = True

    def hide_bubble(self):
        self.visible = False
        self.text = ""

    def isVisible(self):
        return self.visible

    def raise_(self):
        pass

    def geometry(self):
        raise RuntimeError("stub")

    def mapToGlobal(self, p):
        raise RuntimeError("stub")

    def windowOpacity(self):
        return 1.0

    def stackingOrder(self):
        return 0


class _Timer:
    def start(self, *_a, **_k):
        pass

    def stop(self):
        pass

    def isActive(self):
        return False


class _TtsPlayer:
    """最小播放器 stub：只记录起播路径。"""
    def __init__(self, owner):
        self._owner = owner

    def play(self, path):
        self._owner.played.append(path)

    def stop(self):
        pass

    def enqueue(self, path):
        self._owner.played.append(path)


class _PetStub:
    """复现 PetWindow 与气泡相关的契约（足量即可，不是完整实现）。"""
    def __init__(self):
        self.bubble = _Bubble()
        self._bubble_timer = _Timer()
        self._bubble_message = ""
        self._bubble_priority = 0
        self._pending_bubbles = []
        self._last_bubble_text = ""
        self._last_bubble_time = 0.0
        self._is_thinking = False
        self._pending_bubble_text = ""
        self._pending_bubble_emotion = ""
        self._last_tts_emotion = "neutral"
        self.config = {"tts": {"enabled": False}}
        self.shown = []          # 观测：真正上屏的文本序列
        self.played = []         # 观测：实际起播的音频
        self._reposition_bubble = lambda: None
        self._tts_player = _TtsPlayer(self)

    # ── 契约方法（与真实实现同形，但去掉 Qt 依赖）──
    def _show_bubble(self, text, emotion="neutral", priority=0, duration_ms=0, source=""):
        self._show_bubble_impl(text, emotion, priority, duration_ms, source)

    def _show_bubble_impl(self, text, emotion="neutral", priority=0, duration_ms=0, source=""):
        if not text:
            return
        self._is_thinking = False
        # 高优先级正在显示时，低优先级先排队（与真实实现一致）
        if self.bubble.isVisible() and self._bubble_priority > priority:
            self._pending_bubbles.append((text, emotion, priority))
            return
        self._bubble_message = text
        self._bubble_priority = priority
        self.bubble.set_text(text)
        self.bubble.show()
        self.shown.append(text)

    def _bubble_duration(self, text):
        return 10000


@pytest.fixture()
def pet():
    mod = _load_bubble_mixin_module()
    obj = _PetStub()
    # 只绑定被测的两个方法（其余用 stub 自带的）
    obj._clear_hanako_bubble_impl = types.MethodType(
        mod.BubbleMixin._clear_hanako_bubble_impl, obj)
    obj._deliver_reply = types.MethodType(mod.BubbleMixin._deliver_reply, obj)
    return obj


# ── 用例 1：核心回归 —— 排入 3 条，超时后不能只弹一条 ──

def test_pending_bubbles_are_not_starved(pet):
    """核心回归：3 条排队通知，两次超时后不能还卡着第一条之后不动。

    旧实现的病灶在**每轮只弹一条**：第 1 次超时弹 A、队列剩 [B, C]；
    第 2 次超时弹 B、剩 [C]。一次超时看不出问题（两条都只弹了一条），
    但连续两轮后差异就显出来了——旧实现会稳定地「一轮一条」慢慢放，
    而新实现按优先级重排，第 1 轮就会把最高优先级那条顶上来。

    这里用「优先级乱序入队 + 一次超时」来钉死它：
    旧实现弹的是**队首**（最早入队的），新实现弹的是**优先级最高的**。
    """
    pet._show_bubble("占位", priority=9)
    # 乱序入队：最早入队的优先级最低
    pet._show_bubble("最早入队_低优先级", priority=1)
    pet._show_bubble("中间入队_中优先级", priority=5)
    pet._show_bubble("最后入队_高优先级", priority=8)
    assert len(pet._pending_bubbles) == 3

    pet._clear_hanako_bubble_impl()

    # 新实现：优先级最高的先上屏
    assert pet.shown[-1] == "最后入队_高优先级", (
        f"应优先弹出高优先级，实际: {pet.shown}"
    )
    # 且队列已被整体取出并消费（不再原样滞留 3 条）
    assert len(pet._pending_bubbles) < 3, (
        f"排队通知未被消费：仍有 {len(pet._pending_bubbles)} 条滞留"
    )


def test_pending_bubbles_drain_over_successive_timeouts(pet):
    """连续超时：队列应持续被消费，而不是永远卡在同一条。

    这条直接对应线上症状「同时来 3 条通知，只看到第 1 条，后两条要等很久」。
    """
    pet._show_bubble("占位", priority=9)
    for i in range(3):
        pet._show_bubble(f"通知{i}", priority=1)
    assert len(pet._pending_bubbles) == 3

    for _ in range(3):
        pet._clear_hanako_bubble_impl()

    # 三轮超时后，3 条通知都应已被消费（队列清空）
    assert len(pet._pending_bubbles) == 0, (
        f"三轮超时后仍有滞留: {pet._pending_bubbles}"
    )


def test_clear_bubble_does_not_loop_forever(pet):
    """关键回归：不能因重入队而死循环（旧写法删掉 return 就会挂）。"""
    pet._show_bubble("占位", priority=5)
    for i in range(20):
        pet._show_bubble(f"低优先级{i}", priority=0)

    # 若实现有误，这里会无限循环（pytest 会超时）
    pet._clear_hanako_bubble_impl()
    assert True


def test_pending_bubble_priority_order(pet):
    """同批出队时，优先级高的应排在前面。"""
    pet._show_bubble("占位", priority=9)
    pet._show_bubble("低", priority=1)
    pet._show_bubble("高", priority=7)
    pet._show_bubble("中", priority=4)

    pet._clear_hanako_bubble_impl()
    # 上屏的应是「高」（优先级 7 最高）
    assert pet.shown[-1] == "高", f"实际显示: {pet.shown}"


def test_empty_pending_queue_is_safe(pet):
    """空队列不应报错。"""
    pet._clear_hanako_bubble_impl()
    assert pet._pending_bubbles == []


# ── 用例 2：_deliver_reply 契约 ──

def test_deliver_reply_shows_text_when_no_audio(pet):
    """无音频时文字必须立即上屏（不能吞掉）。"""
    pet._deliver_reply("你好", "happy", None)
    assert pet._bubble_message == "你好"
    assert pet.shown == ["你好"]


def test_deliver_reply_defers_when_audio_pending(pet, tmp_path):
    """有音频时文字暂存，等 TTS 开播再显示（避免重复气泡）。"""
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"x")
    pet.config = {"tts": {"enabled": True}}

    pet._deliver_reply("你好", "happy", str(audio))
    # 文字应被暂存而非立即上屏
    assert pet._pending_bubble_text == "你好"
    assert pet._pending_bubble_emotion == "happy"
    assert pet.shown == []


def test_deliver_reply_hides_thinking_bubble_on_empty(pet):
    """空回复要清掉「思考中」气泡。"""
    pet._show_bubble("思考中…", priority=0)
    assert pet.bubble.isVisible()

    pet._deliver_reply("", "neutral", None)
    # 只断言气泡已隐藏 + 暂存已清空。
    # 注意：hide_bubble() 不重置 _bubble_message（真实实现即如此，本次不改）
    assert not pet.bubble.isVisible()
    assert pet._pending_bubble_text == ""


def test_deliver_reply_shows_text_when_tts_disabled(pet, tmp_path):
    """TTS 关闭时，即使有音频路径也要显示文字。"""
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"x")
    pet.config = {"tts": {"enabled": False}}

    pet._deliver_reply("你好", "neutral", str(audio))
    assert pet.shown == ["你好"]
    assert pet._pending_bubble_text == ""
