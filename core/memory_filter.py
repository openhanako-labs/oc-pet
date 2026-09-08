"""记忆过滤 — 只用事实类记忆，避免情感类记忆进桌宠调度

2026-09-06: P2 #12 改动
- 只用两类记忆：
  1. 用户主动标记为"可以提"的标签
  2. 纯事实类（今天有考试 / 有会议 / 用户喜欢美式咖啡）
- 情感类记忆一律不进桌宠调度（避免"AI 太懂我"的恐怖谷）
- 如果引用记忆，气泡必须明示：\"我引用的是你 X 月 X 日的记忆\"
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class MemoryItem:
    """记忆条目"""
    content: str
    date: str  # YYYY-MM-DD
    tags: list[str] = None
    is_fact: bool = False  # 是否事实类记忆
    is_marked: bool = False  # 是否用户主动标记为"可以提"
    
    def __post_init__(self):
        if self.tags is None:
            self.tags = []


# 情感类关键词（这些记忆不进桌宠调度）
EMOTIONAL_KEYWORDS = [
    "情绪", "心情", "感受", "感觉", "寂寞", "孤单", "孤独",
    "想", "思念", "想念", "难过", "伤心", "开心", "快乐",
    "害怕", "恐惧", "焦虑", "压力", "抑郁", "低落",
    "爱", "喜欢", "讨厌", "恨", "嫉妒", "羡慕"
]

# 事实类关键词（这些记忆可以进桌宠调度）
FACT_KEYWORDS = [
    "考试", "会议", "工作", "学习", "上课", "课程",
    "喜欢", "偏好", "习惯", "爱好", "兴趣",
    "计划", "安排", "时间", "日期", "截止", "deadline",
    "地址", "位置", "地点", "电话", "邮箱"
]


def is_fact_memory(content: str) -> bool:
    """判断是否是事实类记忆
    
    事实类记忆：纯信息性内容，不包含情感表达
    情感类记忆：包含情感表达、心理状态
    """
    content_lower = content.lower()
    
    # 检查情感类关键词（优先）
    for keyword in EMOTIONAL_KEYWORDS:
        if keyword in content_lower:
            return False
    
    # 检查事实类关键词
    for keyword in FACT_KEYWORDS:
        if keyword in content_lower:
            return True
    
    # 默认：如果包含数字、时间、日期，可能是事实类
    if re.search(r'\d{4}[-/]\d{2}[-/]\d{2}', content):  # 日期
        return True
    if re.search(r'\d+[:：]\d{2}', content):  # 时间
        return True
    
    # 默认：短文本（< 20 字）且没有情感词，可能是事实类
    if len(content) < 20 and not any(kw in content_lower for kw in EMOTIONAL_KEYWORDS):
        return True
    
    return False


def filter_facts_only(memory_context: str) -> str:
    """过滤记忆上下文，只保留事实类记忆
    
    Args:
        memory_context: 原始记忆上下文（多行，每行一条记忆）
    
    Returns:
        过滤后的记忆上下文（只包含事实类记忆）
    """
    if not memory_context:
        return ""
    
    filtered_lines = []
    
    # 按行分割记忆上下文
    for line in memory_context.split('\n'):
        line = line.strip()
        if not line:
            continue
        
        # 判断是否是事实类记忆
        if is_fact_memory(line):
            filtered_lines.append(line)
    
    return '\n'.join(filtered_lines)


def add_memory_citation(text: str, memory_date: Optional[str] = None) -> str:
    """为引用记忆的气泡添加引用标记
    
    Args:
        text: 气泡文本
        memory_date: 引用的记忆日期（YYYY-MM-DD）
    
    Returns:
        添加引用标记的气泡文本
    """
    if not memory_date:
        return text
    
    # 添加引用标记
    citation = f"\n[引用记忆：{memory_date}]"
    return text + citation
