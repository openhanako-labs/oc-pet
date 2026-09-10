"""日志流加固 —— 防止「写日志」把整个应用冻死。

2026-09-10 事故（已用 py-spy 定位到确切栈）：

    HanaAgent.exe
      └─ cmd.exe /c start_pet.bat
           └─ python launcher.py     ← 卡在 log.critical（launcher.py:113）
                └─ python main.py    ← 卡在 stream.write（logging/__init__.py:1163）

`cmd /c` 的 stdout/stderr 是**管道且无人读取**。管道写满后：

  1. 最先写日志的线程阻塞在 StreamHandler.emit 的 `stream.write()`
  2. logging 有**全局锁**（logging/__init__.py:973），该线程持锁不放
  3. 所有其他线程（GUI 主线程、WS、感知、工具注册…）全部排队等锁
  4. **整个应用冻死**——连 launcher 的看门狗也卡在 `log.critical` 上，
     永远走不到 `child.kill()`，所以「自动复活」也失效

根因：`logging.basicConfig()` 默认挂一个 `StreamHandler(sys.stderr)`，
而它是**同步阻塞写 + 全局锁**。流不排空 = 应用冻死。

修法（双保险，两个入口各修一半）：

  A. **本模块**：stderr 若是管道/套接字（可能永久阻塞），摘掉它的 StreamHandler。
     文件 handler 已完整记录日志，且这种环境下控制台输出本来就没人看。
  B. **launcher.py `_watchdog_wait`**：先 kill 再写日志。
     看门狗的职责是恢复，不能被日志挡住。

两者独立成立：A 消除冻结，B 保证即使将来又出现别的阻塞源，看门狗仍能干活。
"""
from __future__ import annotations

import logging
import os
import stat
import sys
from typing import Optional

__all__ = ["stream_may_block_forever", "drop_blocking_stderr_handler"]


def stream_may_block_forever(stream) -> bool:
    """这个流是否可能在无人读取时永久阻塞？

    - 控制台（isatty）→ 否。写会被用户看见，不会无声地永远卡住。
    - 普通文件（S_ISREG）→ 否。文件写不会因「没人读」而填满。
    - 管道 / 套接字（S_ISFIFO / S_ISSOCK）→ **是**。缓冲区填满即阻塞。
    - 拿不到 fileno（如 StringIO）→ 保守判为「是」。
    """
    if stream is None:
        return True
    try:
        if stream.isatty():
            return False
    except Exception:
        pass
    try:
        mode = os.fstat(stream.fileno()).st_mode
    except Exception:
        # 没有真实 fd（内存流等）——无法判断，保守摘掉比冻死强
        return True
    return not stat.S_ISREG(mode)


def drop_blocking_stderr_handler(
    root: Optional[logging.Logger] = None,
    stderr=None,
) -> int:
    """摘掉 root 上指向「可能永久阻塞的 stderr」的 StreamHandler。

    前置条件：root 上必须已有非 StreamHandler 的处理器（即文件日志已挂上），
    否则摘掉之后日志就彻底没了 —— 那种情况下宁可留着 stderr。

    Args:
        root: 要处理的 logger，默认 root。
        stderr: 要检查的流，默认 `sys.stderr`。显式传入仅为可测（避免测试
                替换全局 sys.stderr 而干扰 pytest 的输出捕获）。

    Returns:
        摘掉的 handler 数量（0 = 未摘，环境安全或无从判断）。
    """
    root = root if root is not None else logging.getLogger()
    stderr = stderr if stderr is not None else sys.stderr

    # 文件日志未就位就不动，避免把日志彻底弄丢。
    # 只认「非纯净 StreamHandler」：FileHandler 是 StreamHandler 的**子类**，
    # 所以用 isinstance 会把文件处理器也当成流处理器，必须用 type(h) is。
    has_durable = any(type(h) is not logging.StreamHandler for h in root.handlers)
    if not has_durable:
        return 0

    stderr = stderr
    if not stream_may_block_forever(stderr):
        return 0

    removed = 0
    for h in list(root.handlers):
        if type(h) is logging.StreamHandler and getattr(h, "stream", None) is stderr:
            root.removeHandler(h)
            removed += 1
    if removed:
        # 摘掉控制台输出必须留痕——否则又是一个无声的降级。
        # 写回**被传入的那个 logger**（而非本模块的 logger）：durable handler
        # 就挂在它上面，这样这行才能真的落到文件里；写本模块 logger 会因
        # propagate 到 root 而丢失（当调用方传的不是 root 时）。
        # 此刻 stderr handler 已摘，这行只会走文件，不会阻塞。
        root.warning(
            "stderr 为管道/套接字且可能永久阻塞，已摘除 %d 个控制台日志处理器；"
            "完整日志见 logs/oc_pet.log（防止「写日志阻塞 → 全局锁被持有 → 应用冻死」）",
            removed,
        )
    return removed
