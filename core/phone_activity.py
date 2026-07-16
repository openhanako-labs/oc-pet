"""手机活动数据管理 + 感知层

存储手机上报的前台应用切换事件，提供：
- 应用分类（entertainment / communication / music / shopping / reading / work / gaming）
- 活动摘要（format_for_prompt）
- 空闲时间（距上次手机活动的分钟数）

数据结构：内存环形缓冲区，保留最近 200 条，重启清空。
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass

logger = logging.getLogger(__name__)

MAX_RECORDS = 200

# (关键词列表, 类别, 情绪)
APP_CATEGORY_MAP: list[tuple[list[str], str, str]] = [
    (['小红书', '抖音', 'b站', 'bilibili', '快手', '西瓜'], 'entertainment', 'happy'),
    (['微信', 'qq', 'telegram', 'whatsapp', 'signal', '钉钉'], 'communication', 'neutral'),
    (['网易云', 'spotify', 'apple music', '酷狗', '酷我', 'qq音乐'], 'music', 'happy'),
    (['淘宝', '京东', '拼多多', '天猫', '闲鱼', '得物'], 'shopping', 'happy'),
    (['kindle', '微信读书', '多看', '豆瓣阅读', '起点'], 'reading', 'thinking'),
    (['wps', '飞书', '企业微信', 'notion', 'obsidian', 'vscode'], 'work', 'thinking'),
    (['王者荣耀', '原神', '明日方舟', '崩坏', '阴阳师', '和平精英'], 'gaming', 'happy'),
]


@dataclass
class PhoneActivity:
    """一条手机活动记录"""
    app_name: str
    event: str
    timestamp: float
    category: str = ''


def classify_app(app_name: str) -> tuple[str, str]:
    """将应用名分类为类别和情绪

    Returns:
        (category, emotion) 如 ('entertainment', 'happy')
    """
    name_lower = app_name.lower()
    for keywords, category, emotion in APP_CATEGORY_MAP:
        for kw in keywords:
            if kw in name_lower:
                return category, emotion
    return ('other', 'neutral')


class PhoneActivityPerception:
    """手机活动感知层

    用法：
        perception = PhoneActivityPerception()
        perception.add_activity("小红书", "switch")
        print(perception.format_for_prompt())
        print(perception.get_idle_minutes())
    """

    def __init__(self):
        self._records: deque = deque(maxlen=MAX_RECORDS)
        self._lock = threading.Lock()

    def add_activity(self, app_name: str, event: str = 'switch'):
        """记录一条手机活动"""
        category, _ = classify_app(app_name)
        record = PhoneActivity(
            app_name=app_name,
            event=event,
            timestamp=time.time(),
            category=category,
        )

        with self._lock:
            self._records.append(record)

    def get_recent(self, minutes: int = 60) -> list[PhoneActivity]:
        """获取最近 N 分钟内的活动"""
        cutoff = time.time() - minutes * 60
        with self._lock:
            return [r for r in self._records if r.timestamp >= cutoff]

    def get_summary(self, minutes: int = 60) -> str:
        """获取活动摘要，如 "小红书(3次)、微信(2次)" """
        recent = self.get_recent(minutes)
        if not recent:
            return ''

        # 统计各 App 出现次数
        app_counts: dict[str, int] = {}
        for r in recent:
            app_counts[r.app_name] = app_counts.get(r.app_name, 0) + 1

        # 按次数降序，取前 5
        sorted_apps = sorted(app_counts.items(), key=lambda x: x[1], reverse=True)
        parts = [f"{name}({count}次)" for name, count in sorted_apps[:5]]
        return '、'.join(parts)

    def get_idle_minutes(self) -> float:
        """距上次手机活动的分钟数，无数据返回 -1.0"""
        with self._lock:
            if not self._records:
                return -1.0
            last = self._records[-1].timestamp
            return (time.time() - last) / 60.0

    def format_for_prompt(self) -> str:
        """格式化为 LLM 上下文字符串"""
        summary = self.get_summary(60)
        if summary:
            return f'[手机活动：最近1小时使用了 {summary}]'
        idle = self.get_idle_minutes()
        if idle < 0:
            return '[手机活动：暂无数据（手机端未连接）]'
        if idle > 60:
            return f'[手机活动：{int(idle)}分钟无活动]'
        return '[手机活动：暂无数据]'
