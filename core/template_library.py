"""模板库 — 预设的模板文案，用于 LLM 降级或快速响应

2026-09-06: P2 #5 改动
- 模板库：预设的模板文案
- LLM 混合：LLM 生成 + 模板库降级
"""
from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class TemplateItem:
    """模板条目"""
    text: str
    scenario: str  # 场景标签（work / chat / idle / night 等）
    period: str  # 时间段（morning / afternoon / evening / night）
    weight: float = 1.0  # 权重


# 预设模板库
TEMPLATES: list[TemplateItem] = [
    # 早安
    TemplateItem(text="早上好~", scenario="greeting", period="morning", weight=1.0),
    TemplateItem(text="今天也要加油哦！", scenario="greeting", period="morning", weight=0.8),
    TemplateItem(text="新的一天开始啦~", scenario="greeting", period="morning", weight=0.8),
    
    # 午安
    TemplateItem(text="下午好~", scenario="greeting", period="afternoon", weight=1.0),
    TemplateItem(text="吃饭了吗？", scenario="greeting", period="afternoon", weight=0.8),
    TemplateItem(text="休息一下吧~", scenario="rest", period="afternoon", weight=0.7),
    
    # 晚安
    TemplateItem(text="晚上好~", scenario="greeting", period="evening", weight=1.0),
    TemplateItem(text="今天辛苦了~", scenario="greeting", period="evening", weight=0.8),
    TemplateItem(text="早点休息哦~", scenario="rest", period="evening", weight=0.7),
    
    # 深夜
    TemplateItem(text="还没睡吗？", scenario="night", period="night", weight=0.5),
    TemplateItem(text="要注意休息哦~", scenario="night", period="night", weight=0.5),
    
    # 工作提醒
    TemplateItem(text="写了这么久，休息一下吧？", scenario="rest", period="afternoon", weight=0.7),
    TemplateItem(text="站起来活动一下~", scenario="rest", period="afternoon", weight=0.7),
    TemplateItem(text="记得喝水~", scenario="rest", period="afternoon", weight=0.6),
    
    # 聊天
    TemplateItem(text="想和你说说话~", scenario="chat", period="evening", weight=0.5),
    TemplateItem(text="最近在忙什么？", scenario="chat", period="evening", weight=0.5),
    TemplateItem(text="有没有什么好玩的事？", scenario="chat", period="evening", weight=0.5),
]


def get_template(scenario: Optional[str] = None, period: Optional[str] = None) -> Optional[str]:
    """获取模板文案
    
    Args:
        scenario: 场景标签（work / chat / idle / night 等）
        period: 时间段（morning / afternoon / evening / night）
    
    Returns:
        模板文案，或 None（如果没有匹配的模板）
    """
    # 如果指定了场景和时间段，优先匹配
    if scenario and period:
        matched = [t for t in TEMPLATES if t.scenario == scenario and t.period == period]
        if matched:
            return _weighted_random(matched)
    
    # 如果指定了场景，按场景匹配
    if scenario:
        matched = [t for t in TEMPLATES if t.scenario == scenario]
        if matched:
            return _weighted_random(matched)
    
    # 如果指定了时间段，按时间段匹配
    if period:
        matched = [t for t in TEMPLATES if t.period == period]
        if matched:
            return _weighted_random(matched)
    
    # 默认：随机选择一个
    return _weighted_random(TEMPLATES)


def _weighted_random(items: list[TemplateItem]) -> str:
    """加权随机选择一个模板"""
    if not items:
        return ""
    
    weights = [item.weight for item in items]
    total = sum(weights)
    if total <= 0:
        return random.choice(items).text
    
    r = random.random() * total
    cumulative = 0.0
    for item, weight in zip(items, weights):
        cumulative += weight
        if r <= cumulative:
            return item.text
    
    return items[-1].text


def get_period() -> str:
    """获取当前时间段"""
    hour = time.localtime().tm_hour
    if hour < 8:
        return "night"
    elif hour < 12:
        return "morning"
    elif hour < 18:
        return "afternoon"
    else:
        return "evening"


def generate_template_fallback(context: Optional[dict] = None) -> str:
    """生成模板降级文案
    
    Args:
        context: 上下文（包含 scenario、period 等）
    
    Returns:
        模板文案
    """
    if context is None:
        context = {}
    
    scenario = context.get("scenario")
    period = context.get("period") or get_period()
    
    return get_template(scenario, period)
