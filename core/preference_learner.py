"""用户偏好学习 — 硬反馈记录与降权/禁用

2026-09-06: P2 #10 改动
- 用户关掉气泡 → 记录负反馈，该类触发 24 小时内降权
- 用户说"别说了" → 记录强负反馈，该类触发 48 小时内禁用
- 用户主动 @ 触发 → 记录正反馈，该类触发 24 小时内加权
- 沉默不算数据（没有正信号）
"""
from __future__ import annotations

import logging
import time
import threading
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class FeedbackRecord:
    """反馈记录"""
    feedback_type: str  # positive / negative / strong_negative
    trigger_source: str  # proactive / idle / screen
    timestamp: float
    text: str = ""
    
    def age_hours(self) -> float:
        return (time.time() - self.timestamp) / 3600


class PreferenceLearner:
    """用户偏好学习器（单例，线程安全）"""
    
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._lock = threading.Lock()
        self._feedback_records: list[FeedbackRecord] = []
        self._max_records = 100  # 最多保留 100 条反馈记录
        
        # 降权/禁用配置
        self._negative_cooldown_hours = 24  # 负反馈降权 24 小时
        self._strong_negative_cooldown_hours = 48  # 强负反馈禁用 48 小时
        self._positive_boost_hours = 24  # 正反馈加权 24 小时
        
        logger.info("PreferenceLearner initialized")
    
    def record_feedback(self, feedback_type: str, trigger_source: str, text: str = ""):
        """记录反馈
        
        Args:
            feedback_type: positive / negative / strong_negative
            trigger_source: proactive / idle / screen
            text: 反馈时的文本内容
        """
        with self._lock:
            record = FeedbackRecord(
                feedback_type=feedback_type,
                trigger_source=trigger_source,
                timestamp=time.time(),
                text=text
            )
            self._feedback_records.append(record)
            
            # 保留最近 100 条
            if len(self._feedback_records) > self._max_records:
                self._feedback_records = self._feedback_records[-self._max_records:]
            
            logger.info("Feedback recorded: %s (source=%s)", feedback_type, trigger_source)
    
    def get_weight_multiplier(self, trigger_source: str) -> float:
        """获取触发源的权重倍数
        
        Returns:
            权重倍数（0.0 = 禁用，0.5 = 降权，1.0 = 正常，1.5 = 加权）
        """
        with self._lock:
            now = time.time()
            multiplier = 1.0
            
            # 遍历反馈记录，计算权重
            for record in reversed(self._feedback_records):
                if record.trigger_source != trigger_source:
                    continue
                
                age_hours = record.age_hours()
                
                if record.feedback_type == "strong_negative":
                    if age_hours < self._strong_negative_cooldown_hours:
                        return 0.0  # 禁用
                elif record.feedback_type == "negative":
                    if age_hours < self._negative_cooldown_hours:
                        multiplier = 0.5  # 降权
                elif record.feedback_type == "positive":
                    if age_hours < self._positive_boost_hours:
                        multiplier = 1.5  # 加权
                        break  # 正反馈优先，找到就停
            
            return multiplier
    
    def is_disabled(self, trigger_source: str) -> bool:
        """检查触发源是否被禁用"""
        return self.get_weight_multiplier(trigger_source) == 0.0
    
    def get_recent_feedbacks(self, limit: int = 10) -> list[FeedbackRecord]:
        """获取最近的反馈记录"""
        with self._lock:
            return self._feedback_records[-limit:]
    
    def clear_expired_feedbacks(self):
        """清理过期反馈记录"""
        with self._lock:
            cutoff = time.time() - 48 * 3600  # 48 小时前
            self._feedback_records = [
                r for r in self._feedback_records if r.timestamp > cutoff
            ]
            logger.info("Cleared expired feedbacks, remaining: %d", len(self._feedback_records))
