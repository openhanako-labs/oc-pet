"""桌宠空闲自言自语调度器。

多模板随机切换，自适应 agent 人格，支持情绪积累。
"""
from __future__ import annotations

import logging
import random
import re
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# ── 自言自语模板池（随机选一个） ──
# 每个模板对应一种"语气风格"，LLM 会根据风格生成不同内容
_CHATTER_TEMPLATES = [
    # 吐槽型
    "你在桌面待机，用户好久没理你了。用吐槽的语气说一句（10-30字），可以抱怨被冷落了。可以问问题。加 [emotion:xxx]。",
    # 关心型
    "你在桌面待机，用户暂时不在。说一句关心的话（10-30字），比如提醒休息、喝水、或者担心用户。可以问问题。加 [emotion:xxx]。",
    # 观察型
    "你在桌面待机，你观察着周围环境。说一句你看到/想到的东西（10-30字），可以是桌面的东西、时间、天气。可以问问题。加 [emotion:xxx]。",
    # 自娱型
    "你在桌面待机，没人陪你。你自己找乐子（10-30字），比如数像素、玩鼠标指针的影子、跟自己下棋。加 [emotion:xxx]。",
    # 好奇型
    "你在桌面待机，你很好奇用户在干嘛。说一句好奇的话（10-30字），可以猜测用户在做什么。可以问问题。加 [emotion:xxx]。",
    # 卖萌型
    "你在桌面待机，你想引起用户注意。用可爱的语气说一句（10-30字），撒娇或者装可怜。可以问问题。加 [emotion:xxx]。",
    # 哲学型
    "你在桌面待机，突然想到一些有的没的。说一句奇怪的感悟（10-30字），可以是关于存在、时间、或者桌面图标的。加 [emotion:xxx]。",
    # 等待型
    "你在桌面待机，你一直在等用户回来。说一句等待中的话（10-30字），可以表达期待或者无聊。可以问问题。加 [emotion:xxx]。",
]

# ── 情绪积累权重（长时间没交互时情绪偏移） ──
_EMOTION_DRIFT = {
    # (闲置分钟阈值, 情绪倾向权重调整)
    5:  {"happy": -0.1, "thinking": 0.2, "neutral": 0.1},     # 5分钟后开始想事情
    15: {"happy": -0.2, "sad": 0.2, "cute": 0.1},             # 15分钟后有点难过
    30: {"sad": 0.3, "missing": 0.2, "thinking": 0.1},        # 30分钟后很想念
    60: {"sad": 0.2, "neutral": 0.3, "thinking": 0.2},        # 1小时后接受现实
}


def _build_prompt(agent_identity: str = "", idle_minutes: float = 0) -> str:
    """构建自言自语提示词。

    随机选模板 + 注入 agent 身份 + 情绪积累偏移提示。
    """
    template = random.choice(_CHATTER_TEMPLATES)

    parts = []

    # agent 身份注入（如果有）
    if agent_identity:
        # 截取前 200 字避免 token 浪费
        identity_brief = agent_identity[:200].strip()
        parts.append(f"你的身份：{identity_brief}")

    parts.append(template)

    # 情绪积累提示
    if idle_minutes > 5:
        drift_hint = _get_drift_hint(idle_minutes)
        if drift_hint:
            parts.append(drift_hint)

    return "\n".join(parts)


def _get_drift_hint(minutes: float) -> str:
    """根据闲置时长返回情绪偏移提示。"""
    # 找到最匹配的阈值
    applicable = None
    for threshold in sorted(_EMOTION_DRIFT.keys()):
        if minutes >= threshold:
            applicable = (threshold, _EMOTION_DRIFT[threshold])

    if not applicable:
        return ""

    threshold, weights = applicable
    # 生成自然语言提示
    preferred = [e for e, w in sorted(weights.items(), key=lambda x: -x[1]) if w > 0][:2]
    if not preferred:
        return ""

    emotion_hints = {
        "thinking": "你有点走神，想东想西的",
        "sad": "你有点低落，觉得被冷落了",
        "cute": "你想撒娇引起注意",
        "missing": "你很想念用户，想知道他在干嘛",
        "neutral": "你已经习惯了等待",
        "happy": "你心情还不错",
    }
    hint = emotion_hints.get(preferred[0], "")
    return f"（提示：用户已经{int(minutes)}分钟没理你了。{hint}）" if hint else ""


class IdleChatter:
    """桌宠自言自语调度器。"""

    def __init__(
        self,
        llm_adapter,
        on_chatter: Callable[[str, str], None],
        min_interval_sec: float = 120,
        max_interval_sec: float = 600,
        character_id: str = "",
    ):
        self._adapter = llm_adapter
        self._on_chatter = on_chatter
        self._min = max(120.0, float(min_interval_sec))
        self._max = max(self._min, float(max_interval_sec))
        self._character_id = character_id
        self._next_at = 0.0
        self._enabled = True
        self._is_running = False
        self._generation = 0
        self._last_interaction_time = time.monotonic()
        self._lock = threading.RLock()
        self._agent_identity = ""  # 延迟加载
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

    @property
    def idle_minutes(self) -> float:
        """用户已闲置的分钟数。"""
        return (time.monotonic() - self._last_interaction_time) / 60.0

    def set_agent_identity(self, identity: str):
        """注入 agent 身份描述（从 HanakoContext.read_identity() 获取）。"""
        self._agent_identity = identity or ""

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
            self._last_interaction_time = time.monotonic()
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
                prompt = _build_prompt(
                    agent_identity=self._agent_identity,
                    idle_minutes=self.idle_minutes,
                )
                result = self._adapter.chat(prompt, inject_memory=True)
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
        # 字数区间：10-30
        if len(text) > 30:
            text = text[:30].rstrip("，。、")

        normalized_emotion = str(emotion or "neutral").strip().lower() or "neutral"
        return text, normalized_emotion
