"""Harness adapter for OC Desktop Pet - Hanako 原生版。

从 Hanako 本体文件读取角色设定和模型配置:
  - identity.md / ishiki.md / description.md → 角色设定
  - provider-catalog.json → API 地址和密钥
  - memory/ → 记忆上下文注入

不再使用 skills/public/<角色>/SKILL.md 和 config.json 的独立配置。
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import requests

from .hanako_context import HanakoContext
from .hanako_ws_client import HanakoUnavailableBeforeSend

logger = logging.getLogger(__name__)


class HanakoUnavailableAfterSend(Exception):
    """已经交给 Hanako 但尚未得到承诺回复 — 不要 fallback，会造成双执行。"""


class HanakoPetAdapter:
    """桌宠适配器:读取 Hanako 本体配置 → API 对话 → 返回回复

    完全依赖 HanakoContext 读取 Hanako 的同一套文件。
    不再保留独立的角色 prompt 和 API 配置。
    """

    def __init__(self, agent_id: str = "yuexinmiao", builtin: bool = False):
        self.agent_id = agent_id
        self._builtin = builtin
        self._context = HanakoContext(agent_id, builtin=builtin)

        # 读取模型配置 - .env 优先,回退到 Hanako, builtin 回退到 catalog 默认
        from env_config import get_llm_config
        env_llm = get_llm_config()
        if env_llm:
            self._base_url = env_llm["base_url"]
            self._api_key = env_llm["api_key"]
            self._model = env_llm["model"]
            self._api_type = "openai-completions"
            self._max_context = 0
            self._model_cfg = {"model": self._model}
            logger.info("LLM using .env override | model=%s", self._model)
        else:
            self._model_cfg = self._context.read_model_config()
            self._base_url = self._model_cfg.get("base_url", "")
            self._api_key = self._model_cfg.get("api_key", "")
            self._model = self._model_cfg.get("model", "")
            self._api_type = self._model_cfg.get("api_type", "openai-completions")
            self._max_context = self._model_cfg.get("max_context", 0)
            self._model_cfg = {"model": self._model}  # 统一属性名

            # builtin 角色没有 Hanako agent 目录，从 catalog 读默认 provider
            if builtin and (not self._base_url or not self._api_key):
                self._load_default_from_catalog()

        # 记忆预算: 优先使用用户配置，否则按模型 context 的 1% 计算
        from config import load_config
        config = load_config()
        memory_config = config.get('memory', {})
        
        user_budget = memory_config.get('budget_chars', 0)
        user_percent = memory_config.get('budget_percent', 1.0)
        
        if user_budget > 0:
            # 用户指定了固定字符数
            self._memory_budget = user_budget
            logger.info("Memory budget: %d chars (user configured)", self._memory_budget)
        elif self._max_context > 0:
            # 按模型 context 的百分比计算
            self._memory_budget = max(800, min(6000, int(self._max_context * user_percent / 100)))
            logger.info("Memory budget: %d chars (%.1f%% of %s)",
                         self._memory_budget, user_percent,
                         f"{self._max_context:,}" if self._max_context else "unknown")
        else:
            self._memory_budget = 800
            logger.info("Memory budget: %d chars (default)", self._memory_budget)

        # 构建 system prompt
        self._system_prompt = self._context.build_prompt()

        # 会话历史(内存)
        self._history: list[dict] = []

        # 验证
        missing = self._context.validate()
        if missing and not builtin:
            logger.warning("配置不完整,缺失: %s", ", ".join(missing))

        # ── M4: Hanako WS 传输模式 ──
        from env_config import get_hanako_config
        hanako_cfg = get_hanako_config()
        self.transport_mode: str = hanako_cfg["transport_mode"]
        self._reply_timeout: float = float(hanako_cfg["reply_timeout"])
        self._mirror_external_replies: bool = hanako_cfg["mirror_external_replies"]

        # 共享实例由 PetManager / ConversationEngine 注入；适配器不创建第二条 WS。
        self._session_manager = None
        logger.info("Hanako transport configured: %s", self.transport_mode)

        # 当前 Session 引用（由 PetManager / ConversationEngine 注入）
        self._current_session = None  # SessionRef | None
        self._pinned_session_id = None  # 钉住的 session_id，确保复用同一个 session

    def _load_default_from_catalog(self):
        """builtin 角色没有 Hanako agent，从 provider catalog 读默认模型"""
        import json
        from pathlib import Path
        catalog_path = Path.home() / ".hanako" / "provider-catalog.json"
        try:
            data = json.loads(catalog_path.read_text("utf-8"))
            providers = data.get("providers", {})
            # 优先用 agnes，其次第一个有 base_url 的 provider
            for prov_id in ["agnes"] + list(providers.keys()):
                prov = providers.get(prov_id, {})
                if prov.get("base_url") and prov.get("api_key"):
                    self._base_url = prov["base_url"]
                    self._api_key = prov["api_key"]
                    # 取第一个模型
                    models = prov.get("models", [])
                    if models:
                        m = models[0]
                        self._model = m.get("id", m) if isinstance(m, dict) else str(m)
                        self._max_context = m.get("context", 0) if isinstance(m, dict) else 0
                    self._api_type = prov.get("api", "openai-completions")
                    self._model_cfg = {"model": self._model}
                    logger.info("Builtin LLM from catalog: provider=%s model=%s", prov_id, self._model)
                    return
        except Exception as e:
            logger.warning("Failed to load default from catalog: %s", e)

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

    def chat_direct(self, message: str, inject_memory: bool = True, extra_context: str = "", tools: list = None) -> tuple:
        """直接调用 LLM API（不走 Hanako WS） - 原 chat() 的完整实现

        由 chat() 路由器在 Hanako 不可用或 transport_mode==direct 时调用。
        """
        if not self._base_url or not self._api_key:
            return "...(模型未配置,请在设置中配置模型)", "neutral"

        messages = [{"role": "system", "content": self._system_prompt + "\n\n[输出规则] 1. 回复简短自然，不超过 2 句话。2. 在回复中嵌入情绪标签，格式 [emotion:xxx]，可选值：happy/sad/angry/surprised/thinking/neutral/cute/missing。可以在句末或句中。例如：'你回来啦！[emotion:happy]' 或 '[emotion:thinking]让我想想……'"}]

        # 注入记忆
        if inject_memory:
            memory_text = self._context.build_memory_context(max_chars=self._memory_budget)
            if memory_text:
                messages.append({
                    "role": "system",
                    "content": f"[以下是你当前的记忆和状态,请自然参考--不要逐字复述,可以作为话题延续的线索]\n{memory_text}",
                })

        # 注入感知上下文(时间/情绪/日程)
        if extra_context:
            messages.append({
                "role": "system",
                "content": extra_context,
            })

        # 追加最近对话历史(最多 10 轮)
        for turn in self._history[-10:]:
            messages.append(turn)

        messages.append({"role": "user", "content": message.strip()})

        try:
            resp = self._call_api(messages, tools=tools)

            # 检查是否是 tool_calls 响应
            if isinstance(resp, dict) and resp.get("tool_calls"):
                # 保存用户消息到历史
                self._history.append({"role": "user", "content": message.strip()})
                return resp, None  # 返回 tool_calls 给调用方处理

            text = resp.strip() if resp and resp.strip() else ""

            # 兜底：检查 content 里是否包含 <function> 标签（非标准 tool calling）
            if text and tools:
                parsed = self._parse_function_in_content(text)
                if parsed:
                    logger.info("Parsed tool call from content (non-standard)")
                    self._history.append({"role": "user", "content": message.strip()})
                    return {"tool_calls": parsed, "message": {"content": text}}, None

            if not text:
                logger.warning("LLM returned empty: %s", repr(resp[:100] if resp else None))
                text = "(......想不起来要说什么了)"
                emotion = "thinking"
                self._history.append({"role": "user", "content": message.strip()})
                self._history.append({"role": "assistant", "content": text})
                return text, emotion

            # 解析情绪标签（匹配全文，支持多个，取最后一个）
            text, emotion = self.parse_emotion(text)

            # 保存到历史
            self._history.append({"role": "user", "content": message.strip()})
            self._history.append({"role": "assistant", "content": text})

            return text, emotion
        except requests.exceptions.Timeout:
            logger.warning("LLM timeout")
            return "(网络有点慢,你再说一遍?)", "neutral"
        except requests.exceptions.ConnectionError:
            logger.warning("LLM connection error")
            return "(连不上--检查一下网络配置吧)", "sad"
        except Exception as e:
            logger.warning("Chat failed: %s", e)
            return "(出了点岔子)", "neutral"

    def chat(self, message: str, inject_memory: bool = True, extra_context: str = "", tools: list = None) -> tuple:
        """入口路由 - 根据 transport_mode 选择 Hanako 或直连"""
        # direct 模式：跳过 Hanako
        if self.transport_mode == "direct":
            return self.chat_direct(message, inject_memory, extra_context, tools)

        # Hanako 模式：先尝试 Hanako，失败再考虑 fallback
        try:
            return self.chat_via_hanako(message, inject_memory, extra_context, tools)
        except HanakoUnavailableBeforeSend as e:
            logger.warning("Hanako 不可用（send 前）: %s", e)
            if self.transport_mode == "prefer_hanako":
                logger.info("Fallback -> chat_direct")
                return self.chat_direct(message, inject_memory, extra_context, tools)
            # hanako_only：不允许 fallback
            raise
        except HanakoUnavailableAfterSend as e:
            # 已交给 Hanako，绝不 fallback - 避免双执行
            logger.error("Hanako 已接收但未完成，不能 fallback: %s", e)
            return "(信号不太好,你再说一遍?)", "neutral"

    def chat_via_hanako(
        self,
        message: str,
        inject_memory: bool = True,
        extra_context: str = "",
        tools: list = None,
        timeout: float = None,
    ) -> tuple:
        """通过 Hanako WS Session 发送消息

        Fallback 边界：
        - send_and_wait() 之前失败 -> raise HanakoUnavailableBeforeSend（chat() 可 fallback）
        - send_and_wait() 之后失败 -> raise HanakoUnavailableAfterSend（绝不能 fallback）
        """
        if self._session_manager is None:
            raise HanakoUnavailableBeforeSend(
                "HanakoSessionManager 未注入（请检查 core/hanako_session_manager.py 是否存在）"
            )
        sm = self._session_manager
        if not hasattr(sm, "send_and_wait"):
            raise HanakoUnavailableBeforeSend("HanakoSessionManager 未实例化")
        if self._current_session is None:
            try:
                pinned = getattr(self, '_pinned_session_id', None)
                if pinned:
                    # 有钉住的 session，复用
                    self._current_session = sm.ensure_session(
                        agent_id=self.agent_id,
                        preferred_session_id=pinned
                    )
                else:
                    # 首次：为每个桌宠创建专属 session，避免多桌宠互相阻塞
                    self._current_session = sm.create_session(
                        agent_id=self.agent_id,
                    )
                self._pinned_session_id = getattr(self._current_session, 'session_id', None)
            except Exception as e:
                raise HanakoUnavailableBeforeSend("无法准备 Hanako Session") from e

        # 拼装 text：extra_context 作为前缀附加（Hanako 自己管记忆，inject_memory 被忽略）
        text = message.strip()
        if extra_context and extra_context.strip():
            text = f"[pet-context]\n{extra_context.strip()}\n[/pet-context]\n\n{text}"

        import time as _time
        max_retries = 3
        retry_delay = 2.0  # 秒
        for attempt in range(max_retries):
            try:
                result = sm.send_and_wait(
                    self._current_session,
                    text,
                    timeout=timeout if timeout is not None else self._reply_timeout,
                    display_text=message.strip(),
                    ui_context={"source": "oc-pet", "agentId": self.agent_id},
                )
                break  # 成功
            except HanakoUnavailableBeforeSend:
                raise
            except Exception as e:
                err_msg = str(e)
                if "pending turn" in err_msg.lower() and attempt < max_retries - 1:
                    logger.info("Session busy, retry %d/%d in %.1fs", attempt + 1, max_retries, retry_delay)
                    _time.sleep(retry_delay)
                    retry_delay *= 1.5  # 递增等待
                    continue
                logger.error("Hanako send_and_wait 异常: %s", e)
                raise HanakoUnavailableAfterSend(f"send_and_wait raised: {e}") from e

        if getattr(result, "error", None):
            raise HanakoUnavailableAfterSend(f"reply error: {result.error}")
        if getattr(result, "aborted", False):
            return "(对话被打断了)", "neutral"

        reply_text = (getattr(result, "text", "") or "").strip()
        cleaned, emotion = self.parse_emotion(reply_text)
        if not cleaned:
            cleaned = "…"
            if emotion == "neutral":
                emotion = "thinking"

        # Hanako 已经执行过 result.tool_calls，绝不能交给桌宠本地再执行一次。
        # 同步本地 history（向后兼容）
        try:
            self._history.append({"role": "user", "content": message.strip()})
            self._history.append({"role": "assistant", "content": cleaned})
            if len(self._history) > 40:
                self._history = self._history[-40:]
        except Exception:
            pass

        return cleaned, emotion

    @staticmethod
    def parse_emotion(text: str) -> tuple:
        """从文本解析 [emotion:xxx]，返回 (cleaned_text, emotion)

        全文匹配所有 [emotion:xxx]，取最后一个出现的 emotion。
        """
        if not text:
            return "", "neutral"
        em_matches = re.findall(r"\[emotion:(\w+)\]", text, flags=re.IGNORECASE)
        emotion = em_matches[-1].lower() if em_matches else "neutral"
        cleaned = re.sub(r"\s*\[emotion:\w+\]\s*", "", text, flags=re.IGNORECASE).strip()
        return cleaned, emotion

    def set_session(self, session_ref) -> None:
        """注入当前 Session 引用（PetManager / ConversationEngine 调用）"""
        self._current_session = session_ref
        self._pinned_session_id = getattr(session_ref, 'session_id', None) if session_ref else None

    def set_session_manager(self, manager) -> None:
        """注入 SessionManager 实例（覆盖延迟导入的类引用）"""
        self._session_manager = manager

    def _parse_function_in_content(self, text: str) -> list:
        """从 content 文本中解析 <function> 标签格式的工具调用

        支持格式：
            <function=tool_name>{"arg": "value"}</function>
            <function=name>args_json</function>
        """
        pattern = r'<function=([a-zA-Z0-9_-]+)[^>]*>(.*?)</function>'
        matches = re.findall(pattern, text, re.DOTALL)
        if not matches:
            return []

        tool_calls = []
        for name, args_str in matches:
            args_str = args_str.strip()
            try:
                json.loads(args_str)  # 验证 JSON
            except json.JSONDecodeError:
                args_str = '{}'

            tool_calls.append({
                "id": f"call_{name}_{len(tool_calls)}",
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": args_str,
                }
            })
        return tool_calls

    def _call_api(self, messages: list[dict], tools: list = None):
        """调用 LLM API

        支持两种 API 类型:
          - openai-completions: POST /chat/completions
          - anthropic-messages: POST /messages
        """
        if self._api_type == "anthropic-messages":
            return self._call_anthropic(messages)
        else:
            return self._call_openai(messages, tools=tools)

    def _call_openai(self, messages: list[dict], tools: list = None):
        """调用 OpenAI 兼容 API"""
        base = self._base_url.rstrip('/')
        # 自动补 /v1 前缀（如果用户填的是裸域名）
        if not base.endswith('/v1') and '/v1/' not in base:
            base += '/v1'
        url = f"{base}/chat/completions"
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
                **({"tools": tools} if tools else {}),
            },
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        choices = data.get("choices", [])
        if not choices:
            logger.warning("API returned no choices: %s", json.dumps(data, ensure_ascii=False)[:200])
            return ""

        message = choices[0].get("message", {})
        finish = choices[0].get("finish_reason", "")

        # 检查 tool_calls
        tool_calls = message.get("tool_calls")
        if tool_calls:
            logger.info("LLM requested %d tool call(s) | finish=%s", len(tool_calls), finish)
            return {"tool_calls": tool_calls, "message": message}

        content = message.get("content", "")
        if not content:
            logger.warning("API returned empty content | finish=%s | usage=%s", finish, data.get("usage", {}))
            return ""
        logger.info("API OK | finish=%s | usage=%s", finish, data.get("usage", {}))
        return content.strip()

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
