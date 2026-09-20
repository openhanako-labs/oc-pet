# -*- coding: utf-8 -*-
"""情绪分类**真机端到端**测试（2026-09-20）。

## 为什么单独一个文件

`tests/test_emotion_classify_wiring.py` 全是桩级单测：直接改
`_pending_reply_emotion` 字段，再断言 `_resolved_reply_emotion()` 的返回值。
它们证明了「映射逻辑对」，但**证明不了「分类结果会真的走到渲染器」**——
而项目反复栽的正是这一类：单测全绿、功能是死的。

本文件补的是那一段：**真 PetWindow + 真信号槽 + 真 QThreadPool**，
跑完整条 `_classify_reply_async → 后台任务 → emit → _apply_classified_emotion
→ 渲染器状态真的变了`。上一轮翻车（后台任务不吐结果）恰好落在这一段里，
当时没有任何测试能发现。

## 断言的是什么

不是「函数被调用了」，而是「**渲染器的状态真的变了**」：
`set_master_emotion` 收到英文情绪词、`set_va_target` 收到连续 VAD、
`set_emotion_intensity` 收到强度、`window._current_emotion` 被更新。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("OC_DISABLE_TRAY", "1")
os.environ.setdefault("OC_DISABLE_PERCEPTION", "1")
os.environ.setdefault("OC_DISABLE_LIVE2D", "1")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from PySide6.QtCore import QThreadPool  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def app():
    inst = QApplication.instance()
    if inst is None:
        inst = QApplication([])
    return inst


class _SpyRenderer:
    """记录渲染器被喂了什么——断言「状态真的变了」，不是「函数被调了」。

    只实现 _apply_classified_emotion 会触碰的接口；
    其余属性由 PetWindow 在构造时已注入到真 renderer，这里整体替换掉。
    """

    def __init__(self):
        self.master_emotions: list[str] = []
        self.va_targets: list[tuple] = []
        self.intensities: list[float] = []
        self.expression_only: list[str] = []
        self._current_emotion = "neutral"
        # pet.py 构造后会读这些
        self._frames = []
        self._frame_tops = []
        self._anim_timer = None

    # ── _apply_classified_emotion / _sync_renderer_master_emotion 用到的 ──
    def set_master_emotion(self, emotion: str) -> None:
        self.master_emotions.append(emotion)
        self._current_emotion = emotion or "neutral"

    def set_va_target(self, v, a, hold_sec: float = 3.0) -> bool:
        self.va_targets.append((v, a, hold_sec))
        return True

    def set_emotion_intensity(self, intensity: float) -> bool:
        self.intensities.append(float(intensity))
        return True

    def set_emotion_expression_only(self, emotion: str) -> None:
        self.expression_only.append(emotion)

    # ── 构造期/ tick 期可能被摸到的空操作 ──
    def __getattr__(self, name):  # noqa: D105
        def _noop(*_a, **_k):
            return None
        return _noop


class _FakeResult:
    """ClassificationResult 的最小形状。"""

    def __init__(self, emotion, confidence=0.9, vad=(0.65, 0.10, 0.10),
                 intensity=0.8):
        self.emotion = emotion
        self.confidence = confidence
        self.vad = vad
        self.intensity = intensity

    def as_line(self) -> str:
        return f"{self.emotion}({self.confidence})"


@pytest.fixture()
def window(app):
    """真 PetWindow（离屏），渲染器换成 spy。"""
    from pet import PetWindow

    w = PetWindow(agent_id="miku")
    w._renderer = _SpyRenderer()
    yield w
    try:
        w.close()
    except Exception:
        pass


def _drain(app, ms: int = 3000) -> None:
    """等线程池跑完，并把队列里的信号投递到主线程。"""
    QThreadPool.globalInstance().waitForDone(ms)
    for _ in range(20):
        app.processEvents()


# ── 1. 慢路径：真信号 → 真槽 → 真渲染器 ──


def test_signal_emit_reaches_renderer(window, app):
    """★ 核心：`emotion_classified_signal.emit(...)` 必须真的驱动渲染器。

    这是慢路径的落点。上一轮「任务不吐结果」若发生在信号层，这条会红。
    """
    window._classify_enabled = True
    window._classify_min_conf = 0.40

    window.emotion_classified_signal.emit("happy", 0.9, (0.65, 0.10, 0.10), 0.8)
    app.processEvents()

    assert window._renderer.master_emotions, "渲染器没收到 master emotion"
    assert window._renderer.master_emotions[-1] == "happy"
    assert window._renderer.va_targets, "渲染器没收到连续 VAD"
    assert window._renderer.intensities, "渲染器没收到强度"
    assert window._current_emotion == "happy", "窗口情绪状态没更新"


def test_signal_maps_classifier_word_to_director_word(window, app):
    """分类器词（concerned）要先映射再喂渲染器（sad），不能原样透传。"""
    window._classify_enabled = True
    window._classify_min_conf = 0.40
    window.emotion_classified_signal.emit("concerned", 0.9, None, 0.5)
    app.processEvents()
    assert window._renderer.master_emotions[-1] == "sad"


def test_signal_ignores_low_confidence(window, app):
    """低置信不驱动——宁可不动，不能乱动。"""
    window._classify_enabled = True
    window._classify_min_conf = 0.40
    before = len(window._renderer.master_emotions)
    window.emotion_classified_signal.emit("happy", 0.10, None, 0.5)
    app.processEvents()
    assert len(window._renderer.master_emotions) == before


def test_signal_ignores_neutral(window, app):
    """neutral 不该被采纳——用一个空结论覆盖现有表情是错的。"""
    window._classify_enabled = True
    window._classify_min_conf = 0.40
    before = len(window._renderer.master_emotions)
    window.emotion_classified_signal.emit("neutral", 0.95, None, 0.5)
    app.processEvents()
    assert len(window._renderer.master_emotions) == before


def test_signal_dedups_rapid_repeat(window, app):
    """同一情绪短时间重复不重复驱动（去重窗口 2s）。"""
    window._classify_enabled = True
    window._classify_min_conf = 0.40
    window.emotion_classified_signal.emit("happy", 0.9, None, 0.8)
    app.processEvents()
    n1 = len(window._renderer.master_emotions)
    window.emotion_classified_signal.emit("happy", 0.9, None, 0.8)
    app.processEvents()
    assert len(window._renderer.master_emotions) == n1, "短时间重复应去重"


# ── 2. 全链路：真后台任务 → 信号 → 渲染器 ──


def test_async_task_end_to_end(window, app, monkeypatch):
    """★★ 最接近真机的回归：`_classify_reply_async` 提交真后台任务，
    任务跑完 emit 信号，主线程收到后驱动渲染器。

    这里把网络分类换成确定性的假结果（不打 API），但**线程池、信号、
    槽、渲染器全是真的**——上一轮翻车正是「任务提交了但没吐结果」，
    落在这一段。
    """
    window._classify_enabled = True
    window._classify_min_conf = 0.40
    # 关掉预热（真预热要打网络 1-3 分钟）
    monkeypatch.setattr(window, "_warmup_classifier_async", lambda: None)
    # 假分类：同步返回确定结果
    monkeypatch.setattr(window, "_classify_sync",
                        lambda text, view="pet": _FakeResult("excited"))

    window._classify_reply_async("我抢到票了！！")
    _drain(app)

    assert window._renderer.master_emotions, (
        "后台分类任务跑完没有驱动渲染器——这正是上一轮的症状")
    assert window._renderer.master_emotions[-1] == "happy", (
        "excited 应映射到 happy")
    assert window._renderer.va_targets, "连续 VAD 没写进渲染器"
    assert window._renderer.intensities, "强度没写进渲染器"
    assert window._current_emotion == "happy"


def test_async_task_survives_classifier_exception(window, app, monkeypatch):
    """分类器抛异常时任务不能崩，也不能驱动渲染器。"""
    window._classify_enabled = True
    monkeypatch.setattr(window, "_warmup_classifier_async", lambda: None)

    def boom(text, view="pet"):
        raise RuntimeError("provider down")

    monkeypatch.setattr(window, "_classify_sync", boom)
    before = len(window._renderer.master_emotions)
    window._classify_reply_async("随便说点什么")
    _drain(app)
    assert len(window._renderer.master_emotions) == before


def test_async_task_skipped_when_disabled(window, app, monkeypatch):
    """分类关闭时不应提交任务。"""
    window._classify_enabled = False
    called = []
    monkeypatch.setattr(window, "_classify_sync",
                        lambda text, view="pet": called.append(text))
    window._classify_reply_async("测试")
    _drain(app)
    assert not called


# ── 3. 快路径：结果已就绪时主线程立即取用 ──


def test_fast_path_reads_ready_result(window):
    """结果已就绪（上一轮缓存 / 分类够快）时，主线程直接取到。"""
    window._classify_enabled = True
    window._classify_min_conf = 0.40
    window._pending_reply_emotion = ("happy", 0.9)
    assert window._resolved_reply_emotion() == "happy"


def test_fast_path_consumed_after_use(window):
    """消费后应清空，防止下轮误用。"""
    window._classify_enabled = True
    window._classify_min_conf = 0.40
    window._pending_reply_emotion = ("happy", 0.9)
    window._resolved_reply_emotion()
    window._consume_reply_emotion()
    assert window._resolved_reply_emotion() == ""


# ── 4. 用户视角：分类结果写状态、不动表情 ──


def test_user_classification_updates_state_only(window, app, monkeypatch):
    """用户消息分类只写 `_last_user_emotion`，**不改桌宠表情**。

    设计意图：用户的情绪不该立刻改变桌宠的脸——那会让桌宠显得没有自我。
    """
    window._classify_enabled = True
    monkeypatch.setattr(window, "_classify_sync",
                        lambda text, view="user": _FakeResult("sad", 0.8))
    before = len(window._renderer.master_emotions)
    window._classify_user_async("我今天有点难过")
    _drain(app)
    assert window._last_user_emotion == "sad"
    assert len(window._renderer.master_emotions) == before, (
        "用户情绪不该驱动桌宠表情")
