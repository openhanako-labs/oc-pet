"""栖息（站在窗口上）——O1-P4。

**为什么在 mixin 里而不是 pet.py**：项目有护栏测试
（``tests/test_signal_contract.py::test_pet_py_does_not_regrow``）要求 pet.py
不得继续膨胀（基线 3456 行，上限 3550）。我第一版把 148 行行为代码直接堆进
pet.py，被那条护栏抓了——**它抓得对**，所以搬到这里。pet.py 只留 2 行接线
（菜单项 + 每帧 tick 调用）。

取证结论（2026-09-19）：零件全都有——窗口矩形（``motion/window_target``）、
移动窗口（``self.move``）、每帧 tick 已在跑。但 miku 的 444×827 在 1080 屏上
**站不住普通窗口**（要踩住窗框，窗口顶边得在 830px 以下），所以会**临时缩小**，
下来时恢复。这不是取巧：Mate-Engine / Desktop Mate 的形象本来就小。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class PerchMixin:
    """让桌宠站到窗口上的行为（站哪儿在 ``motion/perch.py``，本类只管接线）。"""

    #: 栖息时最多缩到多小（再小就看不清了）
    PERCH_MIN_SCALE = 0.25

    def _toggle_perch(self, checked=False):
        """菜单入口：站上去 / 下来。"""
        if getattr(self, "_perch", None):
            self._stop_perch("手动下来")
        else:
            self._start_perch()

    def _start_perch(self):
        """站到当前前台窗口上。

        取证结论（2026-09-19）：零件全都有——窗口矩形（``window_target``）、
        移动窗口（``self.move``）、每帧 tick 已在跑。

        但 miku 的 444×827 在 1080 屏上**站不住普通窗口**（要踩住窗框，
        窗口顶边得在 830px 以下）。所以这里会**临时缩小**，下来时恢复——
        这不是取巧：Mate-Engine / Desktop Mate 的形象本来就小。
        """
        import time

        try:
            from motion import window_target as wt

            tgt = wt.foreground_target()
            if tgt is None:
                self._show_bubble("没找到能站的窗口。", emotion="thinking")
                return
            if tgt.get("own"):
                self._show_bubble("先切到你想让我站的窗口，再喊我～", emotion="thinking")
                return
            self._perch = {
                "hwnd": tgt["hwnd"],
                "title": (tgt.get("title") or "那个窗口")[:60],
                "started": time.monotonic(),
                "ttl": 0.0,
                "scale_before": float(getattr(self, "_pet_scale", 1.0) or 1.0),
                "pos_before": (int(self.x()), int(self.y())),
                "shrunk": False,
            }
            self._perch_shrink_to_fit(tgt["rect"])
            if not self._perch_step():
                reason = self._perch.get("_release") or "站不上去"
                self._stop_perch(reason, silent=False)
                return
            act = getattr(self, "_perch_action", None)
            if act is not None:
                act.setChecked(True)
            logger.info("栖息开始：站在「%s」上", self._perch["title"][:40])
        except Exception as e:
            logger.warning("栖息开始失败（非致命）: %s", e)
            self._perch = None

    def _perch_shrink_to_fit(self, rect):
        """临时缩小到"能站在这个窗口顶边之上"（不缩就站不住）。"""
        try:
            avail = int(rect[1]) - 14           # 窗口顶边上方可用高度
            base_h = int(getattr(self, "_base_h", 0) or 0)
            cur = float(getattr(self, "_pet_scale", 1.0) or 1.0)
            if base_h <= 0 or base_h * cur <= avail:
                return
            new_scale = max(self.PERCH_MIN_SCALE, min(cur, avail / float(base_h)))
            if new_scale >= cur - 0.01:
                return
            self._pet_scale = new_scale
            self._apply_scale()
            self._perch["shrunk"] = True
            logger.info("栖息：临时缩小到 %.2f（基准高 %d，可用 %d）",
                        new_scale, base_h, avail)
        except Exception as e:
            logger.debug("栖息缩放失败（忽略）: %s", e)

    def _perch_step(self) -> bool:
        """跟一帧；返回 False = 该下来了（原因写在 ``perch["_release"]``）。"""
        import time

        perch = getattr(self, "_perch", None)
        if not perch:
            return False
        try:
            from motion import window_target as wt
            from motion.perch import Rect, plan_perch, release_reason
        except Exception as e:
            logger.debug("栖息依赖不可用: %s", e)
            perch["_release"] = "栖息模块不可用"
            return False

        hwnd = perch["hwnd"]
        alive = wt.is_alive(hwnd)
        rect = wt.rect_of(hwnd) if alive else None
        # 注：**不做"用户拖动就下车"**——每帧都在移动它，拖动信号与我们的移动
        # 混在一起，判不准；可靠的下车方式是菜单或目标消失。
        reason = release_reason(
            target_seen=rect is not None,
            minimized=wt.is_minimized(hwnd) if alive else False,
            dragging=False,
            following_for=time.monotonic() - float(perch.get("started", 0.0)),
            ttl=float(perch.get("ttl", 0.0) or 0.0),
        )
        if reason:
            perch["_release"] = reason
            return False

        win = Rect(rect[0], rect[1], rect[0] + rect[2], rect[1] + rect[3])
        scr = wt.screen_rect()
        screen = Rect(scr[0], scr[1], scr[0] + scr[2], scr[1] + scr[3]) if scr else None
        plan = plan_perch(win, max(1, int(self.width())), max(1, int(self.height())),
                          screen=screen)
        if not plan.ok:
            perch["_release"] = plan.reason
            return False
        if (int(self.x()), int(self.y())) != plan.position():
            self.move(plan.x, plan.y)
        return True

    def _perch_tick(self):
        perch = getattr(self, "_perch", None)
        if not perch:
            return
        if not self._perch_step():
            self._stop_perch(perch.get("_release") or "换个地方")

    def _stop_perch(self, reason="", silent=True):
        """下来：恢复缩放与位置。"""
        perch = getattr(self, "_perch", None)
        if not perch:
            return
        self._perch = None
        try:
            before = float(perch.get("scale_before", 1.0) or 1.0)
            if abs(float(getattr(self, "_pet_scale", 1.0) or 1.0) - before) > 1e-6:
                self._pet_scale = before
                self._apply_scale()
            pos = perch.get("pos_before")
            if pos:
                self.move(int(pos[0]), int(pos[1]))
            act = getattr(self, "_perch_action", None)
            if act is not None:
                act.setChecked(False)
            logger.info("栖息结束：%s", reason or "下来")
            if not silent:
                self._show_bubble(f"站不上去：{reason}", emotion="thinking")
        except Exception as e:
            logger.warning("栖息结束失败（非致命）: %s", e)
