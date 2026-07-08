"""无操作检测 + 关怀提醒 — 久坐提醒桌宠气泡。

用法:
    notifier = BreakNotifier(character_id="ophelia", idle_minutes=15)
    notifier.start()

    # 在 pet.py 的主循环中调用：
    if notifier.check():
        # notifier.on_remind 回调已被触发，气泡已显示
        pass
"""
from __future__ import annotations

import ctypes
import logging
import time
from ctypes import wintypes
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# ── Windows API ───────────────────────────────────────────

# GetLastInputInfo: 返回系统最后一次输入（键盘/鼠标）的 tick count
class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("dwTime", wintypes.DWORD),
    ]


def _get_idle_seconds() -> float:
    """返回自上次用户输入以来的秒数（系统级，跨进程）。

    使用 kernel32.GetTickCount 减去 user32.GetLastInputInfo。
    """
    lii = LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
        logger.warning("GetLastInputInfo failed")
        return 0.0

    current_tick = ctypes.windll.kernel32.GetTickCount()
    # dwTime 是 DWORD，会溢出回绕，但差值在 32-bit 范围内总是对的
    idle_ms = current_tick - lii.dwTime
    return idle_ms / 1000.0


# ── 关怀气泡文案 ──────────────────────────────────────────

# 不同超时档位的提醒文案（用角色口吻写）
REMINDER_MESSAGES: dict[str, list[str]] = {
    "light": [
        "还在嘛～",
        "嗯？不理我？",
        "你盯着屏幕好久啦～",
    ],
    "medium": [
        "喝口水吧，别干烧着。",
        "休息一下眼睛？看看窗外也行。",
        "起来走走嘛，我帮你看着。",
    ],
    "heavy": [
        "喂——！你还在吗？再不回来我可要自己出门了。",
        "好——久——了——（戳你",
        "……要不去躺会儿？我看着就好。",
    ],
}


# ── BreakNotifier ─────────────────────────────────────────

@dataclass
class BreakNotifier:
    """无操作检测器 — 检测用户闲置，触发关怀提醒。

    Attributes:
        character_id: 角色 ID（传递给回调）
        idle_minutes: 触发提醒的空闲分钟数（默认 15）
        gradual: 是否启用递进超时（15min→30min→60min 三段）
        cooldown_minutes: 提醒后冷却时间，避免频繁轰炸
    """

    character_id: str = "ophelia"
    idle_minutes: int = 15
    gradual: bool = True
    cooldown_minutes: int = 30

    # ── 内部状态 ──
    _enabled: bool = field(default=True, init=False)
    _last_remind_time: float = field(default=0.0, init=False)
    _stage: int = field(default=0, init=False)  # 0=无，1=light，2=medium，3=heavy

    def __post_init__(self):
        """回调 — 由外部设置，触发时调用 (stage: str, message: str)"""
        self.on_remind: callable = lambda stage, msg: None

    # ── 公开接口 ──

    def enable(self):
        self._enabled = True

    def disable(self):
        self._enabled = False

    def reset(self):
        """重置状态 — 用户有操作时调用"""
        self._stage = 0

    def check(self) -> str | None:
        """检查是否需要触发提醒。

        应该在桌宠主循环中周期性调用（如每 30 秒）。

        Returns:
            提醒文案（如果触发了），否则 None
        """
        if not self._enabled:
            return None

        idle_sec = _get_idle_seconds()

        if self.gradual:
            return self._check_gradual(idle_sec)
        else:
            return self._check_single(idle_sec)

    def _check_single(self, idle_sec: float) -> str | None:
        """单档模式：超过 idle_minutes 即触发"""
        if idle_sec < self.idle_minutes * 60:
            self._stage = 0
            return None

        # 检查冷却
        now = time.time()
        if now - self._last_remind_time < self.cooldown_minutes * 60:
            return None

        self._last_remind_time = now
        import random
        msg = random.choice(REMINDER_MESSAGES["medium"])
        self.on_remind("medium", msg)
        return msg

    def _check_gradual(self, idle_sec: float) -> str | None:
        """递进模式：15min → 30min → 60min 三段"""
        import random

        # 活动恢复 → 重置阶段
        if idle_sec < 120:  # 2 分钟内有操作 = 用户在
            self._stage = 0
            return None

        now = time.time()
        in_cooldown = (now - self._last_remind_time) < (self.cooldown_minutes * 60)

        # Stage 1: 15 分钟
        if idle_sec >= 15 * 60 and self._stage < 1:
            self._stage = 1
            if not in_cooldown:
                self._last_remind_time = now
                msg = random.choice(REMINDER_MESSAGES["light"])
                self.on_remind("light", msg)
                return msg

        # Stage 2: 30 分钟
        elif idle_sec >= 30 * 60 and self._stage < 2:
            self._stage = 2
            if not in_cooldown:
                self._last_remind_time = now
                msg = random.choice(REMINDER_MESSAGES["medium"])
                self.on_remind("medium", msg)
                return msg

        # Stage 3: 60 分钟
        elif idle_sec >= 60 * 60 and self._stage < 3:
            self._stage = 3
            if not in_cooldown:
                self._last_remind_time = now
                msg = random.choice(REMINDER_MESSAGES["heavy"])
                self.on_remind("heavy", msg)
                return msg

        return None


# ── 快速测试 ─────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    print(f"当前空闲: {_get_idle_seconds():.1f}s")
    print("等待 5 秒不操作来测试……")
    import time
    time.sleep(5)
    print(f"5s 后空闲: {_get_idle_seconds():.1f}s")

    notifier = BreakNotifier(idle_minutes=0.1)  # 6 秒即触发，用于测试
    notifier.on_remind = lambda stage, msg: print(f"[{stage}] {msg}")
    result = notifier.check()
    if result:
        print(f"触发提醒: {result}")
    else:
        print("未触发（你可能动了鼠标）")