"""窗口目标：给"栖息"提供真实窗口的 hwnd / 矩形 / 是否最小化。

**为什么单独一个模块**：``motion/perch.py`` 要尽量保持**纯计算**（能被完整单测），
所以 Win32 这一层单独放这里。复用 ``foreground_watcher`` 的同款做法：
``ctypes.windll.user32`` 直调，**不依赖 pywin32**。

只读：本模块**从不移动/修改别人的窗口**，只查询。
"""
from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes
from typing import Optional

logger = logging.getLogger(__name__)

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

SW_RESTORE = 9


def own_pid() -> int:
    try:
        return int(kernel32.GetCurrentProcessId())
    except Exception:  # noqa: BLE001
        return -1


def pid_of_window(hwnd: int) -> int:
    try:
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
        return int(pid.value)
    except Exception:  # noqa: BLE001
        return -1


def rect_of(hwnd: int) -> Optional[tuple]:
    """窗口矩形 ``(x, y, w, h)``；查不到返回 ``None``（**不假装成功**）。"""
    try:
        rect = wintypes.RECT()
        if not user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
            return None
        w = rect.right - rect.left
        h = rect.bottom - rect.top
        if w <= 0 or h <= 0:
            return None
        return (rect.left, rect.top, w, h)
    except Exception as e:  # noqa: BLE001
        logger.debug("rect_of 失败 hwnd=%s: %s", hwnd, e)
        return None


def is_minimized(hwnd: int) -> bool:
    try:
        return bool(user32.IsIconic(wintypes.HWND(hwnd)))
    except Exception:  # noqa: BLE001
        return False


def is_alive(hwnd: int) -> bool:
    try:
        return bool(user32.IsWindow(wintypes.HWND(hwnd)))
    except Exception:  # noqa: BLE001
        return False


def title_of(hwnd: int) -> str:
    try:
        length = user32.GetWindowTextLengthW(wintypes.HWND(hwnd))
        if length <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(wintypes.HWND(hwnd), buf, length + 1)
        return buf.value or ""
    except Exception:  # noqa: BLE001
        return ""


def foreground_target() -> Optional[dict]:
    """当前前台窗口。返回 ``None`` = 没找到（含查询失败）。

    ``own`` 为 True 表示前台就是桌宠自己——那不能站（会把桌宠自己当沙发）。
    """
    try:
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        rect = rect_of(hwnd)
        if rect is None:
            return None
        pid = pid_of_window(hwnd)
        return {
            "hwnd": int(hwnd),
            "rect": rect,
            "title": title_of(hwnd),
            "pid": pid,
            "own": pid == own_pid(),
        }
    except Exception as e:  # noqa: BLE001
        logger.debug("foreground_target 失败: %s", e)
        return None


def screen_rect() -> Optional[tuple]:
    """主屏可用区域 ``(x, y, w, h)``（不含任务栏）；失败返回 None。"""
    try:
        rect = wintypes.RECT()
        SPI_GETWORKAREA = 0x0030
        if not user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rect), 0):
            return None
        return (rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)
    except Exception:  # noqa: BLE001
        return None
