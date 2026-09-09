"""Harness adapter for OC Desktop Pet - Hanako 原生版。

从 Hanako 本体文件读取角色设定和模型配置:
  - identity.md / ishiki.md / description.md → 角色设定
  - provider-catalog.json → API 地址和密钥
  - memory/ → 记忆上下文注入

不再使用 skills/public/<角色>/SKILL.md 和 config.json 的独立配置。
"""
from __future__ import annotations

import collections
import json
import logging
import re
from pathlib import Path

import requests

from .hanako_context import HanakoContext
from .hanako_ws_client import HanakoUnavailableBeforeSend
from .usage_tracker import get_usage_tracker

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
        
        # UI优化: 注入桌宠视觉形象信息（让 AI 知道自己是桌宠）
        self._system_prompt += self._build_appearance_prompt()

        # 会话历史(内存)——有界 deque(maxlen=40)：append 原子（GIL），
        # 消除原 list + 读改写裁剪（self._history = self._history[-40:]）在
        # engine 后台线程与 idle_chatter 线程并发时的丢消息/交错问题（B2-7）。
        self._history: "collections.deque[dict]" = collections.deque(maxlen=40)

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
        self._current_session = None  # SessionRef | None (当前 agent 的)
        self._pinned_session_id = None  # 向后兼容：当前 agent 的 pin
        # M5: per-agent 会话保留 dict[agent_id -> session_id]（F3）
        # 切换 agent 时各自记住自己的 session，切回可续聊。
        self._agent_sessions: dict[str, object] = {}  # agent_id -> SessionRef
        self._agent_pinned: dict[str, str] = {}  # agent_id -> session_id

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

    def chat_direct(self, message: str, inject_memory: bool = True, extra_context: str = "", tools: list = None, source: str = "user") -> tuple:
        """直接调用 LLM API（不走 Hanako WS） - 原 chat() 的完整实现

        由 chat() 路由器在内部来源（proactive/idle/memory_extract/memory_reflect/
        screen_enrich）、Hanako 不可用或 transport_mode==direct 时调用。

        source 标记消息来源：user（用户主动）/ proactive（桌宠主动搭话）/
        idle（闲置闲聊）/ memory_extract（事实抽取）/ memory_reflect（反思）/
        screen_enrich（屏幕感知标注）。proactive/idle 消息会加 [source] 前缀，
        让 LLM 能区分说话人，避免把桌宠自己的主动文案当成用户消息计入上下文。

        历史写入：仅 ``user`` 来源写入 ``self._history``（本地上下文 deque）。
        内部来源（proactive/idle/memory_extract/memory_reflect/screen_enrich）
        不写历史——否则抽取/反思/主动文案会污染后续对话注入的上下文
        （``list(self._history)[-10:]``），QA 实测确认 memory 来源会污染。
        """
        if not self._base_url or not self._api_key:
            # 内部来源（proactive/idle/screen_enrich/memory_*）静默降级，不弹"模型未配置"
            if not self._records_history(source):
                return "", "neutral"
            return "...(模型未配置,请在设置中配置模型)", "neutral"

        # 内部来源静默：LLM 报错时不弹误导性文案给用户，只记日志、返回空串
        _user = self._records_history(source)
        def _err(msg: str, emotion: str = "neutral") -> tuple:
            return (msg, emotion) if _user else ("", "neutral")

        # 来源标记：proactive/idle 加 [source] 前缀；user 保持原样（不破坏现有 prompt 结构）
        user_content = message.strip()
        if source in ("proactive", "idle"):
            user_content = f"[{source}] {user_content}"

        messages = [{"role": "system", "content": self._system_prompt + "\n\n[输出规则] "
            "1. 回复简短自然，不超过 2 句话。"
            "2. 必须嵌入情绪标签，格式 [emotion:xxx]，可选值：happy/sad/angry/surprised/thinking/neutral/cute/missing。可以在句末或句中。例如：'你回来啦！[emotion:happy]' 或 '[emotion:thinking]让我想想……'"
            "3. 必须嵌入表情参数精确控制面部表情，格式 [expression:smile=80,eye_smile=50]。常用参数：smile(嘴型)/eye_smile(眯眼)/blush(脸红)/mouth_form(嘴型)/eye_open(眼睛开合)。数值范围：0.0-1.0（部分参数可负值）。示例：'今天好开心！[emotion:happy][expression:smile=90,blush=60]' 或 '哼，不理你。[emotion:sad][expression:mouth_form=-0.3]'"
            "4. 必须指定持续时间（秒），格式 [duration:3]。表情/动作将在指定秒后自动恢复 idle。示例：'晚安~[emotion:happy][expression:smile=70][duration:5]'"
            "5. 组合使用：[emotion:xxx] + [expression:xxx] + [duration:xxx] 必须同时使用，让桌宠的反应更生动。"
            "6. 注意：以上三个标签必须同时出现在回复中，缺一不可。"
            + self._build_action_prompt()}]

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

        # 追加最近对话历史(最多 10 轮)——deque 不支持切片，先转 list
        for turn in list(self._history)[-10:]:
            messages.append(turn)

        messages.append({"role": "user", "content": user_content})

        try:
            import time as _t
            _t0 = _t.monotonic()
            # P0: LLM 失败重试（指数退避 1s→2s→4s，最多 3 次）
            _max_retries = 3
            _retry_delay = 1.0
            resp = None
            for _attempt in range(_max_retries):
                try:
                    resp = self._call_api(messages, tools=tools)
                    break  # 成功，跳出重试
                except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as _retry_e:
                    if _attempt < _max_retries - 1:
                        logger.warning("LLM 调用失败 (attempt %d/%d): %s, %.1fs 后重试",
                                      _attempt + 1, _max_retries, type(_retry_e).__name__, _retry_delay)
                        _t.sleep(_retry_delay)
                        _retry_delay *= 2  # 指数退避
                    else:
                        raise  # 最后一次失败，抛出异常走 except 块
            _elapsed = _t.monotonic() - _t0
            # BugFix #4：显式记录慢 LLM 调用（>8s），便于区分"模型 inference 慢"
            # 与"prompt/build_context 拖慢"（用户体感"思考中卡 27s"的根因排查）。
            if _elapsed > 8.0:
                logger.warning(
                    "LLM 调用偏慢: %.1fs | model=%s | prompt_tokens≈%d | source=%s",
                    _elapsed, self._model,
                    sum(len(m.get("content") or "") for m in messages), source,
                )

            # 检查是否是 tool_calls 响应
            if isinstance(resp, dict) and resp.get("tool_calls"):
                # 保存用户消息到历史（仅用户真实对话）
                if self._records_history(source):
                    self._history.append({"role": "user", "content": user_content})
                return resp, None  # 返回 tool_calls 给调用方处理

            text = resp.strip() if resp and resp.strip() else ""

            # 兜底：检查 content 里是否包含 <function> 标签（非标准 tool calling）
            if text and tools:
                parsed = self._parse_function_in_content(text)
                if parsed:
                    logger.info("Parsed tool call from content (non-standard)")
                    if self._records_history(source):
                        self._history.append({"role": "user", "content": user_content})
                    return {"tool_calls": parsed, "message": {"content": text}}, None

            if not text:
                logger.warning("LLM returned empty: %s", repr(resp[:100] if resp else None))
                text = "(......想不起来要说什么了)"
                emotion = "thinking"
                if self._records_history(source):
                    self._history.append({"role": "user", "content": user_content})
                    self._history.append({"role": "assistant", "content": text})
                return text, emotion

            # 解析情绪标签（匹配全文，支持多个，取最后一个）
            text, emotion = self.parse_emotion(text)

            # 保存到历史（仅用户真实对话；内部来源不写，避免污染上下文）
            if self._records_history(source):
                self._history.append({"role": "user", "content": user_content})
                self._history.append({"role": "assistant", "content": text})

            return text, emotion
        except requests.exceptions.Timeout:
            logger.warning("LLM timeout")
            return _err("(网络有点慢,你再说一遍?)")
        except requests.exceptions.ConnectionError:
            logger.warning("LLM connection error")
            return _err("(连不上--检查一下网络配置吧)", "sad")
        except requests.exceptions.HTTPError as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status == 401:
                # 精确区分凭证来源，避免用户盲目改 .env
                _url = getattr(getattr(e, "response", None), "url", "") or ""
                _host = ""
                try:
                    from urllib.parse import urlparse
                    _host = (urlparse(_url).netloc or "").lower()
                except Exception:
                    _host = ""
                if "hanako" in _host or "xiaomimi" in _host or "token-plan" in _host:
                    logger.warning(
                        "LLM 401 — Hanako/TokenPlan 凭证失效，请重新登录 Hanako "
                        "刷新 server-info.json / provider-catalog.json"
                    )
                    return _err("(API 凭证失效了，重新登录一下 Hanako 就行)")
                logger.warning("LLM 401 — .env 的 LLM_API_KEY 失效，请更新 .env")
                return _err("(API 凭证失效了，检查下 .env 的 LLM_API_KEY)")
            if status == 429:
                logger.warning("LLM 429 Too Many Requests: %s", e)
                return _err("(模型有点忙，稍后再试)")
            logger.warning("LLM HTTP error: %s", e)
            return _err("(出了点岔子)")
        except Exception as e:
            logger.warning("Chat failed: %s", e)
            return _err("(出了点岔子)")

    @staticmethod
    def _records_history(source: str) -> bool:
        """是否把本轮写入本地 ``self._history``（后续对话会注入该上下文）。

        仅用户真实对话（source 为空/未指定/显式 "user"）写历史；
        内部来源（proactive/idle/memory_extract/memory_reflect/screen_enrich）
        不写——抽取/反思/主动文案作为系统指令发给 LLM，不应进入对话上下文。
        """
        return source in (None, "", "user")

    def chat(self, message: str, inject_memory: bool = True, extra_context: str = "", tools: list = None, source: str = "user") -> tuple:
        """入口路由 - 根据 transport_mode 和 source 选择路径

        P2: 返回完整回复（向后兼容）。如需流式，用 chat_stream()。

        - user 消息：走 Hanako session（工具、记忆、多轮）
        - 内部来源（proactive/idle/memory_extract/memory_reflect/screen_enrich）：
          直接走 LLM API（轻量快速，不占 session、不写本地 _history）
        """
        # 内部来源（非用户真实对话）：一律本地 LLM 直连，绝不进 Hanako session——
        # proactive/idle：主动搭话/闲置闲聊；
        # memory_extract/memory_reflect：事实抽取/反思（prompt 以 user 身份发进
        #   Hanako 会话会污染 display_text 历史，QA 实测确认）；
        # screen_enrich：屏幕感知语义化标注（pet.py 已显式走 chat_direct，设计意图如此）。
        # chat_direct 内部对非 user 来源不写 self._history（见 _records_history），
        # 避免抽取/反思/主动文案污染本地上下文（deque 会被注入后续对话）。
        if source in ("proactive", "idle", "memory_extract", "memory_reflect", "screen_enrich"):
            return self.chat_direct(message, False, extra_context, tools=None, source=source)

        # direct 模式：跳过 Hanako
        if self.transport_mode == "direct":
            return self.chat_direct(message, inject_memory, extra_context, tools)

        # Hanako 模式：先尝试 Hanako，失败再考虑 fallback
        logger.debug("chat() 路由: source=%s transport_mode=%s session_mgr=%s", source, self.transport_mode, self._session_manager is not None)
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
            return "…", "neutral"

    def chat_stream(self, message: str, inject_memory: bool = True, extra_context: str = "", tools: list = None, source: str = "user"):
        """P2: 流式调用 LLM API — 边生成边 yield chunks。

        返回 generator，每次 yield (chunk_text, emotion, action_intent)。
        emotion/action_intent 在解析到完整标签时更新（None = 尚未检测到）。

        用于 _process_message 边生成边触发情绪/动作，用户更早看到反应。

        路由逻辑：
        - 所有消息（包括 proactive）：优先走 Hanako session，失败再 fallback 到 LLM API
        - direct 模式：跳过 Hanako，直接走 LLM API
        """
        # direct 模式：跳过 Hanako
        if self.transport_mode == "direct":
            yield from self._chat_stream_direct(message, inject_memory, extra_context, tools, source)
            return

        # Hanako 模式：先尝试 Hanako，失败再考虑 fallback
        try:
            yield from self._chat_stream_via_hanako(message, inject_memory, extra_context, tools, source)
        except HanakoUnavailableBeforeSend as e:
            logger.warning("Hanako 不可用（send 前）: %s", e)
            if self.transport_mode == "prefer_hanako":
                logger.info("Fallback -> chat_stream_direct")
                yield from self._chat_stream_direct(message, inject_memory, extra_context, tools, source)
            else:
                yield "...", "neutral", None
        except HanakoUnavailableAfterSend as e:
            # 已交给 Hanako，绝不 fallback - 避免双执行
            logger.error("Hanako 已接收但未完成，不能 fallback: %s", e)
            yield "…", "neutral", None

    def _chat_stream_direct(self, message: str, inject_memory: bool = True, extra_context: str = "", tools: list = None, source: str = "user"):
        """直接走 LLM API 的流式调用（原 chat_stream 逻辑）"""
        if not self._base_url or not self._api_key:
            yield "...(模型未配置，请在设置中配置模型)", "neutral", None
            return

        # 构建 messages
        user_content = message.strip()
        if source in ("proactive", "idle"):
            user_content = f"[{source}] {user_content}"

        messages = [{"role": "system", "content": self._system_prompt + "\n\n[输出规则] "
            "1. 回复简短自然，不超过 2 句话。"
            "2. 必须嵌入情绪标签，格式 [emotion:xxx]，可选值：happy/sad/angry/surprised/thinking/neutral/cute/missing。可以在句末或句中。例如：'你回来啦！[emotion:happy]' 或 '[emotion:thinking]让我想想……'"
            "3. 必须嵌入表情参数精确控制面部表情，格式 [expression:smile=80,eye_smile=50]。常用参数：smile(嘴型)/eye_smile(眯眼)/blush(脸红)/mouth_form(嘴型)/eye_open(眼睛开合)。数值范围：0.0-1.0（部分参数可负值）。示例：'今天好开心！[emotion:happy][expression:smile=90,blush=60]' 或 '哼，不理你。[emotion:sad][expression:mouth_form=-0.3]'"
            "4. 必须指定持续时间（秒），格式 [duration:3]。表情/动作将在指定秒后自动恢复 idle。示例：'晚安~[emotion:happy][expression:smile=70][duration:5]'"
            "5. 组合使用：[emotion:xxx] + [expression:xxx] + [duration:xxx] 必须同时使用，让桌宠的反应更生动。"
            "6. 注意：以上三个标签必须同时出现在回复中，缺一不可。"
            + self._build_action_prompt()}]

        if inject_memory:
            memory_text = self._context.build_memory_context(max_chars=self._memory_budget)
            if memory_text:
                messages.append({"role": "system", "content": f"[以下是你当前的记忆和状态，请自然参考--不要逐字复述，可以作为话题延续的线索]\n{memory_text}"})

        if extra_context:
            messages.append({"role": "system", "content": extra_context})

        for turn in list(self._history)[-10:]:
            messages.append(turn)

        messages.append({"role": "user", "content": user_content})

        # 流式调用 API
        try:
            for chunk in self._call_api_stream(messages, tools=tools):
                yield chunk
        except Exception as e:
            logger.error("流式 API 调用失败: %s", e)
            yield "（嗯…让我缓一下）", "neutral", None

    def _chat_stream_via_hanako(self, message: str, inject_memory: bool = True, extra_context: str = "", tools: list = None, source: str = "user"):
        """通过 Hanako WS Session 发送消息（非流式，但模拟流式 yield）

        使用较短的超时（30 秒），超时后 raise HanakoUnavailableBeforeSend 让 chat_stream 走 fallback。
        """
        # 先调用 chat_via_hanako 获取完整回复，超时 30 秒
        reply, emotion = self.chat_via_hanako(
            message=message,
            inject_memory=inject_memory,
            extra_context=extra_context,
            tools=tools,
            source=source,
            timeout=30.0,  # 较短的超时，避免长时间等待
        )
        
        if not reply:
            raise HanakoUnavailableBeforeSend("Hanako 返回空回复")

        # 解析 emotion/action 标签
        action_intent = None
        if "[action:" in reply:
            import re
            import json as _json
            m = re.search(r'\[action:(\{[^}]*\})\]', reply)
            if m:
                try:
                    action_intent = _json.loads(m.group(1))
                except Exception:
                    pass

        # 模拟流式：把完整回复切成小段 yield
        chunks = []
        current = ""
        for char in reply:
            current += char
            if len(current) >= 10 or char in "。！？\n":
                chunks.append(current)
                current = ""
        if current:
            chunks.append(current)

        for chunk in chunks:
            yield chunk, reply, emotion, action_intent

        # 完成
        yield "", reply, emotion, action_intent

    def _call_api_stream(self, messages: list[dict], tools: list = None):
        """P2: 流式调用 OpenAI 兼容 API — yield chunks。

        返回 generator，每次 yield (chunk_text, accumulated_text, emotion, action_intent)。
        """
        base = self._base_url.rstrip('/')
        if not base.endswith('/v1') and '/v1/' not in base:
            base += '/v1'
        url = f"{base}/chat/completions"

        payload = {
            "model": self._model,
            "messages": messages,
            "temperature": 0.7,
            "stream": True,
            **({"tools": tools} if tools else {}),
        }

        resp = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            stream=True,
            timeout=60,
        )
        resp.raise_for_status()

        accumulated = ""
        emotion = "neutral"
        action_intent = None

        import json as _json
        for line in resp.iter_lines():
            if not line:
                continue
            # SSE 格式: data: {...}
            line_str = line.decode('utf-8')
            if not line_str.startswith('data:'):
                continue
            data_str = line_str[5:].strip()
            if data_str == '[DONE]':
                break
            try:
                data = _json.loads(data_str)
            except Exception:
                continue
            
            choices = data.get('choices', [])
            if not choices:
                continue
            delta = choices[0].get('delta', {})
            chunk_text = delta.get('content', '')
            
            if chunk_text:
                accumulated += chunk_text
                # 解析 emotion/action 标签
                emotion = self._parse_emotion_from_text(accumulated)
                action_intent = self._parse_action_from_text(accumulated)
                yield chunk_text, accumulated, emotion, action_intent

        # 完成
        yield '', accumulated, emotion, action_intent

    def _parse_emotion_from_text(self, text: str) -> str:
        """P2: 从流式文本中解析 emotion 标签"""
        import re
        m = re.search(r'\[emotion:([a-z]+)\]', text)
        if m:
            return m.group(1)
        return "neutral"

    def _parse_action_from_text(self, text: str) -> dict | None:
        """P2: 从流式文本中解析 action 标签"""
        import re
        import json as _json
        # 找最后一个完整的 [action:{...}]
        m = re.search(r'\[action:(\{[^}]*\})\]', text)
        if m:
            try:
                return _json.loads(m.group(1))
            except Exception:
                pass
        return None

    def chat_via_hanako(
        self,
        message: str,
        inject_memory: bool = True,
        extra_context: str = "",
        tools: list = None,
        timeout: float = None,
        source: str = "user",
    ) -> tuple:
        """通过 Hanako WS Session 发送消息

        Fallback 边界：
        - send_and_wait() 之前失败 -> raise HanakoUnavailableBeforeSend（chat() 可 fallback）
        - send_and_wait() 之后失败 -> raise HanakoUnavailableAfterSend（绝不能 fallback）

        source 通过 ui_context 透传给 Hanako（proactive/idle/user），
        同时保留 client="oc-pet" 标识来源客户端。
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
                aid = self.agent_id
                # F3: 优先复用该 agent 已 pin 的 session（切回续聊）
                pinned = self._agent_pinned.get(aid)
                if pinned:
                    # 有钉住的 session，复用
                    self._current_session = sm.ensure_session(
                        agent_id=aid,
                        preferred_session_id=pinned
                    )
                elif aid in self._agent_sessions:
                    # 内存里已有该 agent 的 session 引用，直接复用
                    self._current_session = self._agent_sessions[aid]
                else:
                    # 首次：为每个桌宠/agent 创建专属 session
                    self._current_session = sm.create_session(agent_id=aid)
                self._agent_sessions[aid] = self._current_session
                self._agent_pinned[aid] = getattr(self._current_session, 'session_id', None)
                self._pinned_session_id = self._agent_pinned.get(aid)
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
                    ui_context={"source": source, "client": "oc-pet", "agentId": self.agent_id},
                )
                break  # 成功
            except HanakoUnavailableBeforeSend:
                raise
            except Exception as e:
                err_msg = str(e)
                if "pending turn" in err_msg.lower() and attempt < max_retries - 1:
                    logger.info("Session busy, retry %d/%d in %.1fs", attempt + 1, max_retries, retry_delay)
                    # 重试仍在忙：可能是服务端 turn 清理卡住/“半个发送”挂了锁。
                    # 尝试强制中断当前 turn，释放服务端锁，避免 session 永久卡死
                    # （不中断则后续所有消息都会持续撞 busy）。
                    if attempt >= 1:
                        try:
                            self._session_manager.abort(self._current_session, "busy_reset")
                        except Exception:
                            pass
                    _time.sleep(retry_delay)
                    retry_delay *= 1.5  # 递增等待
                    continue
                logger.error("Hanako send_and_wait 异常: %s", e)
                raise HanakoUnavailableAfterSend(f"send_and_wait raised: {e}") from e

        if getattr(result, "error", None):
            # Bug A 兜底：turn 失败（超时/异常）但工具确实在云端执行了——
            # 用已记录的工具结果合成一句可显示的回复，避免用户只看到"…"。
            # 注意这不是 fallback 到本地 LLM（不会双执行），只是把云端工具结果上屏。
            synthesized = self._synthesize_reply_from_tools(
                getattr(result, "tool_calls", ()) or ()
            )
            if synthesized:
                logger.info("Hanako turn 失败但工具已执行，用工具结果兜底: %s", synthesized[:60])
                return synthesized, "neutral"
            raise HanakoUnavailableAfterSend(f"reply error: {result.error}")
        if getattr(result, "aborted", False):
            return "(对话被打断了)", "neutral"

        reply_text = (getattr(result, "text", "") or "").strip()
        cleaned, emotion = self.parse_emotion(reply_text)
        if not cleaned or cleaned in ("…", "...", "..."):
            # Bug A 修复：工具轮若最终文本为空/“…”（WS 没送达最终回复，或服务端
            # 工具链把最终文本放在 tool_end 的 details 里），用最后成功的工具结果
            # 合成一句有意义的回复，避免桌宠只显示“…”的空话。
            synthesized = self._synthesize_reply_from_tools(
                getattr(result, "tool_calls", ()) or ()
            )
            if synthesized:
                cleaned = synthesized
                if emotion == "neutral":
                    emotion = "happy"
        if not cleaned:
            cleaned = "…"
            if emotion == "neutral":
                emotion = "thinking"

        # 截断：桌宠只展示摘要（完整回复在 Hanako 主窗口可见）
        if len(cleaned) > 60:
            # 取第一个句子（。！？\n），或前 40 字
            for sep in ['\n', '。', '！', '？', '.', '!', '?']:
                idx = cleaned.find(sep, 10)  # 从第 10 字开始找，避免太短
                if 0 < idx < 60:
                    cleaned = cleaned[:idx + 1]
                    break
            else:
                cleaned = cleaned[:40] + "…"
            logger.info("Reply truncated for bubble: %s", cleaned[:50])

        # Hanako 已经执行过 result.tool_calls，绝不能交给桌宠本地再执行一次。
        # 同步本地 history（向后兼容）——deque(maxlen=40) 自动裁剪，无需读改写
        try:
            self._history.append({"role": "user", "content": message.strip()})
            self._history.append({"role": "assistant", "content": cleaned})
        except Exception:
            pass

        return cleaned, emotion

    @staticmethod
    def _synthesize_reply_from_tools(tool_calls) -> str:
        """工具轮最终文本缺失时，从工具执行结果合成一句可显示的回复。

        取最后一条成功（phase=end 且 success 非 False）的工具：
          - 优先用 tool_end 事件 details 里的结果文本（服务端常把工具输出放这里）
          - 其次用工具名生成"已完成「xx」"占位
        返回空串表示无可合成内容（调用方回退默认）。
        """
        if not tool_calls:
            return ""
        for tc in reversed(list(tool_calls)):
            if not isinstance(tc, dict):
                continue
            if tc.get("phase") == "end" and tc.get("success") is False:
                continue
            name = str(tc.get("name") or tc.get("tool") or "")
            details = tc.get("details")
            if isinstance(details, dict):
                text = str(details.get("text") or details.get("content") or "").strip()
                if text:
                    return text[:120]
            elif isinstance(details, str) and details.strip():
                return details.strip()[:120]
            if name:
                return f"已为你完成「{name}」"
        return ""

    @staticmethod
    def parse_emotion(text: str) -> tuple:
        """从文本解析 [emotion:xxx]，返回 (cleaned_text, emotion)

        全文匹配所有 [emotion:xxx]，取最后一个出现的 emotion。
        额外剥离：agent 思考/MOOD 块（[ Vibe: ... ] / 纯文本 "Vibe:" 等），
        避免气泡显示内部思考。
        """
        if not text:
            return "", "neutral"
        # 支持 [emotion:xxx] / [emotion=xxx] / [emotion: xxx] / [ emotion : xxx ] 等 LLM 常见变体
        em_matches = re.findall(r"\[\s*emotion\s*[:=]\s*(\w+)\s*\]", text, flags=re.IGNORECASE)
        emotion = em_matches[-1].lower() if em_matches else "neutral"
        cleaned = re.sub(r"\s*\[\s*emotion\s*[:=]\s*\w+\s*\]\s*", " ", text, flags=re.IGNORECASE)
        # 剥离 [expression:xxx] 标签（表情参数，不显示给用户）
        cleaned = re.sub(r"\s*\[expression:[^\]]*\]\s*", " ", cleaned, flags=re.IGNORECASE)
        # 剥离 [duration:xxx] 标签（持续时间，不显示给用户）
        cleaned = re.sub(r"\s*\[duration:[^\]]*\]\s*", " ", cleaned, flags=re.IGNORECASE)
        # BugFix #4：整段剥离 <mood>...</mood> 内省块（Vibe/Reflections/Will/
        # Sparks 字段是给服务端/记忆用的元数据，不是给用户的回复）——必须在
        # HTML 剥离之前做，否则 <mood> 标签被剥掉后只剩 Vibe 文本。
        cleaned = re.sub(r"<mood>.*?</mood>", " ", cleaned, flags=re.IGNORECASE | re.DOTALL)
        # 剥离 agent 思考/MOOD 块：以 [ Vibe:/Sparks:/Reflections:/Will: 开头的成块内容。
        # 先剥闭合块（到 ] 为止，可跨行），再剥未闭合残余（到文本末尾）。
        # 注意不能依赖 MULTILINE 的 $ 作边界（会在块内第一行行尾提前停下）。
        cleaned = re.sub(
            r"^\[\s*(?:Vibe|Sparks|Reflections|Will)\b[\s\S]*?\]",
            "", cleaned, flags=re.IGNORECASE | re.MULTILINE | re.DOTALL
        )
        cleaned = re.sub(
            r"^\[\s*(?:Vibe|Sparks|Reflections|Will)\b[\s\S]*$",
            "", cleaned, flags=re.IGNORECASE | re.MULTILINE | re.DOTALL
        ).strip()
        # BugFix #4：剥离纯文本（无方括号）形式的 mood 前缀块——LLM 输出或
        # 历史恢复常以 "Vibe: ..." / "Reflections: ..." / "Will: ..." /
        # "Sparks: ..." 开头（一行或多行），全部剥到该段末尾，不上气泡。
        cleaned = re.sub(
            r"(?im)^\s*(?:Vibe|Sparks|Reflections|Will)\s*[:：][^\n]*(?:\n|$)",
            "", cleaned,
        )
        # 剥离表情包 XML 段：<parameter name=...>值</parameter> 整段剥掉
        cleaned = re.sub(r"<parameter[^>]*>.*?</parameter>", "", cleaned, flags=re.S)
        # 剥离其余残留标签（<brioqingbao_express> 等）
        cleaned = re.sub(r"<[^>]+>", "", cleaned)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
        return cleaned, emotion

    def set_session(self, session_ref) -> None:
        """注入当前 Session 引用（PetManager / ConversationEngine 调用）"""
        self._current_session = session_ref
        sid = getattr(session_ref, 'session_id', None) if session_ref else None
        self._pinned_session_id = sid
        # 按 agent 维度记录，供切回续聊
        if sid and self.agent_id:
            self._agent_sessions[self.agent_id] = session_ref
            self._agent_pinned[self.agent_id] = sid

    def switch_agent(self, agent_id: str) -> bool:
        """切换对话后端 agent（F2/F4）。

        - 更新 self.agent_id
        - 尝试恢复该 agent 已 pin 的 session（切回续聊）
        - 若该 agent 已有历史，清空本地 _history（避免串味）
        - 不动本地显示角色（立绘/皮肤）

        Returns:
            True 切换成功；False 参数非法（空 agent_id）
        """
        if not agent_id or not str(agent_id).strip():
            return False
        agent_id = str(agent_id).strip()
        if agent_id == self.agent_id:
            return True  # 已是目标 agent，无需切换
        self.agent_id = agent_id
        # 切走时把当前 session 存好（已在 _agent_sessions 里）
        # 恢复目标 agent 的 session（若有）
        self._current_session = self._agent_sessions.get(agent_id)
        self._pinned_session_id = self._agent_pinned.get(agent_id)
        # 切换后本地历史是上一 agent 的，清空防串味（服务端会话各自独立）
        self._history.clear()
        logger.info("adapter 切换 agent: %s (恢复session=%s)",
                    agent_id, self._pinned_session_id or "无")
        return True

    def set_session_manager(self, manager) -> None:
        """注入 SessionManager 实例（覆盖延迟导入的类引用）"""
        self._session_manager = manager
        logger.info("SessionManager 已注入 adapter (agent=%s)", self.agent_id)

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
    
    def _build_appearance_prompt(self) -> str:
        """UI优化: 注入桌宠视觉形象信息，让 AI 知道自己是桌宠
        
        包含：
        - 角色名称
        - 模型类型（Live2D/精灵）
        - 外观描述
        - 能力说明
        
        检查两个位置：
        1. 项目内置：characters/<agent_id>/
        2. 用户自定义：~/.hanako/agents/<agent_id>/pet/
        """
        try:
            from pathlib import Path
            
            # 检查两个位置
            char_dir_builtin = Path(__file__).parent.parent / "characters" / self._agent_id
            char_dir_custom = Path.home() / ".hanako" / "agents" / self._agent_id / "pet"
            
            appearance_info = []
            
            # 1. 角色名称
            appearance_info.append(f"【桌宠形象】你是桌宠「{self._agent_id}」。")
            
            # 2. 模型类型（优先检查内置，再检查自定义）
            model_type_found = False
            
            # 检查内置目录
            if char_dir_builtin.exists():
                if (char_dir_builtin / "live2d").exists():
                    appearance_info.append("- 模型类型：Live2D 动态模型")
                    appearance_info.append("- 你可以做出丰富的表情和动作")
                    model_type_found = True
                elif (char_dir_builtin / "frames").exists():
                    appearance_info.append("- 模型类型：精灵图序列")
                    appearance_info.append("- 你可以做出基本的表情和动作")
                    model_type_found = True
            
            # 检查用户自定义目录
            if not model_type_found and char_dir_custom.exists():
                if (char_dir_custom / "live2d").exists():
                    appearance_info.append("- 模型类型：Live2D 动态模型（用户自定义）")
                    appearance_info.append("- 你可以做出丰富的表情和动作")
                    model_type_found = True
                elif (char_dir_custom / "frames").exists():
                    appearance_info.append("- 模型类型：精灵图序列（用户自定义）")
                    appearance_info.append("- 你可以做出基本的表情和动作")
                    model_type_found = True
            
            # 如果都没找到，说明是纯文本角色（无模型）
            if not model_type_found:
                appearance_info.append("- 模型类型：文本角色（无视觉模型）")
                appearance_info.append("- 你通过气泡和语音与用户互动")
            
            # 3. 能力说明
            appearance_info.append("- 你可以看到用户的屏幕内容")
            appearance_info.append("- 你可以在桌面上移动、被拖拽")
            appearance_info.append("- 你可以弹出气泡说话、播放语音")
            if model_type_found:
                appearance_info.append("- 你可以做出各种动作（眨眼、挥手、跳跃等）")
            
            # 4. 提醒 AI 不要把自己当成普通 AI
            appearance_info.append("- 注意：你是桌宠角色，不是普通聊天 AI。用角色的语气说话。")
            appearance_info.append("- 当用户提到「动漫形象」「角色」「模型」时，你就是那个形象本身。")
            
            return "\n".join(appearance_info)
        except Exception as e:
            logger.debug("Failed to build appearance prompt: %s", e)
            return ""
    
    def _build_action_prompt(self) -> str:
        """构建动作列表 prompt（注入到 system prompt 输出规则）。

        让 LLM 知道有哪些动作/表情/持续时间可以调用，格式：
        [action:{"gesture":"touch","intensity":0.6}]
        [expression:smile=80,eye_smile=50]
        [duration:3]
        
        P1: 隐式文档按需注入 — 只放简短列表，AI 用了才 Poke 完整文档。
        省 token 且渐进式学习。
        """
        try:
            renderer = getattr(self, '_renderer', None) or getattr(self, '_pet_renderer', None)
            actions_prompt = ""
            if renderer and hasattr(renderer, 'available_actions'):
                actions = renderer.available_actions
                if actions:
                    action_names = [a['name'] for a in actions]
                    actions_prompt = (
                        "\n3. 可在回复中嵌入动作标签触发桌宠动作，格式 [action:{...}]"
                        "\n可用动作：" + "/".join(action_names) +
                        "\n提示：当用户描述场景或情绪时，主动配合动作让互动更生动。"
                    )
            # 表情参数控制（新增）
            expression_prompt = (
                "\n4. 可嵌入表情参数精确控制面部表情，格式 [expression:smile=80,eye_smile=50]"
                "\n常用参数：smile(嘴型)/eye_smile(眯眼)/blush(脸红)/mouth_form(嘴型)/eye_open(眼睛开合)"
                "\n数值范围：0.0-1.0（部分参数可负值，如 mouth_form=-0.3 表示撇嘴）"
                "\n提示：[expression] 比 [emotion] 更精细，适合特定场景（如脸红、俏皮嘴型）"
            )
            duration_prompt = (
                "\n5. 可指定持续时间（秒），格式 [duration:3]"
                "\n提示：表情/动作将在指定秒后自动恢复 idle，不用 duration 则持续直到下次变化"
            )
            return actions_prompt + expression_prompt + duration_prompt
        except Exception:
            return ""

    def get_action_documentation(self, action_name: str) -> str:
        """P1: 获取单个动作的完整文档（按需注入）。

        当 AI 使用了某个动作标签时，返回该动作的完整说明。
        """
        try:
            renderer = getattr(self, '_renderer', None) or getattr(self, '_pet_renderer', None)
            if not renderer or not hasattr(renderer, 'available_actions'):
                return ""
            actions = renderer.available_actions
            for a in actions:
                if a['name'] == action_name:
                    example = f'[action:{{"gesture":"{action_name}","intensity":0.7}}]'
                    return (
                        f"动作: {a['name']} ({a['label']})\n"
                        f"说明: {a.get('description', '无')}\n"
                        f"强度范围: 0.0-1.0（默认 0.7）\n"
                        f"示例: {example}"
                    )
            return ""
        except Exception:
            return ""

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
        
        # 记录 Token 使用量
        usage = data.get("usage", {})
        if usage:
            try:
                tracker = get_usage_tracker()
                tracker.record_usage(
                    prompt_tokens=usage.get("prompt_tokens", 0),
                    completion_tokens=usage.get("completion_tokens", 0),
                    source="chat",
                    model=self._model,
                )
            except Exception as e:
                logger.debug("Usage tracking failed: %s", e)
        
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
