"""情绪状态机 — 连续感知、强度自动衰减

线程安全：
- 所有写操作（trigger / tick / reset）加锁
- 所有读操作（property / format_for_prompt）也加锁，避免读到撕裂状态
"""
from __future__ import annotations

import threading
import time
from datetime import datetime

import logging

logger = logging.getLogger(__name__)


class EmotionStateMachine:
    """情绪状态机 - 连续感知，强度衰减（线程安全）"""

    DECAY_RATE = 0.08       # 每分钟衰减 8%
    THRESHOLD_HIGH = 0.5
    THRESHOLD_LOW = 0.15

    def __init__(self):
        self._current: str = "neutral"
        self._intensity: float = 0.0
        self._last_trigger: float = 0.0
        self._history: list[dict] = []
        self._lock = threading.Lock()

    def trigger(self, emotion: str, intensity: float = 1.0):
        if not emotion or emotion == "neutral":
            return
        with self._lock:
            self._current = emotion
            self._intensity = min(1.0, max(0.0, intensity))
            self._last_trigger = time.time()
            self._history.append({"emotion": emotion, "intensity": self._intensity, "time": datetime.now().isoformat()})
            if len(self._history) > 10:
                self._history.pop(0)

    def tick(self):
        with self._lock:
            if self._current == "neutral":
                return
            elapsed = time.time() - self._last_trigger
            decay = self.DECAY_RATE * (elapsed / 60.0)
            self._intensity = max(0.0, self._intensity - decay)
            if self._intensity <= self.THRESHOLD_LOW:
                self._current = "neutral"
                self._intensity = 0.0

    def reset(self):
        with self._lock:
            self._current = "neutral"
            self._intensity = 0.0
            self._last_trigger = time.time()

    @property
    def current(self) -> str:
        with self._lock:
            return self._current

    @property
    def intensity(self) -> float:
        with self._lock:
            return self._intensity

    def should_show_emotion(self) -> bool:
        with self._lock:
            return self._intensity > self.THRESHOLD_LOW

    def format_for_prompt(self) -> str:
        with self._lock:
            if self._current == "neutral":
                return ""
            return f"[当前情绪：{self._current}（强度 {self._intensity:.0%}）]"
