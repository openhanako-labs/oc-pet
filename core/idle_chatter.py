"""桌宠空闲自言自语调度器。"""
from __future__ import annotations

import logging
import random
import re
import threading
import time
from typing import Callable

logger = logging.getLogger(__name__)

_IDLE_CHATTER_PROMPT = """你正在桌面待机，用户暂时没理你。随口说一句简短的话（正文不超过15字），可以是自言自语、观察周围、或者想念主人。不要问问题。回复末尾必须添加一个 [emotion:xxx] 标签，xxx 可选 happy/sad/angry/surprised/thinking/neutral/cute/missing。"""


class IdleChatter:
    """桌宠自言自语调度器。"""

    def __init__(
        self,
        llm_adapter,
        on_chatter: Callable[[str, str], None],
        min_interval_sec: float = 120,
        max_interval_sec: float = 600,
    ):
        self._adapter = llm_adapter
        self._on_chatter = on_chatter
        self._min = max(120.0, float(min_interval_sec))
        self._max = max(self._min, float(max_interval_sec))
        self._next_at = 0.0
        self._enabled = True
        self._is_running = False
        self._generation = 0
        self._lock = threading.RLock()
        self.reset()

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._enabled

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._is_running

    @property
    def min_interval_sec(self) -> float:
        return self._min

    def tick(self) -> bool:
        """检查是否到达触发时间；到时在后台生成一句自言自语。"""
        now = time.monotonic()
        with self._lock:
            if not self._enabled or self._is_running or now < self._next_at:
                return False
            self._is_running = True
            generation = self._generation

        worker = threading.Thread(
            target=self._generate,
            args=(generation,),
            name="idle-chatter",
            daemon=True,
        )
        try:
            worker.start()
        except Exception:
            logger.exception("Failed to start idle chatter worker")
            with self._lock:
                self._is_running = False
                if self._enabled and self._generation == generation:
                    self._schedule_next_locked(time.monotonic())
            return False
        return True

    def reset(self) -> None:
        """用户交互后作废未送达的闲聊，并重新安排触发时间。"""
        with self._lock:
            self._generation += 1
            self._schedule_next_locked(time.monotonic())

    def enable(self) -> None:
        with self._lock:
            if self._enabled:
                return
            self._enabled = True
            self._generation += 1
            self._schedule_next_locked(time.monotonic())

    def disable(self) -> None:
        with self._lock:
            self._enabled = False
            self._generation += 1
            self._next_at = float("inf")

    def _schedule_next_locked(self, now: float) -> None:
        self._next_at = now + random.uniform(self._min, self._max)

    def _generate(self, generation: int) -> None:
        text = ""
        emotion = "neutral"
        try:
            if self._adapter is None or not callable(getattr(self._adapter, "chat", None)):
                logger.warning("Idle chatter skipped: LLM adapter unavailable")
            else:
                result = self._adapter.chat(_IDLE_CHATTER_PROMPT, inject_memory=True)
                text, emotion = self._normalize_result(result)
        except Exception as exc:
            logger.warning("Idle chatter generation failed: %s", exc)

        with self._lock:
            self._is_running = False
            if not self._enabled or self._generation != generation:
                return

            self._schedule_next_locked(time.monotonic())
            if not text:
                return
            try:
                self._on_chatter(text, emotion)
            except Exception:
                logger.exception("Idle chatter callback failed")

    @staticmethod
    def _normalize_result(result) -> tuple[str, str]:
        if isinstance(result, tuple):
            raw_text = result[0] if result else ""
            emotion = result[1] if len(result) > 1 else "neutral"
        else:
            raw_text = result
            emotion = "neutral"

        if not isinstance(raw_text, str):
            return "", "neutral"

        matches = re.findall(r"\[emotion:(\w+)\]", raw_text, flags=re.IGNORECASE)
        if matches:
            emotion = matches[-1]
        text = re.sub(
            r"\s*\[emotion:\w+\]\s*",
            "",
            raw_text,
            flags=re.IGNORECASE,
        )
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) > 15:
            text = text[:15].rstrip()

        normalized_emotion = str(emotion or "neutral").strip().lower() or "neutral"
        return text, normalized_emotion
