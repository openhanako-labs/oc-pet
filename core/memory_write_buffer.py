"""记忆写入缓冲区 — 批量 flush + 异步落盘，避免阻塞对话

竞品参考（FaustBot）：批量 flush（别每条记忆都落盘）+ 异步落盘（不阻塞对话）。
验收：连续对话 100 轮无写入卡顿。

功能：
- 批量 flush：累积 N 次写入或 M 秒后统一落盘
- 异步落盘：写入在后台线程执行，不阻塞主线程
- 线程安全：使用**可重入锁**保护共享状态

## 为什么必须是 RLock（2026-09-20 真实死锁修复）

原实现用 `threading.Lock`（不可重入），但存在**同线程重入**路径：

    mark_dirty()            ← 持有 _lock
      └─ _schedule_flush()  ← 再次获取同一把 _lock → 永久阻塞

    _periodic_flush()       ← 持有 _lock
      └─ _flush()           ← 再次获取同一把 _lock → 永久阻塞

后果：**攒够 max_batch_size 次记忆写入后，整个进程卡死**。
实测复现：`mark_dirty()` 第 3 次（batch=3）即挂住；
真机表现是窗口关闭时卡在 `closeEvent → companion_memory.close()
→ mark_dirty`（faulthandler 栈可证）。

改 RLock 而不是拆锁：这些重入是**有意的**——
`_schedule_flush` 必须与调用方的状态检查原子；
拆开会让「检查 dirty → 安排 flush」出现竞态窗口。
RLock 保持原语义，只消除自锁。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

logger = logging.getLogger(__name__)


class MemoryWriteBuffer:
    """记忆写入缓冲区（批量 flush + 异步落盘）"""
    
    def __init__(
        self,
        flush_interval: float = 5.0,      # 最大间隔（秒）
        max_batch_size: int = 10,          # 最大批量大小
        save_callback: Callable[[], None] = None,  # 实际保存回调
    ):
        self._flush_interval = flush_interval
        self._max_batch_size = max_batch_size
        self._save_callback = save_callback
        
        self._dirty = False               # 是否有未保存的修改
        self._dirty_count = 0             # 未保存的修改次数
        self._last_flush_time = time.time()
        # RLock（可重入）：mark_dirty→_schedule_flush、_periodic_flush→_flush
        # 都是同线程重入路径，普通 Lock 会自死锁。详见模块 docstring。
        self._lock = threading.RLock()
        
        # 异步线程
        self._timer: threading.Timer | None = None
        self._thread: threading.Thread | None = None
        self._running = False
    
    def mark_dirty(self):
        """标记有未保存的修改"""
        with self._lock:
            self._dirty = True
            self._dirty_count += 1
            
            # 检查是否需要立即 flush
            if self._dirty_count >= self._max_batch_size:
                self._schedule_flush()
    
    def _schedule_flush(self):
        """安排一次 flush（延迟执行）"""
        with self._lock:
            if self._timer:
                self._timer.cancel()
                self._timer = None
            
            # 延迟 flush（给更多写入机会）
            self._timer = threading.Timer(
                self._flush_interval,
                self._flush,
                args=(True,),  # force=True
            )
            self._timer.daemon = True
            self._timer.start()
    
    def _flush(self, force: bool = False):
        """执行 flush（保存数据）"""
        with self._lock:
            if not self._dirty:
                return
            self._dirty = False
            self._dirty_count = 0
            self._last_flush_time = time.time()
        
        logger.debug("MemoryWriteBuffer flushing...")
        
        # 异步执行保存（避免阻塞）
        def _do_save():
            try:
                if self._save_callback:
                    self._save_callback()
                    logger.debug("MemoryWriteBuffer save completed")
            except Exception as e:
                logger.warning("MemoryWriteBuffer save failed: %s", e)
        
        if force:
            # force=True 时同步执行（如程序退出时）
            _do_save()
        else:
            # 异步执行
            t = threading.Thread(target=_do_save, daemon=True, name="memory-save")
            t.start()
    
    def flush(self):
        """立即 flush（同步执行）"""
        self._flush(force=True)
    
    def start(self):
        """启动定期 flush 线程"""
        self._running = True
        
        def _periodic_flush():
            while self._running:
                time.sleep(self._flush_interval)
                with self._lock:
                    if self._dirty and time.time() - self._last_flush_time >= self._flush_interval:
                        self._flush()
        
        self._thread = threading.Thread(
            target=_periodic_flush,
            daemon=True,
            name="memory-flush",
        )
        self._thread.start()
        logger.info("MemoryWriteBuffer started (interval=%.1fs, batch=%d)", 
                   self._flush_interval, self._max_batch_size)
    
    def stop(self):
        """停止缓冲区并 flush 剩余数据"""
        self._running = False
        
        if self._timer:
            self._timer.cancel()
            self._timer = None
        
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        
        # 最后 flush 一次
        self.flush()
        logger.info("MemoryWriteBuffer stopped")
    
    @property
    def is_dirty(self) -> bool:
        """是否有未保存的修改"""
        with self._lock:
            return self._dirty
    
    @property
    def dirty_count(self) -> int:
        """未保存的修改次数"""
        with self._lock:
            return self._dirty_count


# ════════════════════════════════════════════════════════════
#  全局缓冲区
# ════════════════════════════════════════════════════════════

_global_buffer: MemoryWriteBuffer | None = None


def get_write_buffer() -> MemoryWriteBuffer:
    """获取全局写入缓冲区（单例）"""
    global _global_buffer
    if _global_buffer is None:
        _global_buffer = MemoryWriteBuffer()
    return _global_buffer


# ════════════════════════════════════════════════════════════
#  导出
# ════════════════════════════════════════════════════════════

__all__ = [
    "MemoryWriteBuffer",
    "get_write_buffer",
]