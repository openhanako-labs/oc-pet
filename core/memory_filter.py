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


# 情感状态关键词（这些记忆不进桌宠调度：影响状态，不是信息）
# 2026-09-10 修复：原实现将「喜欢/爱/想」也归入此类，且情感判定在前，
# 导致 docstring 明确要保留的偏好类记忆（如「用户喜欢美式咖啡」）被全部丢弃。
# 现在按语义拆表：情感状态 / 偏好立场，后者算事实。
EMOTIONAL_STATE_KEYWORDS = [
    "情绪", "心情", "感受", "感觉", "寂寞", "孤单", "孤独",
    "想念", "思念", "难过", "伤心", "开心", "快乐",
    "害怕", "恐惧", "焦虑", "压力", "抑郁", "低落",
]

# 偏好 / 立场关键词（属于事实：是“关于用户的稳定信息”）
PREFERENCE_KEYWORDS = [
    "喜欢", "偏好", "爱好", "讨厌", "爱吃", "爱喝", "习惯",
]

# 事实类关键词（这些记忆可以进桌宠调度）
FACT_KEYWORDS = [
    "考试", "会议", "工作", "学习", "上课", "课程",
    "习惯", "爱好", "兴趣", "偏好",
    "计划", "安排", "时间", "日期", "截止", "deadline",
    "地址", "位置", "地点", "电话", "邮箱",
]

# 兼容旧名（供外部/测试引用）
EMOTIONAL_KEYWORDS = EMOTIONAL_STATE_KEYWORDS


def is_fact_memory(content: str) -> bool:
    """判断是否是事实类记忆

    事实类记忆：纯信息性内容，或关于用户的稳定偏好/立场
    情感类记忆：包含情感表达、心理状态

    2026-09-10 修复：原实现里「喜欢」同时存在于情感/事实两张表，
    且情感判定在前，导致所有偏好类记忆被误判为情感类而丢弃。

    判定顺序（有意如此）：
      1) 情感状态词命中 → 不算事实（“最近心情不好”不进调度）
      2) 偏好/立场词、事实词、日期、时间 → 算事实
      3) 短文本且无情感词 → 算事实

    注：过短的因果句可能混淆（“我不喜欢他”含「喜欢」→ 判为事实）。
    这是有意取向：宁可多保留事实，也不丢用户偏好。
    """
    if not content:
        return False
    content_lower = content.lower()

    # 1) 情感状态优先排除（但先让明确的日期/时间事实通过，避免“考试压力”被误杀）
    has_datetime = bool(
        re.search(r'\d{4}[-/]\d{2}[-/]\d{2}', content)
        or re.search(r'\d+[:：]\d{2}', content)
    )
    if not has_datetime:
        for keyword in EMOTIONAL_STATE_KEYWORDS:
            if keyword in content_lower:
                return False

    # 2) 偏好 / 事实关键词
    for keyword in PREFERENCE_KEYWORDS:
        if keyword in content_lower:
            return True
    for keyword in FACT_KEYWORDS:
        if keyword in content_lower:
            return True

    # 3) 日期 / 时间
    if has_datetime:
        return True

    # 4) 短文本且无情感词
    if len(content) < 20:
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
