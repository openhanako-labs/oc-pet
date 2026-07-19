"""主动对话调度 — 规则引擎 + 空闲检测 + 前台分类

触发流程：
1. tick() 检查是否过冷却期
2. 计算对话空闲时间（since last conversation）
3. 按 idle_min 倒序遍历 rules，匹配 foreground 分类
4. 命中规则的 weight 概率触发，并通过 on_proactive 回调上抛 prompt

外部依赖：
- random（概率触发）
"""
from __future__ import annotations

import logging
import random
import time

logger = logging.getLogger(__name__)


DEFAULT_RULES = [
    {"idle_min": 5,  "foreground": ["writing", "development", "browsing"], "prompt": "写了这么久，休息一下吧？", "weight": 0.7},
    {"idle_min": 15, "foreground": ["gaming", "entertainment"],             "prompt": "带我一起玩嘛～",          "weight": 0.5},
    {"idle_min": 30, "foreground": ["communication"],                       "prompt": "还在忙吗？想和你说说话～", "weight": 0.3},
    {"idle_min": 60, "foreground": ["*"],                                    "prompt": "好安静啊……你在做什么呢？",  "weight": 0.3},
]


class ProactiveScheduler:
    """主动对话调度器 - 规则引擎 + 空闲检测 + 前台分类"""

    def __init__(self, foreground_watcher=None, on_proactive: callable = None):
        self._foreground_watcher = foreground_watcher
        self._enabled = True
        self._cooldown_minutes = 10
        self._rules: list[dict] = list(DEFAULT_RULES)
        self._cooldown_until: float = 0.0
        self._last_conversation: float = time.time()  # 上次对话时间
        self.on_proactive: callable = on_proactive or (lambda text: None)

    def load_config(self, config: dict):
        self._enabled = config.get("enabled", True)
        self._cooldown_minutes = config.get("cooldown_minutes", 10)
        self._rules = config.get("rules", list(DEFAULT_RULES))

    def mark_conversation(self):
        """标记用户刚和桌宠对话过"""
        self._last_conversation = time.time()

    @property
    def enabled(self) -> bool:
        return self._enabled

    def enable(self):
        self._enabled = True

    def disable(self):
        self._enabled = False

    def reset(self):
        self._cooldown_until = time.time() + self._cooldown_minutes * 60

    def tick(self) -> str | None:
        if not self._enabled or not self._rules:
            return None
        now = time.time()
        if now < self._cooldown_until:
            return None

        # 对话空闲时间（上次对话到现在）
        conversation_idle = now - self._last_conversation

        category = "other"
        if self._foreground_watcher:
            category = self._foreground_watcher.last_category or "other"

        sorted_rules = sorted(self._rules, key=lambda r: r.get("idle_min", 0), reverse=True)
        for rule in sorted_rules:
            required_idle = rule.get("idle_min", 0) * 60
            if conversation_idle < required_idle:
                continue
            fg_match = rule.get("foreground", ["*"])
            if "*" in fg_match or category in fg_match:
                weight = rule.get("weight", 0.5)
                if random.random() < weight:
                    prompt = rule.get("prompt", "")
                    if prompt:
                        self._cooldown_until = now + self._cooldown_minutes * 60
                        logger.info("Proactive triggered: idle=%ds fg=%s rule='%s'", int(conversation_idle), category, prompt)
                        self.on_proactive(prompt)
                        return prompt
        return None
