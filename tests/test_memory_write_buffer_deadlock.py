# -*- coding: utf-8 -*-
"""MemoryWriteBuffer 死锁回归测试（2026-09-20 真实 bug）。

## 背景：一个只在收尾时才暴露的自死锁

`tests/test_emotion_classify_e2e.py` 收尾 `window.close()` 时挂住，
faulthandler 栈指向：

    closeEvent → companion_memory.close() → save()
      → MemoryWriteBuffer.mark_dirty() → _schedule_flush()  ← 卡在这

根因：`mark_dirty()` 持有 `_lock`（普通 Lock），再调 `_schedule_flush()`，
后者**再次获取同一把锁** → 同线程自死锁。

两条重入路径都中招：

    mark_dirty()        [持锁] → _schedule_flush()  [再取锁] ✗
    _periodic_flush()   [持锁] → _flush()           [再取锁] ✗

真机后果：**攒够 max_batch_size 次记忆写入后整个进程卡死**
（默认 batch=10，即连续对话约 10 次记忆写入就锁死）。

## 为什么原测试没发现

现有测试只调 `flush()` / `stop()`（单次取锁，不重入），
从不触发 `mark_dirty` 达到批量阈值那条路。

## 修法

`threading.Lock` → `threading.RLock`。重入是**有意设计**
（`_schedule_flush` 必须与调用方的状态检查原子），所以是换锁、不是拆锁。
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from core.memory_write_buffer import MemoryWriteBuffer  # noqa: E402


def _run_with_timeout(fn, timeout: float = 5.0) -> bool:
    """在子线程跑 fn；超时未完成返回 False（= 死锁）。"""
    done = []

    def _go():
        try:
            fn()
            done.append("ok")
        except Exception as exc:  # noqa: BLE001
            done.append(f"err:{exc}")

    t = threading.Thread(target=_go, daemon=True)
    t.start()
    t.join(timeout=timeout)
    return bool(done)


# ── 1. 死锁回归（核心）──


def test_mark_dirty_at_batch_threshold_does_not_deadlock():
    """★ 核心回归：`mark_dirty` 达到批量阈值必须返回，不能自死锁。

    修复前：batch=3，第 3 次调用永久挂住（实测）。
    """
    b = MemoryWriteBuffer(flush_interval=0.05, max_batch_size=3,
                          save_callback=lambda: None)

    def _go():
        for _ in range(5):  # 跨过阈值两次
            b.mark_dirty()

    assert _run_with_timeout(_go), (
        "mark_dirty 达到批量阈值时死锁——_schedule_flush 重入 _lock")


def test_periodic_flush_does_not_deadlock():
    """`_periodic_flush` 内层调 `_flush`，同线程重入也必须不锁。"""
    b = MemoryWriteBuffer(flush_interval=0.02, max_batch_size=100,
                          save_callback=lambda: None)
    b.start()
    try:
        b.mark_dirty()
        time.sleep(0.25)  # 让定期线程至少跑一轮
        # 定期线程若死锁，stop 里的 join 会超时——用 is_alive 侧面验证
        assert b._thread is not None and b._thread.is_alive(), (
            "定期 flush 线程死了（可能死锁后退出）")
    finally:
        assert _run_with_timeout(b.stop, timeout=6.0), "stop 死锁"


def test_lock_is_reentrant():
    """锁必须可重入——这是修法的本质，直接断言。"""
    b = MemoryWriteBuffer()
    acquired = []

    def _reenter():
        with b._lock:
            with b._lock:  # 重入
                acquired.append(True)

    assert _run_with_timeout(_reenter), "锁不可重入（应为 RLock）"
    assert acquired


# ── 2. 语义未变（换锁不能改行为）──


def test_mark_dirty_sets_flags():
    b = MemoryWriteBuffer(max_batch_size=100, save_callback=lambda: None)
    assert not b.is_dirty
    b.mark_dirty()
    assert b.is_dirty
    assert b.dirty_count == 1


def test_flush_calls_save_callback():
    calls = []
    b = MemoryWriteBuffer(max_batch_size=100, save_callback=lambda: calls.append(1))
    b.mark_dirty()
    b.flush()
    assert calls == [1]


def test_flush_clears_dirty():
    b = MemoryWriteBuffer(max_batch_size=100, save_callback=lambda: None)
    b.mark_dirty()
    b.flush()
    assert not b.is_dirty
    assert b.dirty_count == 0


def test_flush_noop_when_clean():
    calls = []
    b = MemoryWriteBuffer(max_batch_size=100, save_callback=lambda: calls.append(1))
    b.flush()  # 没脏过
    assert calls == []


def test_stop_flushes_pending():
    calls = []
    b = MemoryWriteBuffer(flush_interval=10.0, max_batch_size=100,
                          save_callback=lambda: calls.append(1))
    b.start()
    b.mark_dirty()
    b.stop()
    assert calls, "stop 应 flush 剩余数据"


def test_save_callback_exception_does_not_propagate():
    def boom():
        raise RuntimeError("disk full")

    b = MemoryWriteBuffer(max_batch_size=100, save_callback=boom)
    b.mark_dirty()
    b.flush()  # 不应抛异常


def test_batch_threshold_schedules_flush():
    """达到阈值应安排一次延迟 flush（不是立即同步保存）。"""
    calls = []
    b = MemoryWriteBuffer(flush_interval=0.05, max_batch_size=3,
                          save_callback=lambda: calls.append(1))
    for _ in range(3):
        b.mark_dirty()
    assert b._timer is not None, "达到阈值应安排 flush 定时器"
    time.sleep(0.3)
    assert calls, "延迟 flush 应真的执行"
