"""ffmpeg 可用性桥接 —— 单一入口，幂等，失败可见。

2026-09-10 抽取。此前 ``voice_input.py`` 与 ``asr_provider/whisper_local.py``
各有一份**逐字相同**的 import 期探测代码：

    try:
        import imageio_ffmpeg
        _ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        os.environ.setdefault('FFMPEG_BINARY', _ffmpeg)
        ...
    except Exception:
        logger.debug(...)          # ← 生产 INFO 下不可见

三个问题叠在一起：
  1. 模块导入瞬间执行副作用，并改写**进程级** PATH
  2. ``setdefault`` 使「谁先 import 谁决定 FFMPEG_BINARY」，后者静默失效
  3. 失败完全不可见 → ASR 无声地不可用

现在只有这一份实现，且失败会留下可见记录。
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Optional

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_attempted = False
_resolved: Optional[str] = None
_failed_reason = ""


def ensure_ffmpeg() -> Optional[str]:
    """确保 ffmpeg 可用，返回其可执行文件路径；不可用返回 None。

    幂等：重复调用只探测一次，不重复改写 PATH，也不与被调用方互相覆盖。
    失败可见：记一次 warning（INFO 生产级别下可见），原因由 :func:`last_error` 查询。

    返回 None 不代表致命 —— ASR 会降级，但降级这件事不该是无声的。
    """
    global _attempted, _resolved, _failed_reason
    if _attempted:
        return _resolved
    with _lock:
        if _attempted:
            return _resolved
        try:
            import imageio_ffmpeg
            exe = imageio_ffmpeg.get_ffmpeg_exe()
            if not exe:
                raise RuntimeError("imageio_ffmpeg.get_ffmpeg_exe() 返回空路径")
            # 直接赋值而非 setdefault：本模块是唯一写入方，结果不再取决于 import 顺序
            os.environ["FFMPEG_BINARY"] = exe
            exe_dir = os.path.dirname(exe)
            path = os.environ.get("PATH", "")
            if exe_dir and exe_dir not in path.split(os.pathsep):
                os.environ["PATH"] = exe_dir + os.pathsep + path
            _resolved = exe
            logger.info("ffmpeg 就绪: %s", exe)
        except Exception as e:
            _failed_reason = str(e)
            logger.warning(
                "ffmpeg 不可用（ASR 将无法解码音频，属降级非致命）: %s", e
            )
        finally:
            _attempted = True
    return _resolved


def last_error() -> str:
    """最近一次探测的失败原因（空串 = 未失败或尚未探测）。供诊断/自检查询。"""
    return _failed_reason


def is_available() -> bool:
    """ffmpeg 是否可用（首次调用会触发探测）。"""
    return ensure_ffmpeg() is not None


def reset_for_tests() -> None:
    """仅供测试：清空探测状态。生产代码不得调用。"""
    global _attempted, _resolved, _failed_reason
    with _lock:
        _attempted = False
        _resolved = None
        _failed_reason = ""
