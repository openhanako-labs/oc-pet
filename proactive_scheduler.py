"""Proactive 主动对话调度器 — 闲置检测 + 前台分类 → 触发 Agent 对话

与 BreakNotifier 的边界：
  BreakNotifier       → 硬编码气泡文案，提醒喝水/休息
  ProactiveScheduler  → 规则引擎 + Agent 生成回复，关心/撒娇/卖萌

触发流程：
  1. tick() 每 30 秒检查（与 BreakNotifier 同频）
  2. 匹配规则 → 检查 cooldown → 随机权重判定
  3. 通过后 → on_proactive 回调 → 写入 outbox → Agent 回复 → 显示气泡

用法:
    scheduler = ProactiveScheduler(
        character_id="ophelia",
        foreground_watcher=watcher,
        on_proactive=lambda text: write_to_outbox(text),
    )
    scheduler.load_config(config.get("proactive", {}))
    # 在 pet.py 的 _break_check 中调用 scheduler.tick()
"""
from __future__ import annotations

import logging
import random
import time

from break_notifier import _get_idle_seconds

logger = logging.getLogger(__name__)


DEFAULT_RULES = [
    {
        "idle_min": 5,
        "foreground": ["writing", "development", "browsing"],
        "prompt": "写了这么久，休息一下吧？",
        "weight": 0.7,
    },
    {
        "idle_min": 15,
        "foreground": ["gaming", "entertainment"],
        "prompt": "带我一起玩嘛～",
        "weight": 0.5,
    },
    {
        "idle_min": 30,
        "foreground": ["communication"],
        "prompt": "还在忙吗？想和你说说话～",
        "weight": 0.3,
    },
    {
        "idle_min": 60,
        "foreground": ["*"],
        "prompt": "好安静啊……你在做什么呢？",
        "weight": 0.3,
    },
]


class ProactiveScheduler:
    """主动对话调度器 — 规则引擎 + Agent 联动。

    Attributes:
        character_id: 角色 ID（传递给回调）
        foreground_watcher: ForegroundWatcher 实例（用于获取前台分类）
        on_proactive: 回调 (prompt_text: str) -> None，触发时调用
    """

    def __init__(
        self,
        character_id: str = "ophelia",
        foreground_watcher=None,
        on_proactive: callable = None,
    ):
        self._character_id = character_id
        self._foreground_watcher = foreground_watcher
        self._enabled = True
        self._cooldown_minutes = 10
        self._rules: list[dict] = list(DEFAULT_RULES)

        self._cooldown_until: float = 0.0

        # 外部回调：触发时写入 outbox
        self.on_proactive: callable = on_proactive or (lambda text: None)

    # ── 配置 ──

    def load_config(self, config: dict):
        """从 config.json 的 proactive 配置段加载"""
        self._enabled = config.get("enabled", True)
        self._cooldown_minutes = config.get("cooldown_minutes", 10)
        self._rules = config.get("rules", list(DEFAULT_RULES))

    # ── 控制 ──

    @property
    def enabled(self) -> bool:
        return self._enabled

    def enable(self):
        self._enabled = True

    def disable(self):
        self._enabled = False

    def reset(self):
        """重置冷却（用户有操作时调用）"""
        self._cooldown_until = time.time() + self._cooldown_minutes * 60

    # ── 核心检查 ──

    def tick(self) -> str | None:
        """检查是否触发主动对话。

        在 pet.py 的 _break_check() 中周期性调用（每 30 秒）。

        Returns:
            触发的 prompt 文本（如果触发了），否则 None
        """
        if not self._enabled or not self._rules:
            return None

        now = time.time()

        # 1. 冷却检查
        if now < self._cooldown_until:
            return None

        # 2. 获取空闲时间和前台分类
        idle_sec = _get_idle_seconds()

        # 短时间不动（< 3 分钟）不触发
        if idle_sec < 180:
            return None

        category = "other"
        if self._foreground_watcher:
            category = self._foreground_watcher.last_category or "other"

        # 3. 规则匹配（按 idle_min 降序 → 最严格规则优先）
        sorted_rules = sorted(self._rules, key=lambda r: r.get("idle_min", 0), reverse=True)

        for rule in sorted_rules:
            required_idle = rule.get("idle_min", 0) * 60
            if idle_sec < required_idle:
                continue

            fg_match = rule.get("foreground", ["*"])
            if "*" in fg_match or category in fg_match:
                weight = rule.get("weight", 0.5)
                if random.random() < weight:
                    prompt = rule.get("prompt", "")
                    if prompt:
                        # 设置冷却
                        self._cooldown_until = now + self._cooldown_minutes * 60
                        logger.info(
                            "Proactive triggered: idle=%ds fg=%s rule='%s'",
                            int(idle_sec), category, prompt,
                        )
                        self.on_proactive(prompt)
                        return prompt

        return None