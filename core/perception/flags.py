"""桌宠权限开关 — 控制各感知模块启用/禁用

所有开关默认开启，用户可通过设置面板关闭。
关闭后对应模块降级或跳过。
"""
from __future__ import annotations

from datetime import datetime


class PetPermissions:
    """桌宠权限开关 — 控制各感知模块的启用/禁用

    所有开关默认开启，用户可通过设置面板关闭。
    关闭后对应模块降级或跳过。
    """

    def __init__(self):
        self.screenshot_enabled: bool = True       # 截图总开关
        self.diary_enabled: bool = True            # 日报总开关
        self.session_read_enabled: bool = True     # Session 读取总开关
        self.cross_session_enabled: bool = True    # 跨 Session 总开关
        self.tool_call_enabled: bool = True        # 插件工具调用总开关
        self.active_hours: tuple[int, int] = (6, 23)  # 活跃时段（默认 6:00-23:00）

    def is_in_active_hours(self) -> bool:
        """是否在活跃时段内"""
        hour = datetime.now().hour
        return self.active_hours[0] <= hour < self.active_hours[1]

    def to_dict(self) -> dict:
        return {
            "screenshot_enabled": self.screenshot_enabled,
            "diary_enabled": self.diary_enabled,
            "session_read_enabled": self.session_read_enabled,
            "cross_session_enabled": self.cross_session_enabled,
            "tool_call_enabled": self.tool_call_enabled,
            "active_hours": list(self.active_hours),
        }

    def load_from_dict(self, data: dict):
        """从配置加载"""
        for key in ('screenshot_enabled', 'diary_enabled', 'session_read_enabled',
                     'cross_session_enabled', 'tool_call_enabled'):
            if key in data:
                setattr(self, key, bool(data[key]))
        if 'active_hours' in data:
            ah = data['active_hours']
            if isinstance(ah, (list, tuple)) and len(ah) == 2:
                self.active_hours = (int(ah[0]), int(ah[1]))

    def get_status_text(self) -> str:
        """当前感知状态文本（展示给用户）"""
        parts = []
        parts.append("截图: " + ("✅" if self.screenshot_enabled else "❌"))
        parts.append("日报: " + ("✅" if self.diary_enabled else "❌"))
        parts.append("Session: " + ("✅" if self.session_read_enabled else "❌"))
        parts.append("跨Session: " + ("✅" if self.cross_session_enabled else "❌"))
        parts.append("工具调用: " + ("✅" if self.tool_call_enabled else "❌"))
        hour = "活跃时段" if self.is_in_active_hours() else "休息时段"
        parts.append(f"{hour} ({self.active_hours[0]}:00-{self.active_hours[1]}:00)")
        return " | ".join(parts)
