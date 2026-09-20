"""celebrating 入口判定收敛（2026-09-19）。

背景
----
``_do_celebrating`` 的入口判定原先**散在两处**，同一份逻辑做了两遍：

    _do_hanako_state  (旧 236-240)   5s 时间节流   ← 唯一调用者
    _do_celebrating   (旧 380-388)   并发锁 + 5s 时间节流（"兜底"）

而 ``_do_celebrating`` 全仓只有 ``_do_hanako_state`` 一个调用者，
所以那句「防御其他入口直调」的注释是过时假设 —— 第二处时间节流形同虚设。

三处叠一起的问题不只是重复：两条日志文案几乎一样
（「celebrating 节流：距离上次…」/「celebrating 节流：5s 内已触发」），
出问题时**说不清到底被谁拦了**。

现收敛为 ``_celebration_should_proceed()`` 一处。

本测试最重要的两条：
  * 并发锁与时间节流**不等价**，少任何一道都会漏（见 test_both_gates_needed_*）
  * 通过时**负责置位**（调用方只管复位）
"""
from __future__ import annotations

import importlib.util
import os
import sys
import types

import pytest


def _load_bubble_mixin():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(here, "pet_mixins", "bubble_mixin.py")
    if not os.path.exists(path):
        pytest.skip(f"bubble_mixin.py 不存在: {path}")

    # 用完即恢复：不能把假 PySide6 留在 sys.modules（会污染同批次测试）
    keys = ("PySide6", "PySide6.QtCore")
    saved = {k: sys.modules.get(k) for k in keys}
    had = {k: (k in sys.modules) for k in keys}

    pyside = types.ModuleType("PySide6")
    pyside.__path__ = []
    qtcore = types.ModuleType("PySide6.QtCore")

    class _QThread:
        _main = object()

        @classmethod
        def currentThread(cls):
            return cls._main

    class _QTimer:
        @staticmethod
        def singleShot(_ms, _cb):
            pass

    qtcore.QThread = _QThread
    qtcore.QTimer = _QTimer
    pyside.QtCore = qtcore
    sys.modules["PySide6"] = pyside
    sys.modules["PySide6.QtCore"] = qtcore

    try:
        spec = importlib.util.spec_from_file_location("_bubble_mixin_celeb", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        for k, was in had.items():
            if was:
                sys.modules[k] = saved[k]
            else:
                sys.modules.pop(k, None)


class _CelebStub:
    """只带 celebrating 判定所需的最小状态。"""
    def __init__(self):
        self._celebration_in_progress = False
        self._last_celebrating_at = 0.0

    _CELEBRATION_THROTTLE_SEC = 5.0
    _CELEBRATION_IN_PROGRESS_SEC = 3.0

    def _celebration_should_proceed(self):
        return _BubbleMixinMod.BubbleMixin._celebration_should_proceed(self)


_BubbleMixinMod = _load_bubble_mixin()


@pytest.fixture()
def stub():
    return _CelebStub()


# ── 基本行为 ──

def test_first_call_proceeds(stub):
    assert stub._celebration_should_proceed() is True


def test_proceed_sets_both_flags(stub):
    """通过时负责置位，调用方只管复位。"""
    stub._celebration_should_proceed()
    assert stub._celebration_in_progress is True
    assert stub._last_celebrating_at > 0


def test_second_call_immediately_blocked(stub):
    stub._celebration_should_proceed()
    assert stub._celebration_should_proceed() is False


# ── 核心：两道判定不等价，都必要 ──

def test_both_gates_needed_concurrent_lock_blocks(stub):
    """演完前（并发锁在）即使已过 5s 也要挡 —— 只留时间节流会漏这条。"""
    stub._celebration_should_proceed()
    # 并发锁仍在，但把时间推过节流窗口
    stub._last_celebrating_at = 0.0
    assert stub._celebration_should_proceed() is False, (
        "并发锁失效：演完前的重复触发会叠加多条庆祝序列"
    )


def test_both_gates_needed_throttle_blocks(stub):
    """演完了但太近 —— 只留并发锁会漏这条（用户看到连播两次撒花）。"""
    stub._celebration_should_proceed()
    # 演完了（锁已复位），但时间还在 5s 内
    stub._celebration_in_progress = False
    assert stub._celebration_should_proceed() is False, (
        "时间节流失效：演完立刻再来，用户会看到连续两次撒花"
    )


def test_proceeds_after_lock_released_and_window_passed(stub):
    """锁释放 + 超过 5s → 应放行（不能永久挡死）。"""
    stub._celebration_should_proceed()
    stub._celebration_in_progress = False          # 演完
    stub._last_celebrating_at -= 10.0              # 且已过 5s
    assert stub._celebration_should_proceed() is True


def test_throttle_boundary(stub):
    """边界：恰好等于窗口时应放行（< 才挡）。"""
    stub._celebration_should_proceed()
    stub._celebration_in_progress = False
    stub._last_celebrating_at -= 5.0
    assert stub._celebration_should_proceed() is True


def test_throttle_just_inside_window(stub):
    stub._celebration_should_proceed()
    stub._celebration_in_progress = False
    stub._last_celebrating_at -= 4.9
    assert stub._celebration_should_proceed() is False


# ── 收敛本身：不应再有两处 ──

def test_no_duplicate_throttle_in_do_hanako_state():
    """_do_hanako_state 里不应再有独立的时间节流（已收敛）。"""
    src = _load_bubble_mixin.__doc__  # 触发加载（模块已缓存在 sys.modules 之外）
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    text = open(os.path.join(here, "pet_mixins", "bubble_mixin.py"),
                encoding="utf-8").read()
    # 旧文案不应存在
    assert "celebrating 节流：距离上次" not in text, "第一处节流未删除"
    assert "celebrating 节流：5s 内已触发" not in text, "第三处节流未删除"
    # 新方法存在
    assert "_celebration_should_proceed" in text


def test_single_call_site_of_do_celebrating():
    """_do_celebrating 应只有 _do_hanako_state 一个调用者（原「防御其他入口」假设已过时）。"""
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    hits = 0
    for root, _dirs, files in os.walk(os.path.join(here, "pet_mixins")):
        for f in files:
            if not f.endswith(".py"):
                continue
            text = open(os.path.join(root, f), encoding="utf-8").read()
            hits += text.count("self._do_celebrating(")
    assert hits == 1, f"_do_celebrating 调用点应为 1，实际 {hits}"
