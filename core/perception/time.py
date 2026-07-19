"""时间感知 — 区分工作时段 / 休息时段 / 深夜

提供时段标签（morning / noon / afternoon / evening / late_night / midnight）
供 PerceptionController.build_context 注入 LLM prompt。
"""
from __future__ import annotations

from datetime import datetime


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
        now = datetime.now()
        hour = now.hour
        period, label = "other", "未知"
        for (start, end), (pid, plabel) in self.PERIODS.items():
            if start <= hour < end:
                period, label = pid, plabel
                break
        return {
            "period": period, "label": label, "hour": hour,
            "weekday": now.weekday(), "is_weekend": now.weekday() >= 5,
            "date": now.strftime("%Y-%m-%d"), "time": now.strftime("%H:%M"),
        }

    def format_for_prompt(self) -> str:
        ctx = self.get_context()
        weekend = "周末" if ctx["is_weekend"] else "工作日"
        return f"[当前时间：{ctx['label']} {ctx['time']}，{weekend}]"
