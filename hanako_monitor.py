"""
Hanako 状态监控模块
轮询 hanako-state.json，将状态变化通过回调通知 PetWindow。
"""

import os
import json
import time
from pathlib import Path

# 状态文件路径（放在桌宠目录下，Hanako Agent 会在此写入）
STATE_FILE = Path(__file__).resolve().parent / "hanako-state.json"

# 状态 → 动画映射
STATE_TO_ANIM = {
    "listening": "idle",
    "thinking": "extra",
    "working": "extra",
}

# 超时：多久没有状态更新视为离线（秒）
STALE_TIMEOUT = 30


def read_state() -> dict | None:
    """读取状态文件，返回 None 表示文件不存在或损坏"""
    try:
        if not STATE_FILE.exists():
            return None
        raw = STATE_FILE.read_text(encoding="utf-8").strip()
        if not raw:
            return None
        return json.loads(raw)
    except (json.JSONDecodeError, OSError):
        return None


class HanakoMonitor:
    """
    轮询 Hanako 状态文件的监视器。
    通过 callback 通知 PetWindow 状态变化。
    """

    def __init__(self, on_state_change=None):
        """
        on_state_change(anim_name: str, message: str)
          anim_name: 要切换的动画序列名（'idle'/'extra'）
          message:   显示在气泡里的文字（空字符串则不显示）
        """
        self._on_state_change = on_state_change
        self._current_anim = "idle"
        self._last_state: dict | None = None
        self._last_update: float = 0

    def tick(self):
        """每次轮询调用（由 QTimer 驱动），检查状态变化"""
        state = read_state()
        now = time.time()

        # 没有状态文件 → idle
        if state is None:
            self._set_if_changed("idle", "")
            return

        # 首次读到有效状态 → 直接处理，不判超时
        if self._last_update == 0:
            self._last_state = state
            self._last_update = now
        else:
            # 状态过期 → 默认 idle
            if (now - self._last_update) > STALE_TIMEOUT:
                self._set_if_changed("idle", "")
                return

            # 状态相同 → 不触发回调，但更新计时
            if state == self._last_state:
                return

        self._last_state = state
        self._last_update = now

        s = state.get("state", "listening")
        tool = state.get("tool", "")
        msg = state.get("message", "")

        anim = STATE_TO_ANIM.get(s, "idle")

        # 构造气泡文字
        bubble = ""
        if s == "thinking":
            bubble = "正在思考…"
        elif s == "working":
            if tool:
                bubble = f"工作中 [{tool}]"
            else:
                bubble = "工作中…"
        elif msg:
            bubble = msg

        self._set_if_changed(anim, bubble)

    def _set_if_changed(self, anim: str, message: str):
        if anim != self._current_anim or message:
            self._current_anim = anim
            if self._on_state_change:
                self._on_state_change(anim, message)

    def force_idle(self):
        """强制回到 idle（退出时调用）"""
        self._last_state = None
        self._set_if_changed("idle", "")
