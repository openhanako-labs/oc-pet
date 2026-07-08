"""感知系统 - 时间感知 + 情绪状态机 + 日程感知

为桌宠提供连续的环境感知能力，不依赖单次触发。
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

HANAKO_HOME = Path.home() / ".hanako"


# ── 时间感知 ──────────────────────────────────────────────

class TimePerception:
    """时间感知 - 区分工作时段/休息时段/深夜"""

    PERIODS = {
        (6, 12): ("morning", "早上"),
        (12, 14): ("noon", "中午"),
        (14, 18): ("afternoon", "下午"),
        (18, 22): ("evening", "晚上"),
        (22, 24): ("late_night", "深夜"),
        (0, 6): ("midnight", "凌晨"),
    }

    def get_context(self) -> dict:
        """返回当前时间段信息"""
        now = datetime.now()
        hour = now.hour

        period = "other"
        label = "未知"
        for (start, end), (pid, plabel) in self.PERIODS.items():
            if start <= hour < end:
                period = pid
                label = plabel
                break

        return {
            "period": period,
            "label": label,
            "hour": hour,
            "weekday": now.weekday(),  # 0=Mon, 6=Sun
            "is_weekend": now.weekday() >= 5,
            "date": now.strftime("%Y-%m-%d"),
            "time": now.strftime("%H:%M"),
        }

    def format_for_prompt(self) -> str:
        """格式化为可注入 prompt 的文本"""
        ctx = self.get_context()
        weekend = "周末" if ctx["is_weekend"] else "工作日"
        return f"[当前时间：{ctx['label']} {ctx['time']}，{weekend}]"


# ── 情绪状态机 ────────────────────────────────────────────

class EmotionStateMachine:
    """情绪状态机 - 连续感知，强度衰减

    不是单次触发就切帧，而是：
    1. 触发情绪时设置 intensity=1.0
    2. 每分钟衰减 decay_rate
    3. intensity > threshold_high -> 显示对应情绪帧
    4. intensity 在 threshold_low~high 之间 -> 渐变
    5. intensity < threshold_low -> 回到 neutral
    """

    DECAY_RATE = 0.08  # 每分钟衰减 8%
    THRESHOLD_HIGH = 0.5
    THRESHOLD_LOW = 0.15

    def __init__(self):
        self._current: str = "neutral"
        self._intensity: float = 0.0
        self._last_trigger: float = 0.0
        self._history: list[dict] = []  # 最近 10 条情绪记录

    def trigger(self, emotion: str, intensity: float = 1.0):
        """触发情绪"""
        if not emotion or emotion == "neutral":
            return

        self._current = emotion
        self._intensity = min(1.0, max(0.0, intensity))
        self._last_trigger = time.time()

        self._history.append({
            "emotion": emotion,
            "intensity": self._intensity,
            "time": datetime.now().isoformat(),
        })
        if len(self._history) > 10:
            self._history.pop(0)

    def tick(self):
        """每分钟调用 - 衰减情绪强度"""
        if self._current == "neutral":
            return

        elapsed = time.time() - self._last_trigger
        decay = self.DECAY_RATE * (elapsed / 60.0)
        self._intensity = max(0.0, self._intensity - decay)

        if self._intensity <= self.THRESHOLD_LOW:
            self._current = "neutral"
            self._intensity = 0.0

    def reset(self):
        """重置到 neutral（用户交互时调用）"""
        self._current = "neutral"
        self._intensity = 0.0
        self._last_trigger = time.time()

    @property
    def current(self) -> str:
        return self._current

    @property
    def intensity(self) -> float:
        return self._intensity

    def should_show_emotion(self) -> bool:
        """是否应该显示非 neutral 情绪"""
        return self._intensity > self.THRESHOLD_LOW

    def format_for_prompt(self) -> str:
        """格式化当前情绪状态"""
        if self._current == "neutral":
            return ""
        return f"[当前情绪：{self._current}（强度 {self._intensity:.0%}）]"


# ── 日程感知 ──────────────────────────────────────────────

class SchedulePerception:
    """日程感知 - 读取 Hanako 的自动化任务"""

    def __init__(self):
        self._automations: list[dict] = []

    def refresh(self):
        """刷新自动化任务列表"""
        # Hanako 的自动化配置存储位置待确认
        # 可能的路径：~/.hanako/automations.json 或类似
        self._automations = []
        try:
            # 尝试读取 Hanako 定时任务
            auto_dir = HANAKO_HOME / ".ephemeral"
            if auto_dir.exists():
                for f in auto_dir.glob("automation*.json"):
                    try:
                        data = json.loads(f.read_text("utf-8"))
                        if isinstance(data, list):
                            self._automations.extend(data)
                        elif isinstance(data, dict):
                            self._automations.append(data)
                    except Exception:
                        pass
        except Exception as e:
            logger.debug("Schedule refresh failed: %s", e)

    def get_upcoming(self, max_items: int = 3) -> list[dict]:
        """获取即将到来的任务（简化版，不解析 cron）"""
        return self._automations[:max_items]

    def format_for_prompt(self) -> str:
        """格式化日程上下文"""
        items = self.get_upcoming()
        if not items:
            return ""
        lines = ["[即将到来的定时任务]"]
        for item in items:
            label = item.get("label", item.get("name", "未知"))
            schedule = item.get("schedule", "")
            lines.append(f"- {label}（{schedule}）")
        return "\n".join(lines)


# ── 统一感知控制器 ────────────────────────────────────────

class PerceptionController:
    """统一感知控制器 - 整合时间、情绪、日程"""

    def __init__(self, character_id: str = "ophelia"):
        self._character_id = character_id
        self._time = TimePerception()
        self._emotion = EmotionStateMachine()
        self._schedule = SchedulePerception()
        self._last_schedule_refresh = 0.0

    @property
    def time(self) -> TimePerception:
        return self._time

    @property
    def emotion(self) -> EmotionStateMachine:
        return self._emotion

    @property
    def schedule(self) -> SchedulePerception:
        return self._schedule

    def tick_emotion(self):
        """每分钟调用 - 情绪衰减"""
        self._emotion.tick()

    def tick_schedule(self):
        """每 10 分钟刷新日程"""
        now = time.time()
        if now - self._last_schedule_refresh > 600:
            self._schedule.refresh()
            self._last_schedule_refresh = now

    def trigger_emotion(self, emotion: str, intensity: float = 1.0):
        """触发情绪"""
        self._emotion.trigger(emotion, intensity)

    def reset_emotion(self):
        """重置情绪（用户交互时）"""
        self._emotion.reset()

    def build_context(self) -> str:
        """组合所有感知信息为 prompt 上下文"""
        parts = []

        time_ctx = self._time.format_for_prompt()
        if time_ctx:
            parts.append(time_ctx)

        emotion_ctx = self._emotion.format_for_prompt()
        if emotion_ctx:
            parts.append(emotion_ctx)

        schedule_ctx = self._schedule.format_for_prompt()
        if schedule_ctx:
            parts.append(schedule_ctx)

        return "\n".join(parts) if parts else ""
