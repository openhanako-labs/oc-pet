"""Harness adapter for OC Desktop Pet - Hanako 原生版。

从 Hanako 本体文件读取角色设定和模型配置:
  - identity.md / 意识文件(AGENTS.md→ishiki.md→awareness.md) / description.md → 角色设定
  - provider-catalog.json → API 地址和密钥
  - memory/ → 记忆上下文注入

不再使用 skills/public/<角色>/SKILL.md 和 config.json 的独立配置。
"""
from __future__ import annotations

import collections
import contextlib
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

    def set_pet_memory(self, text: str) -> None:
        """把桌宠本体记忆（FactStore 渲染结果）推给记忆注入咽喉点。

        2026-09-21：读取侧合并——桌宠的事实库不进 Hanako 的 .md，也不反过来
        迁移，而是由 pet 侧在 FactStore 变化时推到这里，``build_memory_context``
        把它当额外一段（【桌宠】）。未推送过 → 该段不出现，行为与旧版一致。
        """
        try:
            self._context.set_pet_memory(text)
        except Exception as e:  # 绝不因记忆推送打断对话
            logger.debug("set_pet_memory 失败（非致命）: %s", e)

    def set_atmosphere(self, text: str) -> None:
        """把氛围累积层的渲染结果推给记忆注入咽喉点（【氛围】段）。

        2026-09-21：与 ``set_pet_memory`` 同一套路子——氛围层只算出"该说什么"，
        真正进 prompt 由 ``build_memory_context`` 的统一分段预算负责。
        未推送 / 推空串 → 该段不出现，行为与旧版一致。
        """
        try:
            self._context.set_atmosphere(text)
        except Exception as e:  # 绝不因氛围推送打断对话
            logger.debug("set_atmosphere 失败（非致命）: %s", e)

    def set_life_cursor(self, text: str) -> None:
        """把生活游标的近况推给记忆注入咽喉点（【近况】段）。

        2026-09-21：同 ``set_atmosphere``。素材是事件流（带时间轴），由
        ``core/life_cursor.py`` 压成一句话。未推送 / 推空串 → 该段不出现。
        """
        try:
            self._context.set_life_cursor(text)
        except Exception as e:  # 绝不因近况推送打断对话
            logger.debug("set_life_cursor 失败（非致命）: %s", e)

    def render_life_cursor(self, prompt: str) -> str:
        """用 utility 模型把事件流简报润成一句近况。

        走 ``chat_direct(source="life_cursor")`` —— 该来源在 ``_UTILITY_SOURCES``
        与 ``_DIRECT_SOURCES`` 里都有，因此不抢主对话配额、也绝不进 Hanako 会话。
        拿不到文字 → 回空串，调用方退化为确定性 brief（这一层不静默消失）。
        """
        try:
            text, _emotion = self.chat_direct(
                prompt, inject_memory=False, extra_context="", source="life_cursor",
            )
            return str(text or "").strip()
        except Exception as e:  # 非致命：没有文字还有确定性 brief
            logger.debug("render_life_cursor 失败（退化为 brief）: %s", e)
            return ""

    def render_atmosphere(self, prompt: str, *, timeout_note: str = "") -> str:
        """用 utility 模型把氛围数值渲染成一句自然语言（1-2 句）。

        为什么单独包一层：氛围层的触发是**低频**的（实测约 8% 的轮次），
        但它需要一次额外模型调用。走 ``chat_direct(source="atmosphere")``
        ——该来源已列入 ``_UTILITY_SOURCES``，会路由到用户配置的 utility 模型，
        不抢主对话配额。失败 / 未配置 utility → 回空串，由调用方退化为数值直陈。
        """
        try:
            text, _emotion = self.chat_direct(
                prompt, inject_memory=False, extra_context="", source="atmosphere",
            )
            return str(text or "").strip()
        except Exception as e:  # 非致命：没有文字还有数值兜底
            logger.debug("render_atmosphere 失败（退化为数值）: %s", e)
            return ""

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

        # 2026-09-17：后台任务专用模型（Hana preferences 的 utility_model）。
        #
        # 五个内部来源（screen_enrich / proactive / idle / memory_extract /
        # memory_reflect）此前与用户对话共用 models.chat —— 实测造成 429
        # 限流（屏幕感知占 73% LLM 调用，与用户消息同时段抢配额）。
        #
        # Hana 设置页已有 utility_model 字段专供这类用途，oc-pet 此前不读。
        # 未配置时为空 dict → chat_direct 回退对话模型（保持旧行为）。
        #
        # 动态生效：_refresh_utility_cfg 用 preferences.json 的 mtime 做失效
        # 检测，用户改完设置页立即生效，不用重启桌宠（与 screen.py 的
        # 视觉配置行为对齐——那条是每次截屏都读）。
        self._utility_cfg: dict = {}
        self._utility_cfg_mtime = "__unset__"
        self._refresh_utility_cfg()
        if self._utility_cfg:
            logger.info(
                "后台任务模型（utility_model）: %s",
                self._utility_cfg.get("model"),
            )

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
        # 2026-09-20 事故后：记录「哪些 pin 是桌宠自己建的会话」。
        # 只有 create_session 拿到的才写。用来在恢复时区分
        # 「桌宠专属会话」与「ensure_session 兜底漂移来的助手主对话」。
        self._owned_sessions: dict[str, str] = {}  # agent_id -> session_id
        # 2026-09-20（用户要求 A）：**从磁盘恢复 pin**。
        # 原实现 `_agent_pinned` 是纯内存 dict，重启即丢——于是每次重启
        # 桌宠都新建一个 session（实测日志里 5 个不同 sess_xxx）。
        # 后果：上下文不连贯（每轮从零开始）+ Hana 会话列表被碎片灌满。
        self._load_pinned_sessions()

    # ── 会话 pin 持久化（2026-09-20，用户要求 A）──────────────
    #
    # 目标：桌宠无论是否重启，都在**同一个固定会话**里回复。
    # 存储位置沿用桌宠已有的 `~/.hanako/pets/`（greet_*.json 同目录）。

    def _pinned_path(self):
        """pin 落盘路径：~/.hanako/pets/session_<agent>.json"""
        try:
            from hanako_home import hanako_home
            d = hanako_home() / "pets"
        except Exception:
            import os as _os
            d = __import__("pathlib").Path(_os.path.expanduser("~")) / ".hanako" / "pets"
        return d / f"session_{self.agent_id}.json"

    def _load_pinned_sessions(self) -> None:
        """启动时从磁盘恢复 pin（失败静默——不阻断启动）。

        ⚠️ 2026-09-20 事故后的**归属校验**：

        仅凭磁盘上的 session_id 不足以信任——那条 pin 可能是
        `ensure_session` 兜底漂移的残留（指向助手主对话）。

        本方法只做**静态**校验（不联网）：确认 pin 的形状合法。
        真正是否仍存在/是否被换成主对话，由 `_validate_pin_async()`
        在后台向 Hana 核对（不阻塞启动）。
        """
        try:
            import json
            p = self._pinned_path()
            if not p.exists():
                return
            data = json.loads(p.read_text(encoding="utf-8"))
            pinned = data.get("pinned") or {}
            if isinstance(pinned, dict):
                # 只收非空字符串，防脏数据
                self._agent_pinned.update(
                    {k: v for k, v in pinned.items() if isinstance(v, str) and v})
            owned = data.get("owned") or {}
            if not hasattr(self, "_owned_sessions"):
                self._owned_sessions = {}
            if isinstance(owned, dict):
                self._owned_sessions.update(
                    {k: v for k, v in owned.items() if isinstance(v, str) and v})
            sid = self._agent_pinned.get(self.agent_id)
            if sid:
                self._pinned_session_id = sid
                logger.info("[session] 已从磁盘恢复 pin: agent=%s session=%s",
                            self.agent_id, sid)
                # 归属校验（丢弃无标记的 pin = 漂移残留）
                self._validate_pin_async()
        except Exception:
            logger.debug("harness_adapter: 恢复会话 pin 失败", exc_info=True)

    def _validate_pin_async(self) -> None:
        """核对 pin：它必须是**桌宠自己建的**那个会话。

        ## 为什么不用「消息数/标题」当判据

        最初我想用「title 非空 或 messageCount>0 → 脏」来识别漂移。
        **那是错的**：桌宠自己的会话在它聊过之后 messageCount 也会 >0。
        这会误杀合法 pin。（同一个坑：用表象当判据。）

        ## 正确判据：归属标记

        pin 文件里记 `owned` —— 只在**桌宠自己调 create_session / set_session**
        拿到会话时写入。恢复时：

          - 有 `owned` 标记且指向同一会话 → 信任
          - 无标记（遗留/来路不明）→ 丢弃，下次新建
          - 指向的会话已不存在 → 丢弃

        精确，无启发式。旧代码写的 pin 没有标记，会被当作不可信
        —— 正好清掉那次漂移的残留。

        ## 时序

        归属检查是**纯本地数据**（只读 `_owned_sessions`），所以
        **同步**做——否则 `chat_via_hanako` 可能抢在校验前用上脏 pin。
        “会话是否仍存在”才需要联网，放后台。
        """
        sid = self._agent_pinned.get(self.agent_id)
        if not sid:
            return

        # ── 同步：归属标记（不联网）──
        if self._owned_sessions.get(self.agent_id) != sid:
            logger.warning(
                "[session] ★ 丢弃无归属标记的 pin: %s（agent=%s）"
                "——它可能是漂移残留。下次对话新建专属会话。",
                sid, self.agent_id)
            self._agent_pinned.pop(self.agent_id, None)
            self._owned_sessions.pop(self.agent_id, None)
            self._pinned_session_id = None
            self._current_session = None
            self._save_pinned_sessions()
            return

        # ── 异步：会话是否仍存在（需联网）──
        def _check():
            try:
                from core.hana_client import HanaClient
                rows = HanaClient.from_env().list_sessions(
                    agent_id=self.agent_id) or []
            except Exception as e:  # noqa: BLE001
                logger.debug("pin 存在性校验跳过: %s", e)
                return
            for s in rows:
                get = (s.get if isinstance(s, dict)
                       else (lambda k, d=None: getattr(s, k, d)))
                if get("sessionId") == sid:
                    logger.info("[session] pin 校验通过: %s（桌宠专属）", sid)
                    return
            logger.warning("[session] ★ pin 指向的会话已不存在: %s——丢弃", sid)
            self._agent_pinned.pop(self.agent_id, None)
            self._owned_sessions.pop(self.agent_id, None)
            self._pinned_session_id = None
            self._current_session = None
            self._save_pinned_sessions()

        import threading
        t = threading.Thread(target=_check, daemon=True, name="pin-validate")
        t.start()
        # 2026-09-21：把线程交出去，调用方（尤其是测试）才能 join。
        #
        # 原先它是个**孤儿**，而且两个毛病都从这一个点长出来：
        #   ① 测试结束、monkeypatch 已撤销之后它才调 `_save_pinned_sessions()`，
        #      于是落到了**真实**的 `~/.hanako/pets/` 上（实测把真 pin 从
        #      189B 擦成 73B）；
        #   ② 更阴的是它会落进**下一个测试**当时正被 monkeypatch 的路径里，
        #      把那个测试刚写好的 pin 冲掉 —— 全量回归里偶发的
        #      `assert None == 'sess_ROUNDTRIP'` 就是它。
        # 异步本身没错，错的是“没人拿得住它”。
        return t

    def _save_pinned_sessions(self) -> None:
        """把 pin 写回磁盘（失败静默——不因写盘失败影响对话）。"""
        try:
            import json
            p = self._pinned_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(
                json.dumps({"pinned": self._agent_pinned,
                            "owned": getattr(self, "_owned_sessions", {}),
                            "updated_at": __import__("time").time()},
                           ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            logger.debug("harness_adapter: 保存会话 pin 失败", exc_info=True)

    def _load_default_from_catalog(self):
        """builtin 角色没有 Hanako agent，从 provider catalog 读默认模型"""
        import json
        from hanako_home import hanako_home
        catalog_path = hanako_home() / "provider-catalog.json"
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
            self.agent_id, self._model, self._base_url[:40] + "..." if self._base_url else "N/A",
            len(self._system_prompt),
        )

    @property
    def system_prompt(self) -> str:
        return self._system_prompt

    @property
    def model_config(self) -> dict:
        return dict(self._model_cfg)

    # ── 输出规则（唯一来源）───────────────────────────────
    # 2026-09-10：原实现只在 chat_direct / _chat_stream_direct 里内联这段规则，
    # 而实际运行走 chat_via_hanako（transport_mode=prefer_hanako）——标签契约从未送达。
    # 实测：14 次回复，0 次带 [emotion:]，14 次全部走兜底表。
    #
    # 2026-09-10 二次收敛：原四个标签（emotion / action / expression / duration）
    # 职责重叠且要求「必须同时出现，缺一不可」，模型面对「该写哪个」选择了全不写。
    # 改为一个必给 + 一个可选：
    #   [feel:v,a]  — 连续 VA 坐标，直驱渲染器已有的逐帧插值层（_va_target→_va_cur）
    #   [do:名称]   — 可选，明确要一个动作时
    # 旧标签仍被解析（向后兼容与兜底路径），但不再要求模型输出。
    #
    # 2026-09-20 重新引入 [emotion:] —— **这次是必填，且只加这一个**。
    #
    # 背景：VA 二维坐标承载不了中文情绪细粒度（正价区全塌成 happy，
    # 「记得让眼睛歇一歇」被判开心，实际是关心）。此前的补救是**在桌宠本地
    # 再加一层 embedding 分类器读回复文本反推情绪**——但那是让本地小模型
    # 重做一遍主 LLM 已经做过的事，且它更弱（桌宠视角实测仅 67%）。
    #
    # 正确分工：**读懂对话是主 LLM 的活**（它本来就在读，且比本地小模型强得多），
    # 本地小模型只做「已知情绪 → 从候选里挑具体表情」的窄判断（实测 10/10）。
    # 所以让主 LLM 直接多吐一个情绪词，分类器降级为兜底。
    #
    # 为什么这次敢加（历史上「加可选标签」被数据否决过两次）：
    #   - 2026-09-10 的失败是**四个标签全必填**（职责重叠，模型全不写）
    #   - 本次是**两个必填**（feel + emotion），职责不重叠：
    #       一个给连续坐标，一个给离散类别
    #   - 实测对照（tools/verify_emotion_contract.py，本地 Qwen2.5-1.5B，6 语境）：
    #       契约          写 feel   写 emotion
    #       A 只 feel      2/6        0/6      ← 现状
    #       B 双必填       4/6        4/6      ← 选它
    #       C 可选追加     3/6        0/6      ← 证明「可选」确实没人写
    #   - **向后兼容**：主 LLM 若不写，emotion 退回 neutral → 分类器兜底
    #     → 行为与改动前完全一致。零风险。
    _OUTPUT_RULES = (
        "1. 回复简短自然，不超过 2 句话。"
        "2. 必须给出情绪坐标，格式 [feel:valence,arousal]。"
        "valence ∈[-1,1]：-1 很消极（难过/生气），0 中性，+1 很积极（开心/温暖）。"
        "arousal ∈[-1,1]：-1 很平静（放松/低落），0 一般，+1 很兴奋（激动/紧张）。"
        "两者独立判断。例：[feel:0.8,0.7] 开心兴奋；[feel:-0.5,-0.4] 低落安静；"
        "[feel:-0.6,0.8] 生气激动；[feel:0.2,0.1] 平静。拿不准就写 [feel:0,0]。"
        "3. 必须给出情绪词，格式 [emotion:情绪词]。"
        "从这些里选一个：happy（开心）/ sad（难过）/ angry（生气）/ "
        "surprised（惊讶）/ thinking（思考）/ confused（疑惑）/ shy（害羞）/ "
        "cute（撒娇）/ sleepy（困）/ neutral（平静）。"
        "情绪词描述**角色自己的感受**（不是用户的）。"
        "例：[emotion:happy] [emotion:sleepy]。拿不准就写 [emotion:neutral]。"
    )

    # 输出交给机器读的来源：不得注入标签规则（否则污染其结构化输出）
    _NON_DISPLAY_SOURCES = frozenset({
        "memory_extract", "memory_reflect", "screen_enrich",
    })

    # 走后台任务模型（utility_model）的来源 —— 与上面那个集合**不同**。
    #
    # 这里关心的是“谁在消耗对话配额”，而非“输出给谁看”：
    #   - screen_enrich / memory_extract / memory_reflect：机器读的，高频
    #   - proactive / idle：给用户看的，但**不是用户主动发起的**，
    #     且由定时器触发（屏幕感知、闲置），实测占调用量大头
    #   - user：用户真实对话，**永远**用 models.chat
    #
    # 2026-09-17：这五个来源原先全部与用户对话共用 models.chat，
    # 与用户消息抢同一份配额（429 的主因）。现在改走 Hana 的 utility_model。
    _UTILITY_SOURCES = frozenset({
        "memory_extract", "memory_reflect", "screen_enrich",
        "proactive", "idle",
        # 2026-09-21：氛围累积层的低频渲染（实测约 8% 的轮次才触发）
        "atmosphere",
        # 2026-09-21：生活游标（事件流 → 一句近况），默认每小时最多一次
        "life_cursor",
    })

    #: 内部来源：一律本地 LLM 直连，**绝不进 Hanako session**。
    #
    # 与 `_UTILITY_SOURCES` 是**两个不同的轴**（前者问「走不走主对话会话」，
    # 后者问「谁在消耗对话配额」），所以刻意不复用同一个集合——
    # 否则以后往任一个轴加来源，都会静默地改变另一个轴的行为。
    #
    # 2026-09-21：原先这个清单是写在 `chat()` 里的一行硬编码元组，新增来源
    # （atmosphere）时必须记得同步改两处，漏一处就会把内部调用打进真实会话。
    # 提取成具名常量，并把 atmosphere 一次补上。
    _DIRECT_SOURCES = frozenset({
        "proactive", "idle", "memory_extract", "memory_reflect",
        "screen_enrich", "atmosphere", "life_cursor",
    })

    def _output_rules(self) -> str:
        """输出规则全文（含当前角色可用动作清单）。"""
        return self._OUTPUT_RULES + self._build_action_prompt()

    def _inject_rules_in_text(self) -> bool:
        """是否把输出规则注入用户消息正文。

        默认 **False**（2026-09-16 起）：规则改由 agent 的 AGENTS.md 承担，
        进 system 层，用户消息保持干净——会话标题与历史不再被规则污染。

        回退开关：config 里 ``dialog.inject_output_rules_in_text = true``
        可恢复旧行为（给未配置 AGENTS.md 的 agent 兜底）。

        ## 2026-09-20 修复：这个开关以前是**死的**

        原实现只读 `getattr(self, "_config", None)`，而 `_config`
        **在本类里从来没有被赋值过**（全仓搜 `_config =` 无赋值点，
        只有三处 `getattr(self, "_config", None)` 读）。
        所以无论 config.json 写什么，它永远拿到 None → 永远返回 False。

        实证：2026-09-20 日志里 ``标签检测: feel=False emotion=False``
        贯穿全程——契约从未送达 Hana，而注释声称“由 AGENTS.md 承担”，
        实际 AGENTS.md 里一条规则都没有。两头落空。

        现在三级回退：显式注入的 `_config` → `load_config()` 读盘。
        仍然默认 False（不改变 09-16 的默认行为），只是让开关真的能开。
        """
        try:
            cfg = getattr(self, "_config", None)
            if not isinstance(cfg, dict):
                # `_config` 从未被赋值（见 docstring）——回退到读盘。
                # 只在这里读：本方法每次发消息才调一次，开销可忽略。
                try:
                    from config import load_config
                    cfg = load_config()
                except Exception:
                    cfg = None
            if isinstance(cfg, dict):
                dlg = cfg.get("dialog") or {}
                if isinstance(dlg, dict) and dlg.get("inject_output_rules_in_text"):
                    return True
                # agent 级覆盖（PetWindow 传入的 agent_config）优先于全局
                acfg = getattr(self, "_agent_config", None)
                if isinstance(acfg, dict):
                    adlg = acfg.get("dialog") or {}
                    if isinstance(adlg, dict) and adlg.get("inject_output_rules_in_text"):
                        return True
        except Exception:
            logger.debug("harness_adapter: 读取 inject_output_rules_in_text 失败", exc_info=True)
        return False

    def _needs_output_rules(self, source: str) -> bool:
        """该来源的回复是否会展示给用户 —— 决定要不要教它用标签。

        memory_extract / memory_reflect / screen_enrich 的输出是给机器读的，
        注入「必须嵌入情绪标签」会污染它们。其余（user/proactive/idle/...）都要。
        """
        return source not in self._NON_DISPLAY_SOURCES

    def _refresh_utility_cfg(self) -> None:
        """重新读 utility_model —— 用 preferences.json 的 mtime 做失效检测。

        为什么要动态读：视觉配置（screen.py）是每次截屏都调
        `get_vision_config()`，用户改完 Hana 设置页**立即生效**。
        而 utility 配置若只在 __init__ 读一次，用户改完要重启桌宠才生效
        —— 两条链行为不一致，用户会以为“又改了没反应”。

        mtime 检测而非每次读盘：后台任务调用频繁（屏幕增强每几秒一次），
        每次读 JSON 是浪费；stat 一次的开销可以忽略。
        """
        try:
            from hanako_home import hanako_home
            pref_path = hanako_home() / "user" / "preferences.json"
            mtime = str(pref_path.stat().st_mtime_ns) if pref_path.exists() else "missing"
        except Exception:
            mtime = "error"
        if mtime == getattr(self, "_utility_cfg_mtime", None):
            return  # 文件未变，沿用缓存
        try:
            from env_config import get_utility_config
            new_cfg = get_utility_config() or {}
        except Exception as e:
            logger.debug("读 utility_model 失败（回退对话模型）: %s", e)
            new_cfg = {}
        old_model = (getattr(self, "_utility_cfg", None) or {}).get("model")
        self._utility_cfg = new_cfg
        self._utility_cfg_mtime = mtime
        new_model = new_cfg.get("model")
        if new_model != old_model:
            logger.info(
                "后台任务模型变更: %s → %s",
                old_model or "（对话模型）", new_model or "（对话模型）",
            )

    @contextlib.contextmanager
    def _using_utility_model(self, source: str):
        """内部来源临时切换到后台任务模型（utility_model）。

        2026-09-17：屏幕增强 / 主动对话 / 记忆抽取 / 反思四个内部来源
        此前与用户对话共用 models.chat，与用户消息抢同一份配额
        （429 的主因）。现在这四个来源改走 Hana 的 utility_model。

        安全：
        - 只对 `_UTILITY_SOURCES` 里的来源生效（不碰用户对话）
        - 未配置 utility_model → 不动（保持旧行为）
        - 异常 / 退出时**一定**还原（用 try/finally）
        - 嵌套调用安全（保存/还原而非重写）
        """
        # 每次进入时刷新（mtime 未变则零开销）——保证用户改设置页即时生效
        self._refresh_utility_cfg()
        cfg = getattr(self, "_utility_cfg", None) or {}
        # 只对定时器/机器触发的内部来源生效；user 来源永远用对话模型
        if not cfg or source not in self._UTILITY_SOURCES:
            yield
            return
        saved = (self._base_url, self._api_key, self._model)
        try:
            self._base_url = cfg.get("base_url") or self._base_url
            self._api_key = cfg.get("api_key") or self._api_key
            self._model = cfg.get("model") or self._model
            yield
        finally:
            self._base_url, self._api_key, self._model = saved

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
            + self._output_rules()}]

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
            # 内部来源走后台任务模型（utility_model）；user 不切。
            # 上下文包住整个重试循环，以便日志记录**实际生效**的模型名
            # （若只包 _call_api，退出后 self._model 已还原，日志会写错）。
            _effective_model = self._model
            with self._using_utility_model(source):
                _effective_model = self._model
                for _attempt in range(_max_retries):
                    try:
                        resp = self._call_api(messages, tools=tools)
                        # O1-P2：成功 → 复位全局闸门的 429 计数（提供方是好的）
                        try:
                            from core.llm_gate import get_gate
                            get_gate().notify_ok()
                        except Exception:  # noqa: BLE001 — 闸门异常不影响主流程
                            pass
                        break  # 成功，跳出重试
                    except requests.exceptions.HTTPError as _http_e:
                        # 429 限流是「稍后重试」的临时信号，纳入指数退避（避免
                        # 立即失败后下个 tick 再次撞墙）；其它 HTTP 错误（400/401
                        # 等配置类）不重试，直接抛给外层 except 分支。
                        _status = getattr(getattr(_http_e, "response", None), "status_code", None)
                        if _status == 429:
                            # O1-P2：把 429 告诉全局闸门——让所有后台源一起收手，
                            # 而不是这里重试、屏幕那边继续撞。
                            try:
                                from core.llm_gate import get_gate
                                get_gate().notify_429(source or "direct")
                            except Exception:  # noqa: BLE001
                                pass
                        if _status == 429 and _attempt < _max_retries - 1:
                            logger.warning(
                                "LLM 429 限流 (attempt %d/%d, source=%s)，%.1fs 后重试",
                                _attempt + 1, _max_retries, source, _retry_delay,
                            )
                            _t.sleep(_retry_delay)
                            _retry_delay *= 2  # 指数退避
                        else:
                            raise  # 非 429，或最后一次失败
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
                    _elapsed, _effective_model,
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
                try:
                    from core.llm_gate import get_gate
                    get_gate().notify_429(source or "direct")
                except Exception:  # noqa: BLE001
                    pass
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
        if source in self._DIRECT_SOURCES:
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
            + self._output_rules()}]

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
            # 内部来源（proactive/idle/...）走后台任务模型（utility_model）；
            # user 来源不切（见 _UTILITY_SOURCES 注释）。
            with self._using_utility_model(source):
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
                    logger.debug("harness_adapter: 非致命异常(已静默吞掉)", exc_info=True)

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
                logger.debug("harness_adapter: 非致命异常(已静默吞掉)", exc_info=True)
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
                    self._current_session = sm.ensure_session(
                        agent_id=aid,
                        preferred_session_id=pinned,
                    )
                elif aid in self._agent_sessions:
                    self._current_session = self._agent_sessions[aid]
                else:
                    # 首次：为每个桌宠/agent 创建专属 session
                    self._current_session = sm.create_session(agent_id=aid)
                    # 标记归属：这个会话是桌宠自己建的（不是兜底漂移来的）
                    self._owned_sessions[aid] = getattr(
                        self._current_session, "session_id", None)
                self._agent_sessions[aid] = self._current_session
                new_sid = getattr(self._current_session, "session_id", None)
                self._agent_pinned[aid] = new_sid
                self._pinned_session_id = new_sid
                # 2026-09-20（A）：落盘，让重启后还能回到同一个会话
                self._save_pinned_sessions()
                # 2026-09-11：把会话 id 记进日志。
                # 排查「桌宠到底在哪个会话里说话」时这是唯一的直接凭据——
                # 我刚为此查了一整轮（文件取证、API 取证、被我自己的输出
                # 循环引用骗了四次）。一条日志能省下这些。
                logger.info("[session] agent=%s session=%s", aid, new_sid)
            except Exception as e:
                raise HanakoUnavailableBeforeSend("无法准备 Hanako Session") from e

        # 拼装 text：
        # 2026-09-16：输出规则**不再注入 text**。
        # 根因：规则以 [pet-output-rules] 包在用户消息前面，Hanako 侧
        #   - 会话标题从第一条 user message 生成 → 标题被规则污染
        #   - 历史里存的是带规则的原文 → 上下文里规则像"用户说过的话"
        # 现在规则改由 agent 的 AGENTS.md 承担（进 system 层，见
        #   ~/.hanako/agents/ophelia/AGENTS.md——桌宠读的就是主 agent 这一份）。
        # 2026-09-17：曾短暂用过专属 agent `ophelia-pet` + 仓库内 `persona/`
        # 软链方案，用户明确表示“只是同步人设，不是新增助手”，已退场并删除
        # persona/（它当时已无人引用）。
        # 这里只发干净的用户消息，标题/历史从此干净。
        # 兜底：若调用方显式要求（如本地直连路径），仍可注入。
        text = message.strip()
        if self._needs_output_rules(source) and self._inject_rules_in_text():
            text = (
                f"[pet-output-rules]\n{self._output_rules()}\n[/pet-output-rules]\n\n"
                + text
            )
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
                            logger.debug("harness_adapter: 非致命异常(已静默吞掉)", exc_info=True)
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
            logger.debug("harness_adapter: 非致命异常(已静默吞掉)", exc_info=True)

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
                # 2026-09-11：不再把工具名说给用户。
                # 原为 f"已为你完成「{name}」" —— 实测用户看到的是
                # “已为你完成「search_memory」”，把内部工具名泄到了气泡里。
                # 工具名只进日志（排障用）；用户侧返回空，调用方自会略过气泡，
                # 宁可不说，也不说一句工程黑话。
                logger.info("[synthesize] 工具轮无最终文本，工具=%s（不上气泡）", name)
                return ""
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
        # 剥离 [feel:v,a] / [do:name]（2026-09-10 新增标签）。
        #
        # 2026-09-11 修正：这三行曾把 [feel:]/[do:]/[message:] 在这里就剥掉，
        # 导致后续 parse_action_intent 看不到它们——**标签被删了，值也丢了**。
        # 实测：模型真的输出了 [feel:0.6,0.3]，parse_emotion 先把它剔了，
        # 意图解析拿到的是空文本，VA 永远不生效。
        #
        # 现在这里**不剥**，改由 parse_action_intent（两条路径都会调）负责；
        # 它能同时完成「提取值 + 清显示文本」两件事。
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
            # 标记归属：调用方（菜单「新建对话」）显式注入的是**桌宠自己的**会话。
            # 没这一步，重启后归属校验会把合法 pin 当作漂移残留丢掉。
            # getattr 兜底：测试/旧路径可能用 __new__ 绕过 __init__，
            # 不能让一个标记属性把 set_session 搞崩。
            if not hasattr(self, "_owned_sessions"):
                self._owned_sessions = {}
            self._owned_sessions[self.agent_id] = sid
            # 2026-09-20（A）：落盘，让重启后还能回到同一个会话
            self._save_pinned_sessions()

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
            char_dir_custom = hanako_home() / "agents" / self._agent_id / "pet"
            
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
        """构建可选表情/动作提示（注入输出规则）。

        2026-09-11 曾把 66 个原始预设/motion 名收敛成少量语义标签，
        同时把教学标签换成 `[do:名字]`、并标注“旧标签不再要求模型输出”。

        **但实测那条从未被采用**（`logs/oc_pet.log` 的 79 条“标签检测”）：

            feel=True      23 次   ← 情绪坐标，正常在用
            action=True    31 次   ← 动作**实际走的这条**
            do=True         0 次   ← prompt 正在教的那条，空转
            emotion/expression/duration = 0 次（兜底路径）

        模型一直在用旧标签 `[action:{...}]`（它仍被解析，所以**功能没坏**），
        而 prompt 教的那条一次也没出现。

        2026-09-19：改为**只教模型真在用的那条**，并把语义白名单挪到它上面——
        白名单是防“模型自己编动作名”的唯一栅栏，挂在没人用的标签上等于没挂。
        （`[do:]` 仍然照旧解析，向后兼容不变。）
        """
        try:
            renderer = getattr(self, '_renderer', None) or getattr(self, '_pet_renderer', None)
            if not renderer:
                return ""
            options = self._action_options_from_snapshot(renderer)
            if not options:
                # 回退：手写常量（renderer 的 _AI_DO_PROMPT）
                options = getattr(type(renderer), "_AI_DO_PROMPT", "") or \
                    getattr(renderer, "_AI_DO_PROMPT", "")
            if not options:
                return ""
            return (
                "3. 可选：想配合一个表情或小动作时加 "
                '[action:{"gesture":"名字","intensity":0.7}]，'
                "gesture **只能从这些里选**："
                + options
                + "。intensity 可省（默认 0.7）。"
                '例：[feel:0.8,0.6] [action:{"gesture":"开心"}]，'
                '[feel:-0.3,-0.2] [action:{"gesture":"叹气"}]。'
                "没有合适的就不加——不加也自然。"
            )
        except Exception:
            return ""

    def _action_options_from_snapshot(self, renderer) -> str:
        """从**实测能力快照**生成候选清单（B 方案，2026-09-20）。

        与手写常量 ``_AI_DO_PROMPT`` 的区别：那份是 36 个中文标签写死的，
        加一个新预设得手补一行；这份从模型文件实测生成（53 预设 / 7 motion /
        7 表情），**模型换了一行代码不用改**。

        两个开关（都要满足）：
          - ``config.dialog.action_options_from_snapshot``（默认 True）
          - 快照构建成功

        任一步失败都返回空串，调用方回退手写常量——**绝不能因为快照坏了
        就让 prompt 里没有候选**（那等于放任模型自己编动作名）。
        """
        try:
            cfg = getattr(self, "_config", None)
            if isinstance(cfg, dict):
                dlg = cfg.get("dialog") or {}
                if isinstance(dlg, dict) and dlg.get("action_options_from_snapshot") is False:
                    return ""
        except Exception:
            pass

        try:
            from core.capability_snapshot import build_snapshot

            cid = self._current_character_id()
            if not cid:
                return ""
            snap = build_snapshot(cid)
            return snap.grouped_labels_line()
        except Exception as e:
            logger.debug("harness_adapter: 快照生成候选失败（回退手写常量）: %s", e)
            return ""

    def _current_character_id(self) -> str:
        """当前角色 id（快照要按模型生成，不能写死）。

        ⚠ 2026-09-20 实测踩坑：**agent_id 不是 character_id**。

        adapter 的 ``self.agent_id`` 是 Hana 的 agent 名（如 ``"ophelia"``），
        而快照要的是 ``characters/`` 下的**模型目录名**（如 ``"miku"`` /
        ``"shizuku"``）。两者不同——第一版拿 ``_character_id`` / ``_current_char``
        / ``_agent_id`` 去猜，全取不到，快照路径**静默退化成空串**，
        prompt 一直用手写常量（单元测试全绿，功能是死的）。

        正确来源与 ``perception_mixin`` 初始化表达决策器时一致：
        ``config.character``，缺省回退 ``"miku"``。
        """
        try:
            cfg = getattr(self, "_config", None)
            if isinstance(cfg, dict):
                v = cfg.get("character")
                if isinstance(v, str) and v.strip():
                    return v.strip()
        except Exception:
            pass
        # 兜底：有些路径会把角色挂在 adapter 上
        for attr in ("_character_id", "_current_char"):
            v = getattr(self, attr, None)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return "miku"

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
