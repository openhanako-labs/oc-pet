"""主题系统 — 跟随系统时间切换 light/dark 主题

light = 6:00-18:00（基于 round-A 桌宠宇宙 token）
dark  = 18:00-6:00（基于 round-B 观星台 token）

启动时检测一次，每分钟轮询边界，到点自动切换。
主题切换通过 theme_changed 信号通知订阅者。
"""
from .theme_manager import ThemeManager, init_default, get_default, LIGHT_START, DARK_START

__all__ = ["ThemeManager", "init_default", "get_default", "LIGHT_START", "DARK_START"]