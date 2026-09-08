"""桌宠使用记忆 — 桌宠自己的"使用记忆"，不写 Hana

2026-09-06: P2 #13 改动
- 桌宠只读 Hana 的记忆，不写
- 桌宠有自己的"使用记忆"（如用户划走了哪条气泡），存在本地
- "使用记忆"可以做置信度衰减，但不同步到 Hana
- 如果用户说"别再说寂寞了"，桌宠记录到"使用记忆"，24 小时内不再触发该类内容
"""
from __future__ import annotations

import json
import logging
import time
import threading
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class UsageRecord:
    """使用记录"""
    content_type: str  # 内容类型（如"寂寞"、"考试提醒"）
    action: str  # 用户动作（dismiss / like / ignore / explicit_ban）
    timestamp: float
    text: str = ""
    confidence: float = 1.0  # 置信度（0.0 - 1.0）
    
    def age_hours(self) -> float:
        return (time.time() - self.timestamp) / 3600
    
    def decayed_confidence(self, half_life_hours: float = 24.0) -> float:
        """置信度衰减（半衰期：24 小时）"""
        return self.confidence * (0.5 ** (self.age_hours() / half_life_hours))


class UsageMemory:
    """桌宠使用记忆（单例，线程安全）"""
    
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance
    
    def __init__(self, storage_path: Optional[str] = None):
        if self._initialized:
            return
        self._initialized = True
        self._lock = threading.Lock()
        
        # 存储路径
        if storage_path is None:
            from config import get_user_data_dir
            storage_path = str(get_user_data_dir() / "usage_memory.json")
        self._storage_path = storage_path
        
        # 使用记录
        self._records: list[UsageRecord] = []
        self._max_records = 500  # 最多保留 500 条记录
        
        # 禁止内容列表（用户明确说"别再说 X 了"）
        self._banned_content: dict[str, float] = {}  # content_type -> banned_until timestamp
        
        # 置信度衰减半衰期（小时）
        self._half_life_hours = 24.0
        
        # 加载持久化数据
        self._load()
        
        logger.info("UsageMemory initialized (path=%s)", storage_path)
    
    def record_usage(self, content_type: str, action: str, text: str = "", confidence: float = 1.0):
        """记录使用
        
        Args:
            content_type: 内容类型（如"寂寞"、"考试提醒"）
            action: 用户动作（dismiss / like / ignore / explicit_ban）
            text: 文本内容
            confidence: 置信度（0.0 - 1.0）
        """
        with self._lock:
            record = UsageRecord(
                content_type=content_type,
                action=action,
                timestamp=time.time(),
                text=text,
                confidence=confidence
            )
            self._records.append(record)
            
            # 如果是明确禁止，添加到禁止列表
            if action == "explicit_ban":
                self._banned_content[content_type] = time.time() + 24 * 3600  # 24 小时禁止
            
            # 保留最近 500 条
            if len(self._records) > self._max_records:
                self._records = self._records[-self._max_records:]
            
            self._save()
            logger.info("Usage recorded: %s (action=%s)", content_type, action)
    
    def is_banned(self, content_type: str) -> bool:
        """检查内容类型是否被禁止"""
        with self._lock:
            banned_until = self._banned_content.get(content_type, 0)
            if banned_until > time.time():
                return True
            # 清理过期禁止
            if banned_until > 0 and banned_until <= time.time():
                del self._banned_content[content_type]
            return False
    
    def get_confidence(self, content_type: str) -> float:
        """获取内容类型的置信度（基于历史使用记录）
        
        Returns:
            置信度（0.0 - 1.0），0.0 表示禁用
        """
        with self._lock:
            if self.is_banned(content_type):
                return 0.0
            
            # 查找相关记录，计算衰减后的置信度
            total_confidence = 0.0
            count = 0
            
            for record in reversed(self._records):
                if record.content_type != content_type:
                    continue
                
                decayed = record.decayed_confidence(self._half_life_hours)
                
                if record.action == "dismiss":
                    total_confidence -= decayed * 0.5  # 划走：负向信号
                elif record.action == "like":
                    total_confidence += decayed * 1.0  # 喜欢：正向信号
                elif record.action == "ignore":
                    pass  # 忽略：不算信号
                elif record.action == "explicit_ban":
                    return 0.0  # 明确禁止：禁用
                
                count += 1
                if count >= 10:  # 只看最近 10 条
                    break
            
            # 计算置信度（0.0 - 1.0）
            if count == 0:
                return 1.0  # 没有记录：默认置信度 1.0
            
            # 归一化到 0.0 - 1.0
            confidence = max(0.0, min(1.0, 0.5 + total_confidence / count))
            return confidence
    
    def get_recent_records(self, limit: int = 20) -> list[UsageRecord]:
        """获取最近的使用记录"""
        with self._lock:
            return self._records[-limit:]
    
    def clear_expired(self, max_age_hours: float = 48.0):
        """清理过期记录"""
        with self._lock:
            cutoff = time.time() - max_age_hours * 3600
            self._records = [
                r for r in self._records if r.timestamp > cutoff
            ]
            # 清理过期禁止
            self._banned_content = {
                k: v for k, v in self._banned_content.items() if v > time.time()
            }
            self._save()
            logger.info("Cleared expired usage records, remaining: %d", len(self._records))
    
    def _load(self):
        """从文件加载使用记忆"""
        try:
            if not Path(self._storage_path).exists():
                return
            
            with open(self._storage_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            self._records = [
                UsageRecord(**record) for record in data.get("records", [])
            ]
            self._banned_content = data.get("banned_content", {})
            
            logger.info("Loaded usage memory: %d records", len(self._records))
        except Exception as e:
            logger.warning("Failed to load usage memory: %s", e)
    
    def _save(self):
        """保存使用记忆到文件"""
        try:
            data = {
                "records": [asdict(r) for r in self._records],
                "banned_content": self._banned_content
            }
            
            # 确保目录存在
            Path(self._storage_path).parent.mkdir(parents=True, exist_ok=True)
            
            with open(self._storage_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            
            logger.debug("Saved usage memory: %d records", len(self._records))
        except Exception as e:
            logger.warning("Failed to save usage memory: %s", e)
