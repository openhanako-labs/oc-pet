"""栖息（Perch）：让桌宠**站在某个窗口上**。

## 为什么现在能做了（先取证，不凭感觉）

Mate-Engine 的"站在窗口/任务栏上"看着玄，其实零件 oc-pet 早就都有了：

===========================  ==================================================
需要什么                       oc-pet 现状
===========================  ==================================================
窗口枚举                      ``motion/foreground_watcher.list_visible_windows()``
                             （**ctypes 直调 Win32，不依赖 pywin32**）
窗口矩形                      ``_get_foreground_window_rect()`` → (x, y, w, h) 已有
移动宠物窗口                  ``self.move()``，且**每帧 tick 已在跑**
                             （宠物现在就会追鼠标：``_chasing``/``_update_chase``）
===========================  ==================================================

缺的只是"一个栖息行为"。本模块负责其中**纯计算**的两件事：

1. **在哪站** —— 把宠物**底部**贴到目标窗口**顶边**（脚踩在窗框上），
   上方没空间时退到窗口**下沿**；横向居中并夹进屏幕内。
2. **什么时候该走** —— 目标没了/最小化/被拖走/超时。

线程/定时器/右键菜单都不在这里（接线是下一步），所以它能被完整单测。
"""
from __future__ import annotations

from dataclasses import dataclass

#: 宠物底部与窗框之间的留白
DEFAULT_MARGIN_PX = 6
#: 太小的窗口不值得站（也避免挡住按钮）
MIN_WINDOW_W = 220
MIN_WINDOW_H = 140

#: 退让原因（"" = 继续待着）
REASON_TARGET_GONE = "目标窗口已关闭"
REASON_MINIMIZED = "目标窗口最小化"
REASON_DRAGGING = "宠物被拖动"
REASON_TIMEOUT = "站够久了"


@dataclass(frozen=True)
class Rect:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    def is_valid(self) -> bool:
        return self.width > 0 and self.height > 0


@dataclass(frozen=True)
class PerchPlan:
    """站哪儿。``ok=False`` 时看 ``reason``。"""

    ok: bool
    x: int = 0
    y: int = 0
    reason: str = "ok"
    on_edge: str = ""        # "top"（踩在窗顶） / "bottom"（退到窗下沿）

    def position(self) -> tuple:
        return (self.x, self.y)


def _clamp(v: int, lo: int, hi: int) -> int:
    if hi < lo:                # 屏幕比宠物还窄这种荒唐情况：别返回负数
        return lo
    return max(lo, min(v, hi))


def plan_perch(window: Rect | None,
               pet_w: int,
               pet_h: int,
               *,
               screen: Rect | None = None,
               margin: int = DEFAULT_MARGIN_PX,
               min_window_w: int = MIN_WINDOW_W,
               min_window_h: int = MIN_WINDOW_H) -> PerchPlan:
    """算出宠物应该出现在哪个坐标（窗口左上角）。

    Args:
        window: 目标窗口矩形（None = 没目标）。
        pet_w / pet_h: 宠物窗口尺寸。
        screen: 可用屏幕矩形（None = 不夹取，只保证非负）。
        margin: 宠物脚底与窗框之间的留白。
    """
    pet_w = max(1, int(pet_w))
    pet_h = max(1, int(pet_h))
    margin = max(0, int(margin))

    if window is None or not window.is_valid():
        return PerchPlan(False, reason="没有可站的目标窗口")
    if window.width < min_window_w or window.height < min_window_h:
        return PerchPlan(False, reason="目标窗口太小，站上去会挡事")

    # 全屏窗口（铺满屏幕）没有"窗框"可站
    if screen is not None and screen.is_valid():
        if (window.left <= screen.left and window.top <= screen.top
                and window.right >= screen.right and window.bottom >= screen.bottom):
            return PerchPlan(False, reason="目标窗口是全屏，没有地方站")

    # 可用的落脚范围（没给屏幕就不夹取，只保证非负）
    if screen is not None and screen.is_valid():
        lo_x, hi_x = screen.left, screen.right - pet_w
        lo_y, hi_y = screen.top, screen.bottom - pet_h
    else:
        lo_x, hi_x = 0, 10 ** 9
        lo_y, hi_y = 0, 10 ** 9

    x = _clamp(window.left + (window.width - pet_w) // 2, lo_x, hi_x)

    # 优先踩在窗顶（脚落在窗框上方）；上方没空间就退到窗下沿
    y_top = window.top - pet_h - margin
    if y_top >= lo_y:
        return PerchPlan(True, x=x, y=y_top, on_edge="top")

    y_bottom = window.bottom + margin
    if y_bottom <= hi_y:
        return PerchPlan(True, x=x, y=y_bottom, on_edge="bottom")

    return PerchPlan(False, reason="这个窗口上方和下方都没有落脚的地方")


def release_reason(*,
                   target_seen: bool = True,
                   minimized: bool = False,
                   dragging: bool = False,
                   following_for: float = 0.0,
                   ttl: float = 0.0) -> str:
    """该不该从窗口上下来；返回 ``""`` 表示继续待着。

    顺序有意：**用户主动碰宠物 > 目标消失 > 状态变化 > 超时**。
    被拖动优先，是因为那是人的意图，其余都只是环境变化。
    """
    if dragging:
        return REASON_DRAGGING
    if not target_seen:
        return REASON_TARGET_GONE
    if minimized:
        return REASON_MINIMIZED
    if ttl > 0 and following_for >= ttl:
        return REASON_TIMEOUT
    return ""
