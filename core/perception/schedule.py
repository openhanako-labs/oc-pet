"""日程感知 — 读取 Hanako 自动化任务

数据源：
  ~/.hanako/.ephemeral/automation*.json

每个文件可能是单个 dict 或 dict 列表，refresh() 把所有任务汇成单列表。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

HANAKO_HOME = Path.home() / ".hanako"


class SchedulePerception:
    """日程感知 - 读取 Hanako 自动化任务"""

    def __init__(self):
        self._automations: list[dict] = []

    def refresh(self):
        self._automations = []
        try:
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
        return self._automations[:max_items]

    def format_for_prompt(self) -> str:
        items = self.get_upcoming()
        if not items:
            return ""
        lines = ["[即将到来的定时任务]"]
        for item in items:
            label = item.get("label", item.get("name", "未知"))
            schedule = item.get("schedule", "")
            lines.append(f"- {label}（{schedule}）")
        return "\n".join(lines)
