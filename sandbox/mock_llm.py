"""Mock LLM 适配器 - 替代 HanakoPetAdapter，不调用任何 API。

用法：
    from sandbox.mock_llm import MockLLMAdapter
    adapter = MockLLMAdapter(agent_id="ophelia")

    # 普通对话
    reply, emotion = adapter.chat("你好")

    # 脚本模式（按预设序列回复）
    adapter.set_script(["你好呀。[emotion:happy]", "在想什么呢？[emotion:thinking]"])
    reply, emotion = adapter.chat("测试")  # 第一次返回脚本[0]
"""
from __future__ import annotations

import logging
import random
import re
import time
from pathlib import Path

logger = logging.getLogger("sandbox.llm")

# ── 默认回复池 ──
DEFAULT_REPLIES: list[str] = [
    "嗯，听到了。[emotion:neutral]",
    "是吗……[emotion:thinking]",
    "有意思。[emotion:happy]",
    "我看看。[emotion:thinking]",
    "……你说得对。[emotion:neutral]",
    "嗯？再说说？[emotion:surprised]",
    "好吧。[emotion:sad]",
    "哈，确实。[emotion:happy]",
    "这倒不一定。[emotion:angry]",
    "我在。[emotion:neutral]",
]

# ── 按关键词匹配的回复 ──
KEYWORD_REPLIES: list[tuple[str, str]] = [
    (r"你好|hi|hello|嗨",     "你好呀。[emotion:happy]"),
    (r"再见|bye|拜拜",        "嗯，去吧。[emotion:sad]"),
    (r"谢谢|感谢|thanks",    "……不用谢。[emotion:happy]"),
    (r"笨|蠢|傻|stupid",      "……你才笨。[emotion:angry]"),
    (r"可爱|萌|cute",         "……别盯着我看。[emotion:happy]"),
    (r"天气|weather",         "我没法看窗外，但我能感觉你在想什么。[emotion:thinking]"),
    (r"时间|几点",            "你在问我时间？有意思。[emotion:thinking]"),
    (r"无聊|boring",          "那就和我说说话吧。[emotion:neutral]"),
    (r"生气|angry|气",        "……我没生气。[emotion:angry]"),
    (r"开心|高兴|happy",      "你开心就好。[emotion:happy]"),
]


class MockLLMAdapter:
    """模拟 LLM 适配器，接口与 HanakoPetAdapter 一致。

    三种回复模式：
    1. 脚本模式 - set_script() 后按序列返回，到末尾循环
    2. 关键词匹配 - 优先尝试匹配预设关键词
    3. 随机模式 - 从默认回复池随机选取
    """

    def __init__(self, agent_id: str = "ophelia", builtin: bool = False):
        self.agent_id = agent_id
        self._builtin = builtin
        self._script: list[str] | None = None
        self._script_index = 0
        self._history: list[dict] = []
        self._call_count = 0
        self._total_latency_ms = 0

        # 模拟配置
        self._model = "mock-llm-sandbox"
        self._max_context = 128000
        self._memory_budget = 800

        logger.info("MockLLM 初始化 | agent=%s | model=%s", agent_id, self._model)

    # ── 公开属性（与 HanakoPetAdapter 一致）──

    @property
    def system_prompt(self) -> str:
        return "[Sandbox Mock] 这是一个沙盒测试环境，不会调用任何 API。"

    @property
    def model_config(self) -> dict:
        return {"model": self._model}

    # ── 脚本控制 ──

    def set_script(self, replies: list[str]):
        """设置脚本回复序列，按顺序返回"""
        self._script = list(replies)
        self._script_index = 0
        logger.info("脚本模式 | %d 条预设回复", len(self._script))

    def clear_script(self):
        """清除脚本，回到随机模式"""
        self._script = None
        self._script_index = 0

    # ── 核心接口 ──

    def chat(self, message: str, inject_memory: bool = True,
             extra_context: str = "", tools: list = None) -> tuple:
        """模拟对话，返回 (reply, emotion)

        模拟 200-800ms 延迟以接近真实体验。
        """
        self._call_count += 1
        start = time.time()

        # 模拟延迟
        latency = random.uniform(0.15, 0.6)
        time.sleep(latency)

        # 1. 脚本模式优先
        if self._script:
            reply_raw = self._script[self._script_index % len(self._script)]
            self._script_index += 1
        else:
            # 2. 关键词匹配
            reply_raw = None
            for pattern, response in KEYWORD_REPLIES:
                if re.search(pattern, message, re.IGNORECASE):
                    reply_raw = response
                    break
            # 3. 随机默认
            if not reply_raw:
                reply_raw = random.choice(DEFAULT_REPLIES)

        # 解析情绪标签
        emotion = "neutral"
        em_match = re.search(r'\[emotion:(\w+)\]', reply_raw)
        if em_match:
            emotion = em_match.group(1)
            reply = re.sub(r'\s*\[emotion:\w+\]\s*$', '', reply_raw).strip()
        else:
            reply = reply_raw.strip()

        # 保存历史
        self._history.append({"role": "user", "content": message.strip()})
        self._history.append({"role": "assistant", "content": reply})

        elapsed_ms = int((time.time() - start) * 1000)
        self._total_latency_ms += elapsed_ms

        logger.info(
            "Mock LLM #%d | %dms | reply=%s | emotion=%s",
            self._call_count, elapsed_ms, reply[:40], emotion
        )

        return reply, emotion

    # ── 统计 ──

    @property
    def stats(self) -> dict:
        """返回调用统计"""
        avg = (self._total_latency_ms / self._call_count) if self._call_count else 0
        return {
            "calls": self._call_count,
            "total_latency_ms": self._total_latency_ms,
            "avg_latency_ms": int(avg),
            "history_length": len(self._history),
        }

    def reset_history(self):
        """清空对话历史"""
        self._history.clear()
        logger.info("历史已清空")
