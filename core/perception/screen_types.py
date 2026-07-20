"""屏幕感知数据结构

两类：
- ScreenEvent：单次截屏的结构化元数据（应用名/窗口标题/时间戳/触发模式）
- ActivityEvent：从视觉模型 JSON 解析出的活动事件（含分类、置信度、起止时间）

两者均使用 __slots__，频繁创建时节省内存。
"""
from __future__ import annotations


class ScreenEvent:
    """一次屏幕感知的结构化数据"""
    __slots__ = ('app', 'title', 'timestamp', 'mode', 'description')

    def __init__(self, app: str = "", title: str = "", timestamp: float = 0.0,
                 mode: str = "timer", description: str = ""):
        self.app = app              # 进程名（如 Obsidian.exe）
        self.title = title          # 窗口标题
        self.timestamp = timestamp  # time.time()
        self.mode = mode            # "timer" / "event" / "manual"
        self.description = description  # 视觉模型描述

    def to_dict(self) -> dict:
        return {
            "app": self.app, "title": self.title,
            "timestamp": self.timestamp, "mode": self.mode,
            "description": self.description,
        }


class ActivityEvent:
    """结构化活动事件（从视觉分析 JSON 提取）"""
    __slots__ = ('app', 'activity', 'category', 'summary', 'detail', 'confidence',
                 'source', 'start_time', 'end_time')

    def __init__(self, app: str = "", activity: str = "", category: str = "other",
                 summary: str = "", detail: str = "", confidence: float = 0.5, source: str = "vision",
                 start_time: float = 0.0, end_time: float = 0.0):
        self.app = app                # 应用名
        self.activity = activity      # 具体活动（如 "writing code", "watching video"）
        self.category = category      # 分类：work/learn/entertainment/communication/other
        self.summary = summary        # 一句话摘要
        self.detail = detail          # 详细描述（50-80字）
        self.confidence = confidence  # 置信度 0~1
        self.source = source          # "vision"（模型推断）/ "foreground"（窗口标题直接判断）
        self.start_time = start_time  # 开始时间
        self.end_time = end_time      # 结束时间（0 = 进行中）

    @property
    def duration_minutes(self) -> float:
        if self.start_time and self.end_time:
            return (self.end_time - self.start_time) / 60.0
        return 0.0

    def is_same_activity(self, other: 'ActivityEvent') -> bool:
        """判断两个事件是否是同一活动（用于合并）"""
        return (self.app == other.app and self.activity == other.activity
                and self.category == other.category)

    def to_dict(self) -> dict:
        return {
            "app": self.app, "activity": self.activity,
            "category": self.category, "summary": self.summary,
            "confidence": self.confidence, "source": self.source,
            "start_time": self.start_time, "end_time": self.end_time,
            "duration_minutes": round(self.duration_minutes, 1),
        }
