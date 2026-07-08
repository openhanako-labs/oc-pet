"""Harness adapter for OC Desktop Pet — Hanako 原生版。

从 Hanako 本体文件读取角色设定和模型配置：
  - identity.md / ishiki.md / description.md → 角色设定
  - provider-catalog.json → API 地址和密钥
  - memory/ → 记忆上下文注入

不再使用 skills/public/<角色>/SKILL.md 和 config.json 的独立配置。
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

import requests

from hanako_context import HanakoContext

logger = logging.getLogger(__name__)


class HanakoPetAdapter:
    """桌宠适配器：读取 Hanako 本体配置 → API 对话 → 返回回复

    完全依赖 HanakoContext 读取 Hanako 的同一套文件。
    不再保留独立的角色 prompt 和 API 配置。
    """

    def __init__(self, agent_id: str = "ophelia"):
        self.agent_id = agent_id
        self._context = HanakoContext(agent_id)

        # 读取模型配置
        self._model_cfg = self._context.read_model_config()
        self._base_url = self._model_cfg.get("base_url", "")
        self._api_key = self._model_cfg.get("api_key", "")
        self._model = self._model_cfg.get("model", "")
        self._api_type = self._model_cfg.get("api_type", "openai-completions")

        # 构建 system prompt
        self._system_prompt = self._context.build_prompt()

        # 会话历史（内存）
        self._history: list[dict] = []

        # 验证
        missing = self._context.validate()
        if missing:
            logger.warning("配置不完整，缺失: %s", ", ".join(missing))

        logger.info(
            "HanakoPetAdapter ready | agent=%s | model=%s | api=%s | prompt_len=%d",
            agent_id, self._model, self._base_url[:40] + "..." if self._base_url else "N/A",
            len(self._system_prompt),
        )

    @property
    def system_prompt(self) -> str:
        return self._system_prompt

    @property
    def model_config(self) -> dict:
        return dict(self._model_cfg)

    def chat(self, message: str, inject_memory: bool = True, extra_context: str = "") -> str:
        """发送消息，返回角色回复。

        Args:
            message: 用户消息
            inject_memory: 是否注入记忆上下文
            extra_context: 额外上下文（时间/情绪/日程等感知信息）

        Returns:
            角色回复文本
        """
        if not self._base_url or not self._api_key:
            return "…（模型未配置，请在 Hanako 设置中配置模型后重试）"

        messages = [{"role": "system", "content": self._system_prompt}]

        # 注入记忆
        if inject_memory:
            memory_text = self._context.build_memory_context(max_chars=800)
            if memory_text:
                messages.append({
                    "role": "system",
                    "content": f"[以下是你当前的记忆和状态，请自然参考——不要逐字复述，可以作为话题延续的线索]\n{memory_text}",
                })

        # 注入感知上下文（时间/情绪/日程）
        if extra_context:
            messages.append({
                "role": "system",
                "content": extra_context,
            })

        # 追加最近对话历史（最多 10 轮）
        for turn in self._history[-10:]:
            messages.append(turn)

        messages.append({"role": "user", "content": message.strip()})

        try:
            resp = self._call_api(messages)
            text = resp.strip() or "…"

            # 保存到历史
            self._history.append({"role": "user", "content": message.strip()})
            self._history.append({"role": "assistant", "content": text})

            return text
        except requests.exceptions.Timeout:
            return "...（网络有点慢，你再说一遍？）"
        except requests.exceptions.ConnectionError:
            return "...（连不上——检查一下 Hanako 的网络配置吧）"
        except Exception as e:
            logger.warning("Chat failed: %s", e)
            return f"...（出了点岔子）"

    def _call_api(self, messages: list[dict]) -> str:
        """调用 LLM API

        支持两种 API 类型：
          - openai-completions: POST /chat/completions
          - anthropic-messages: POST /messages
        """
        if self._api_type == "anthropic-messages":
            return self._call_anthropic(messages)
        else:
            return self._call_openai(messages)

    def _call_openai(self, messages: list[dict]) -> str:
        """调用 OpenAI 兼容 API"""
        url = f"{self._base_url.rstrip('/')}/chat/completions"
        resp = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self._model,
                "messages": messages,
                "temperature": 0.7,
                "max_tokens": 300,
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"].strip()

    def _call_anthropic(self, messages: list[dict]) -> str:
        """调用 Anthropic 兼容 API"""
        url = f"{self._base_url.rstrip('/')}/messages"

        # 分离 system 消息
        system_content = ""
        api_messages = []
        for m in messages:
            if m["role"] == "system":
                system_content += m["content"] + "\n"
            else:
                api_messages.append(m)

        payload = {
            "model": self._model,
            "messages": api_messages,
            "max_tokens": 300,
            "temperature": 0.7,
        }
        if system_content.strip():
            payload["system"] = system_content.strip()

        resp = requests.post(
            url,
            headers={
                "x-api-key": self._api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["content"][0]["text"].strip()

    def reset_history(self):
        """清空对话历史"""
        self._history.clear()