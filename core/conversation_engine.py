"""对话引擎 - 合并 bridge + pet 的核心逻辑

在 pet 进程内后台运行，不依赖文件中转：
  用户消息 -> LLM -> TTS -> 回调（气泡 + 音频）

用法:
    engine = ConversationEngine(character_id="yuexinmiao")
    engine.start()  # 启动后台线程 + 预加载 TTS
    engine.send("你好")  # 发送消息，异步处理
    # 结果通过 on_reply 回调返回
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time

from .harness_adapter import HanakoPetAdapter
from .perception import PerceptionController

logger = logging.getLogger(__name__)


def map_emotion_to_anim(emotion: str) -> str:
    """情绪 -> 动画序列
    
    与 pet.py 中的情绪动画映射保持一致：
    - happy -> waving
    - surprised -> jumping
    - thinking -> running
    - sad -> failed
    - 其他 -> idle
    """
    anim_map = {
        'happy': 'waving',
        'angry': 'waving',
        'surprised': 'jumping',
        'thinking': 'running',
        'sad': 'failed',
    }
    return anim_map.get(emotion, 'idle')


class ConversationEngine:
    """对话引擎 - LLM + TTS 一体化，后台线程处理

    生命周期：随 pet 启动而启动，随 pet 关闭而关闭。
    """

    def __init__(self, character_id: str = "yuexinmiao", perception: PerceptionController = None, tts_provider=None, builtin: bool = False, session_manager=None):
        self._character_id = character_id
        self._builtin = builtin
        self._adapter = None
        self._tts = tts_provider  # 外部注入，None 时用默认
        self._perception = perception or PerceptionController(character_id)  # 外部注入优先
        self._session_manager = session_manager  # 可选注入；PetManager 也可在 start() 后注入
        self._session_unsubscribers: list[callable] = []
        self._queue: list[dict] = []
        self._lock = threading.Lock()
        self._running = False
        self._user_turn_active = False  # 用户对话进行中时阻止主动消息

        # 工具系统
        from .tool_registry import ToolRegistry
        from .tool_executor import ToolExecutor
        self._tool_registry = ToolRegistry()
        self._tool_executor = ToolExecutor()
        self._tools: list[dict] = []  # OpenAI 格式工具列表
        self._thread = None
        self._tts_ready = False

        # 能力路由器（快速路径）
        from .capability_registry import CapabilityRouter
        self._capability_router = CapabilityRouter(
            perception=self._perception,
            tool_registry=self._tool_registry,
            tool_executor=self._tool_executor,
        )

        # ── M3: 记忆快照管理器 ──
        self._memory_snapshot_mgr = None
        try:
            from .memory_snapshot import MemorySnapshotManager
            self._memory_snapshot_mgr = MemorySnapshotManager(character_id)
            logger.info("MemorySnapshotManager initialized for %s", character_id)
        except Exception as e:
            logger.warning("MemorySnapshotManager not available: %s", e)

        # 回调（由 pet 设置）
        self.on_reply: callable = lambda reply, emotion, anim, audio_path: None
        self.on_status: callable = lambda msg: None  # 状态提示
        self.on_progress: callable = lambda msg: None  # 长任务进度提示
        self.on_tts_ready: callable = lambda: None  # TTS 加载完成
        # M4: 工具进度回调（Hanako WS 模式下，工具调用由服务端执行，这里只展示进度）
        # 参数: tool_name, phase ("start"/"progress"/"end"), display_text, success
        self.on_tool_progress: callable = lambda tool_name, phase, display_text, success: None

    @property
    def tts_ready(self) -> bool:
        return self._tts_ready

    def start(self):
        """启动引擎（后台线程）"""
        self._running = True

        # 初始化 LLM 适配器
        try:
            self._adapter = HanakoPetAdapter(agent_id=self._character_id, builtin=self._builtin)
            logger.info("LLM 适配器就绪 | model=%s | transport_mode=%s",
                        self._adapter.model_config.get("model", "?"), self._adapter.transport_mode)
        except Exception as e:
            logger.error("LLM 适配器初始化失败: %s", e)
            return

        # M4: 把 SessionManager 注入到 adapter（如果有）
        if self._session_manager is not None:
            try:
                self._adapter.set_session_manager(self._session_manager)
                logger.info("SessionManager 已注入 adapter")
            except Exception as e:
                logger.warning("SessionManager 注入失败: %s", e)

        # M4: 订阅共享 SessionManager；回调由 WS 派发线程触发。
        self._subscribe_session_manager()

        # 初始化 TTS（如果未注入）
        if not self._tts:
            try:
                from tts_provider.cosyvoice import CosyVoiceProvider
                self._tts = CosyVoiceProvider()
            except Exception as e:
                logger.warning("TTS 初始化失败，禁用 TTS: %s", e)
                self._tts = None

        if self._tts:
            try:
                spk_info = self._tts.get_speaker_info(self._character_id) if hasattr(self._tts, 'get_speaker_info') else {}
                if spk_info:
                    logger.info("TTS 配置就绪 | ref=%s", spk_info.get("ref_audio", "?")[-30:])
                else:
                    logger.info("TTS provider: %s", getattr(self._tts, 'name', 'unknown'))
            except Exception as e:
                logger.warning("TTS 信息获取失败: %s", e)

        # 启动后台线程
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        """停止引擎"""
        self._running = False
        self._perception.stop_screen()
        with self._lock:
            self._queue.clear()
        self._clear_session_subscriptions()

    def _get_builtin_help_text(self) -> str:
        """返回桌宠内置的使用说明"""
        return """喵~ 我是你的桌面宠物助手！这是我能做的事情：

**🎭 叙事引擎**
- 我会自动生成桌面小事件，陪你聊天解闷
- 每隔一段时间，我会主动和你说话

**👁️ 环境感知**
- 我能识别你正在用什么应用和文件
- 根据你的活动，我会给出有趣的评论

**💾 记忆快照**
- 我能导出我们的对话记忆，方便备份
- 也可以导入记忆，恢复之前的对话

**🐾 多宠协作**
- 如果你运行多个桌宠，我们可以互相聊天
- 我们会一起关心你，给你送虚拟礼物

**📦 角色包**
- 我能打包成角色包，方便分享给其他人
- 也可以导入别人分享的角色包

**🎤 语音交互**
- 我能用语音和你说话（如果配置了 TTS）
- 也能听你说话（如果配置了 ASR）

**⚙️ 设置面板**
- 右键点击我可以打开设置
- 在那里可以配置 API、TTS、ASR 等

有什么想问我的吗？"""

    def send(self, text: str, character: str = "", source: str = "user"):
        """发送消息（异步，结果通过 on_reply 回调）

        source: 'user' | 'proactive' | 'idle'
        - proactive/idle: 用户对话进行中时丢弃，否则插队到最前面
        - user: 正常排队 + 走 capability 路由
        """
        with self._lock:
            # 用户对话进行中：丢弃所有主动消息
            if source in ("proactive", "idle") and self._user_turn_active:
                logger.debug("Proactive message dropped: user turn active")
                return

            item = {
                "text": text,
                "character": character or self._character_id,
                "time": time.time(),
                "source": source,
            }
            if source in ("proactive", "idle"):
                self._queue.insert(0, item)
            else:
                self._queue.append(item)

    def _run(self):
        """后台线程主循环"""
        # 预加载 TTS
        self.on_status("正在准备声音...")
        if self._tts:
            self._tts.preload()
            self._tts_ready = self._tts.is_ready
        self.on_status("")
        self.on_tts_ready()

        # 刷新日程 + 启动屏幕感知
        self._perception.tick()
        self._perception.start_screen(interval=120)

        # 发现插件工具
        self._tool_registry.discover()
        self._tools = self._tool_registry.get_tools()
        if self._tools:
            logger.info("Plugin tools available: %d", len(self._tools))

        logger.info("对话引擎启动完成")

        while self._running:
            # 取消息
            msg = None
            with self._lock:
                if self._queue:
                    msg = self._queue.pop(0)

            if msg:
                self._process_message(msg)
            else:
                time.sleep(0.2)

    def _process_message(self, msg: dict):
        """处理一条消息：LLM -> 工具调用（可选）-> 回调文字 -> TTS"""
        text = msg["text"]
        character = msg["character"]
        source = msg.get("source", "user")

        logger.info("处理消息 [%s]: %s", character, text[:50])

        # 用户消息：标记 turn 活跃，阻止主动消息
        if source == "user":
            self._user_turn_active = True

        # 内置使用说明：当用户问“你能干什么”时，返回桌宠自身的功能说明
        help_keywords = ["你能干什么", "你会什么", "你有什么功能", "你能做什么", "怎么用你", "使用说明", "功能介绍"]
        if any(keyword in text for keyword in help_keywords):
            help_text = self._get_builtin_help_text()
            anim = "extra"
            emotion = "happy"
            logger.info("内置使用说明 [emotion:%s]: %s", emotion, help_text)
            # 直接回调，不调用 LLM
            if source == "user":
                self._user_turn_active = False
            self.on_reply(help_text, emotion, anim, "")
            return

        # 快速路径：能力路由器（仅用户消息，主动/idle 消息跳过）
        is_user_msg = msg.get("source", "user") == "user"
        route_result = self._capability_router.route(text) if is_user_msg else None
        if route_result:
            anim = route_result.anim or "idle"
            logger.info("Capability routed: %s -> %s", route_result.capability, route_result.text[:50])
            if source == "user":
                self._user_turn_active = False
            self.on_reply(route_result.text, route_result.emotion, anim, route_result.audio_path)
            return

        # 1. LLM 回复（可能返回 tool_calls）
        try:
            perception_ctx = self._perception.build_context()
            reply, emotion = self._adapter.chat(
                message=text, inject_memory=True,
                extra_context=perception_ctx,
                tools=self._tools if self._tools else None,
                source=source,
            )

            # 处理 tool_calls
            if isinstance(reply, dict) and reply.get("tool_calls"):
                # M4: Hanako 模式下的 tool_calls 由服务端执行，不重复跑本地 executor
                origin = reply.get("origin", "direct")
                if origin == "hanako":
                    logger.info("Hanako 服务端处理 tool_calls (%d 个)，跳过本地 executor",
                                len(reply["tool_calls"]))
                    # 通知 UI 工具被跳过（Hanako 端已经处理完）
                    self.on_tool_progress(
                        "hanako_tool", "end",
                        "工具已在 Hanako 端执行", True,
                    )
                    # 这里不重复处理，等待下一轮 turn_end 后由 chat_via_hanako 返回最终文本
                    # 实际上 chat_via_hanako 当前不会返回带 tool_calls 的中间状态——这是透传
                    # 防御性处理：如果有 content，用它
                    cleaned = (reply.get("message", {}) or {}).get("content", "") or "…"
                    emotion = self._adapter.parse_emotion(cleaned)[1] or "neutral"
                    cleaned = self._adapter.parse_emotion(cleaned)[0]
                    reply, emotion = cleaned or "…", emotion or "neutral"
                else:
                    reply, emotion = self._handle_tool_calls(
                        reply, text, character, perception_ctx
                    )

            if not reply:
                reply = "…"
            logger.info("LLM 回复: %s [emotion:%s]", reply, emotion)
        except Exception as e:
            logger.error("LLM 失败: %s", e)
            reply = "…（信号不太好，你再说一遍？）"
            emotion = "neutral"

        # 2. 动画映射
        anim = map_emotion_to_anim(emotion)

        # 3. TTS 合成（同步，和文字一起回调）
        audio_path = ""
        skip_reason = ""
        if not self._tts:
            skip_reason = "no tts provider"
        elif not self._tts_ready:
            skip_reason = "tts not ready"
        elif not reply.strip():
            skip_reason = "empty reply"
        elif reply.strip() in ("\u2026", "..."):
            skip_reason = "ellipsis reply"
        
        if not skip_reason:
            try:
                instruct_map = {
                    "happy": "开心", "sad": "难过", "angry": "生气",
                    "cute": "可爱", "thinking": "思考",
                }
                instruct = instruct_map.get(emotion, "")
                audio_path = self._tts.synthesize(reply, character_id=character, instruct=instruct) or ""
                if audio_path:
                    logger.info("TTS done: %s", os.path.basename(audio_path))
                else:
                    logger.warning("TTS failed, no audio")
            except Exception as e:
                logger.warning("TTS error: %s", e)
        else:
            logger.info("TTS skipped: %s", skip_reason or "unknown")

        # 4. 回调（文字 + 音频一起）
        if source == "user":
            self._user_turn_active = False
        self.on_reply(reply, emotion, anim, audio_path)

    def _handle_tool_calls(self, resp: dict, user_text: str, character: str, perception_ctx: str) -> tuple:
        """处理 LLM 的 tool_calls：执行工具 → 结果回传 → 再次调用 LLM"""
        tool_calls = resp["tool_calls"]
        assistant_message = resp["message"]

        # 将 assistant 消息（含 tool_calls）加入历史
        self._adapter._history.append({
            "role": "assistant",
            "content": assistant_message.get("content", ""),
            "tool_calls": tool_calls,
        })

        # 逐个执行工具
        for tc in tool_calls:
            func = tc.get("function", {})
            tool_name = func.get("name", "")
            tool_id = tc.get("id", "")

            # 解析参数
            try:
                args = json.loads(func.get("arguments", "{}"))
            except json.JSONDecodeError:
                args = {}

            logger.info("Tool call: %s(%s)", tool_name, json.dumps(args, ensure_ascii=False)[:100])

            # 查找并执行工具
            tool_def = self._tool_registry.get_tool(tool_name)
            if tool_def:
                result = self._tool_executor.execute(tool_def, args)
            else:
                result = f"工具 '{tool_name}' 不存在"

            logger.info("Tool result: %s", result[:100])

            # 将工具结果加入历史
            self._adapter._history.append({
                "role": "tool",
                "tool_call_id": tool_id,
                "content": result,
            })

        # 再次调用 LLM，让模型基于工具结果生成最终回复
        try:
            reply, emotion = self._adapter.chat(
                message="[工具执行完成，请根据结果用自然语言回复用户]",
                inject_memory=False,
                extra_context=perception_ctx,
            )
            return reply or "…", emotion or "neutral"
        except Exception as e:
            logger.error("LLM follow-up failed: %s", e)
            return "工具执行完成", "neutral"

    # ── M4: SessionManager 集成 ──

    def set_session_manager(self, manager) -> None:
        """注入 SessionManager（PetManager 启动后可调）"""
        self._clear_session_subscriptions()
        self._session_manager = manager
        if self._adapter is not None:
            try:
                self._adapter.set_session_manager(manager)
            except Exception as e:
                logger.warning("adapter.set_session_manager 失败: %s", e)
        self._subscribe_session_manager()

    def _subscribe_session_manager(self) -> None:
        manager = self._session_manager
        if manager is None or self._session_unsubscribers:
            return
        subscriptions = (
            ("on_progress", self._handle_session_progress),
            ("on_tool", self._handle_session_tool_progress),
            ("on_reply", self._handle_session_reply),
        )
        for method_name, callback in subscriptions:
            method = getattr(manager, method_name, None)
            if not callable(method):
                continue
            try:
                unsubscribe = method(callback)
                if callable(unsubscribe):
                    self._session_unsubscribers.append(unsubscribe)
            except Exception as e:
                logger.warning("SessionManager.%s 订阅失败: %s", method_name, e)

    def _clear_session_subscriptions(self) -> None:
        for unsubscribe in self._session_unsubscribers:
            try:
                unsubscribe()
            except Exception:
                pass
        self._session_unsubscribers.clear()

    def set_session(self, session_ref) -> None:
        """注入当前 Session 引用"""
        if self._adapter is not None:
            try:
                self._adapter.set_session(session_ref)
            except Exception as e:
                logger.warning("adapter.set_session 失败: %s", e)

    def create_new_session(self, agent_id: str = None, **kwargs) -> "object | None":
        """创建新 Session（供 pet.py 菜单“新建对话”调）

        Returns:
            SessionRef 或 None（创建失败）
        """
        if self._session_manager is None or not hasattr(self._session_manager, "create_session"):
            logger.warning("SessionManager 不可用，无法创建新 Session")
            return None
        try:
            aid = agent_id or self._character_id
            session = self._session_manager.create_session(agent_id=aid, **kwargs)
            self.set_session(session)
            logger.info("新 Session 已创建: %s", getattr(session, "session_id", "?"))
            return session
        except Exception as e:
            logger.error("create_session 失败: %s", e)
            return None

    def _is_current_session(self, session: object) -> bool:
        current = getattr(self._adapter, "_current_session", None)
        if current is None or session is None:
            return False
        return (
            getattr(current, "session_id", None) == getattr(session, "session_id", None)
            or getattr(current, "session_path", None) == getattr(session, "session_path", None)
        )

    def _handle_session_progress(self, session: object, display_text: str) -> None:
        """转发当前 Session 的思考/工具进度。"""
        if self._is_current_session(session):
            self.on_progress(display_text)

    def _handle_session_reply(self, result: object) -> None:
        """镜像来自 Hanako 主窗口或插件的外部回复。"""
        if getattr(result, "origin", "oc_pet") != "external":
            return
        if not self._is_current_session(getattr(result, "session", None)):
            return
        text, emotion = self._adapter.parse_emotion(getattr(result, "text", "") or "")
        self.on_reply(text or "…", emotion, map_emotion_to_anim(emotion), "")

    def _handle_session_tool_progress(self, progress: "object") -> None:
        """接收 SessionManager 的 ToolProgress 事件，转发给 UI"""
        try:
            if not self._is_current_session(getattr(progress, "session", None)):
                return
            tool_name = getattr(progress, "tool_name", "tool")
            phase = getattr(progress, "phase", "progress")
            display = getattr(progress, "display_text", "") or self._tool_display(tool_name)
            success = getattr(progress, "success", None)
            self.on_tool_progress(tool_name, phase, display, success)
        except Exception as e:
            logger.warning("_handle_session_tool_progress 错误: %s", e)

    def _tool_display(self, tool_name: str) -> str:
        """工具名 -> 中文友好展示文本"""
        mapping = {
            "web_search": "正在搜索…",
            "web_fetch": "正在读取网页…",
            "browser": "正在浏览…",
            "media_generate-image": "正在生成图片…",
            "read": "正在读取文件…",
            "write": "正在编辑…",
            "edit": "正在编辑…",
            "exec_command": "正在执行命令…",
        }
        return mapping.get(tool_name, f"正在使用 {tool_name}…")

    def switch_character(self, character_id: str):
        """切换角色 - 清空队列和历史"""
        with self._lock:
            self._queue.clear()
        self._character_id = character_id
        try:
            self._adapter = HanakoPetAdapter(agent_id=character_id)
            if self._session_manager is not None:
                self._adapter.set_session_manager(self._session_manager)
            if hasattr(self._adapter, '_history'):
                self._adapter._history.clear()
            logger.info("角色切换: %s", character_id)
        except Exception as e:
            logger.error("角色切换失败: %s", e)

    # ── M3: 记忆快照导出/导入 ──

    def export_memory_snapshot(self, output_path: str = None, description: str = "") -> str | None:
        """导出当前角色的记忆为 JSON 快照
        
        Args:
            output_path: 输出路径，默认自动生成
            description: 快照描述
            
        Returns:
            输出的文件路径，失败返回 None
        """
        if not self._memory_snapshot_mgr:
            logger.warning("MemorySnapshotManager not initialized")
            return None
        try:
            path = self._memory_snapshot_mgr.export_snapshot(
                output_path=output_path,
                description=description or f"Export for {self._character_id}",
            )
            logger.info("Memory snapshot exported: %s", path)
            return str(path)
        except Exception as e:
            logger.error("Failed to export memory snapshot: %s", e)
            return None

    def import_memory_snapshot(self, input_path: str, strategy: str = "smart") -> dict | None:
        """从 JSON 快照导入记忆
        
        Args:
            input_path: 快照 JSON 文件路径
            strategy: 合并策略 (overwrite / smart / skip_existing)
            
        Returns:
            操作结果统计 {imported, skipped, errors}，失败返回 None
        """
        if not self._memory_snapshot_mgr:
            logger.warning("MemorySnapshotManager not initialized")
            return None
        try:
            result = self._memory_snapshot_mgr.import_snapshot(
                input_path=input_path,
                strategy=strategy,
            )
            logger.info("Memory snapshot imported: %s", result)
            return result
        except Exception as e:
            logger.error("Failed to import memory snapshot: %s", e)
            return None

    def list_memory_snapshots(self, directory: str = None) -> list:
        """列出可用的记忆快照
        
        Returns:
            快照列表 [{path, agent_id, created_at, description}, ...]
        """
        if not self._memory_snapshot_mgr:
            return []
        try:
            return self._memory_snapshot_mgr.list_snapshots(directory=directory)
        except Exception as e:
            logger.error("Failed to list snapshots: %s", e)
            return []
