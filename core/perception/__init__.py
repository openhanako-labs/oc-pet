"""感知系统 — 感知能力包

统一入口 PerceptionController，对外暴露：
  - build_context()  -> 注入 LLM prompt 的感知上下文
  - tick()           -> 每 30 秒调用，驱动情绪衰减 + 屏幕分析 + 主动对话
  - trigger_emotion() -> 触发情绪状态
  - get_screen_context() -> 屏幕感知结果
  - check_proactive()   -> 主动对话触发检查
  - generate_daily_diary() -> 日报 Markdown 生成

模块拆分（原 perception.py 1032 行 → 包）：
  - time.py          — TimePerception
  - emotion.py       — EmotionStateMachine
  - schedule.py      — SchedulePerception
  - flags.py         — PetPermissions
  - screen_types.py  — ScreenEvent + ActivityEvent
  - screen.py        — ScreenPerception + 黑名单 helpers + 视觉相关常量
  - proactive.py     — ProactiveScheduler + DEFAULT_RULES
  - controller.py    — PerceptionController

向后兼容：
  - from core.perception import PerceptionController, ProactiveScheduler
  - from core.perception import ActivityEvent  ← ui/activity_feed.py 用
  - from core.perception import PetPermissions  ← 其他模块可能用
  - 所有原来能 import 的公开类都可继续 import
"""
from __future__ import annotations

# ── 时段感知 ──
from .time import TimePerception

# ── 情绪状态机 ──
from .emotion import EmotionStateMachine

# ── 日程感知 ──
from .schedule import SchedulePerception

# ── 权限开关 ──
from .flags import PetPermissions

# ── 屏幕感知（数据类型 + 主类） ──
from .screen_types import ScreenEvent, ActivityEvent
from .screen import (
    ScreenPerception,
    SCREENSHOT_SCALE,
    JPEG_QUALITY,
    VISION_PROMPT,
    SCREEN_EMOTION_MAP,
    SCREENSHOT_PROCESS_BLACKLIST,
    SCREENSHOT_TITLE_BLACKLIST,
    _is_screen_blacklisted,
)

# ── 主动对话调度 ──
from .proactive import ProactiveScheduler, DEFAULT_RULES

# ── 统一控制器 ──
from .controller import PerceptionController


__all__ = [
    # 时段
    "TimePerception",
    # 情绪
    "EmotionStateMachine",
    # 日程
    "SchedulePerception",
    # 权限
    "PetPermissions",
    # 屏幕感知
    "ScreenEvent",
    "ActivityEvent",
    "ScreenPerception",
    # 屏幕常量 / 帮助
    "SCREENSHOT_SCALE",
    "JPEG_QUALITY",
    "VISION_PROMPT",
    "SCREEN_EMOTION_MAP",
    "SCREENSHOT_PROCESS_BLACKLIST",
    "SCREENSHOT_TITLE_BLACKLIST",
    # 主动对话
    "ProactiveScheduler",
    "DEFAULT_RULES",
    # 统一入口
    "PerceptionController",
]
