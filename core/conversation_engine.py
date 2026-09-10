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

import collections
import inspect
import json
import logging
import re
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .harness_adapter import HanakoPetAdapter
from .perception import PerceptionController

logger = logging.getLogger(__name__)


def _call_reply_cb(cb, reply, emotion, anim, audio_path, action_intent=None):
    """兼容 4 参（历史契约）与 5 参（含 action_intent）的 on_reply 回调。

    BugFix #6-C 给 on_reply 增加了第 5 个参数 action_intent；为保持向后兼容，
    旧回调（仅接受 4 个位置参数）仍应正常工作，不应因多传一个参数而静默失败
    （后台线程的 on_reply 调用被 try/except 包裹，多参 TypeError 会被吞掉，
    导致回复永不触发）。无法 introspect 的回调（如 Qt Signal.emit）按 5 参调用。
    """
    if not callable(cb):
        return
    try:
        params = list(inspect.signature(cb).parameters.values())
        has_var = any(p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD) for p in params)
        accepts_extra = len(params) >= 5 or has_var
    except (ValueError, TypeError):
        accepts_extra = True
    if accepts_extra:
        cb(reply, emotion, anim, audio_path, action_intent)
    else:
        cb(reply, emotion, anim, audio_path)


def map_emotion_to_anim(emotion: str) -> str:
    """情绪 -> 动画序列

    统一从 config.EXPRESSION_MAP（权威映射）读取，避免多份映射分叉。
    与 pet.py 的 _do_screen_emotion / SpriteRenderer 保持一致。
    """
    try:
        from config import EXPRESSION_MAP
        mapped = EXPRESSION_MAP.get(emotion)
        if mapped:
            return mapped[0] or 'idle'
    except Exception:
        logger.debug("conversation_engine: 非致命异常(已静默吞掉)", exc_info=True)
    return 'idle'


class ConversationEngine:
    """对话引擎 - LLM + TTS 一体化，后台线程处理

    生命周期：随 pet 启动而启动，随 pet 关闭而关闭。
    
    P2: 事件总线驱动，消息处理、流式解析、TTS 通过事件通信。
    """

    # ── 事件类型 ──
    EVT_MESSAGE_RECEIVED = "message_received"  # 消息入队
    EVT_LLM_START = "llm_start"  # LLM 开始调用
    EVT_LLM_CHUNK = "llm_chunk"  # LLM 流式 chunk
    EVT_LLM_COMPLETE = "llm_complete"  # LLM 完成
    EVT_TTS_START = "tts_start"  # TTS 开始合成
    EVT_TTS_COMPLETE = "tts_complete"  # TTS 完成
    EVT_REPLY = "reply"  # 回复（气泡）
    EVT_TOOL_CALL = "tool_call"  # 工具调用
    EVT_ERROR = "error"  # 错误

    # ── 事件处理器（类级别，所有实例共享）──
    _handlers: dict[str, list] = None

    @classmethod
    def on(cls, event_type: str, callback: callable):
        """P2: 注册事件处理器"""
        if cls._handlers is None:
            cls._handlers = {}
        if event_type not in cls._handlers:
            cls._handlers[event_type] = []
        cls._handlers[event_type].append(callback)

    @classmethod
    def off(cls, event_type: str, callback: callable):
        """P2: 移除事件处理器"""
        if cls._handlers and event_type in cls._handlers:
            cls._handlers[event_type].remove(callback)

    @classmethod
    def emit(cls, event_type: str, **kwargs):
        """P2: 触发事件（通知所有处理器）"""
        if cls._handlers and event_type in cls._handlers:
            for callback in cls._handlers[event_type]:
                try:
                    callback(**kwargs)
                except Exception as e:
                    logger.debug("事件处理器异常 [%s]: %s", event_type, e)

    def __init__(self, character_id: str = "yuexinmiao", perception: PerceptionController = None, tts_provider=None, builtin: bool = False, session_manager=None, agent_id: str = None):
        self._character_id = character_id
        self._builtin = builtin
        # F2: 对话后端 agent_id——独立于显示角色。
        # 默认取注入值；未指定时回退到 character_id（保持向后兼容），
        # 但接入 dialog.agent_id 配置后由 pet.py 显式传入。
        self._agent_id = agent_id or character_id
        self._adapter = None
        self._tts = tts_provider  # 外部注入，None 时用默认
        self._perception = perception or PerceptionController(character_id)  # 外部注入优先
        self._session_manager = session_manager  # 可选注入；PetManager 也可在 start() 后注入
        self._session_unsubscribers: list[callable] = []
        # 有界消息队列：maxlen=200，满时丢最旧（防异常场景下无限堆积内存）。
        # deque 的 popleft/appendleft 与 list 的 pop(0)/insert(0) 语义对齐。
        self._queue: "collections.deque[dict]" = collections.deque(maxlen=200)
        self._lock = threading.Lock()
        self._running = False

        # ── P0: Poke 系统耳语缓存（去重 + 防抖 500ms）──
        self._poke_cache: list[dict] = []
        self._poke_timer: threading.Thread | None = None

        # ── P2: 生命周期钩子（OnAwake/Start/Update/Destroy）──
        self._on_awake: callable | None = None  # 初始化后、start() 前
        self._on_start: callable | None = None  # start() 开始时
        self._on_update: callable | None = None  # 每次消息处理时（心跳）
        self._on_destroy: callable | None = None  # stop() 时
        self._last_update_time: float = 0.0  # 上次 update 时间（防频繁调用）

        # ── P1 打断状态机：消息代际（generation）机制 ──
        # 每次 send/interrupt 递增 generation，消息处理时校验代际是否过期：
        #   过期（用户已发新消息/已打断）→ 丢弃该消息的 LLM/TTS/回调结果，
        #   避免“打断后旧回复又冒出来”。
        self._generation = 0
        self._current_gen: int | None = None   # 正在处理的消息代际
        self._interrupt_event = threading.Event()  # 打断信号（LLM/工具/合成可检查）

        # 工具系统
        from .tool_registry import ToolRegistry
        from .tool_executor import ToolExecutor
        self._tool_registry = ToolRegistry()
        self._tool_executor = ToolExecutor()
        self._tools: list[dict] = []  # OpenAI 格式工具列表
        self._thread = None
        self._tts_ready = False
        # P1-4: TTS 专用线程池（max_workers=1 匹配 provider 内部串行锁），
        # 把合成从 _run 主循环解耦，避免单句 60-150s 卡住整个消息队列。
        self._tts_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="TTS")
        # 引用计数：正在合成中的 provider 数（用于 TTSReload 安全清理旧实例）
        self._tts_in_use = 0
        # P2-7: 语音音色解析器（由 PetWindow 注入）：(agent_id, emotion) -> voice。
        # 返回空字符串表示使用 provider 默认音色（向后兼容，未配置时行为不变）。
        # P2: 情绪历史追踪（最近 100 次对话情绪分布统计）
        self._emotion_history: collections.deque = collections.deque(maxlen=100)
        self._voice_resolver = None
        # B2: 工具进度节流（参考小蕾米插件日志节流）——同 工具+phase 在窗口内合并
        self._tool_progress_throttle: dict[str, float] = {}
        self._tool_progress_throttle_ms: float = 500.0

        # 能力路由器（快速路径）
        from .capability_registry import CapabilityRouter
        self._capability_router = CapabilityRouter(
            perception=self._perception,
            tool_registry=self._tool_registry,
            tool_executor=self._tool_executor,
        )

        # 需求②：Minecraft 桥接能力（可选，依赖外部 bot；未启用或不报错不致命）
        self._mc_bridge = None
        self._mc_window = None
        try:
            # P4：优先读 config.json 的 mc: 块（设置面板「🎮 Minecraft」页写入）
            from config import load_config
            _mc_cfg = load_config().get("mc")
        except Exception as _e:  # noqa: BLE001
            logger.debug("读取 mc 配置失败，退化为环境变量：%s", _e)
            _mc_cfg = None
        self.setup_mc_bridge(_mc_cfg)

        # 需求③：Hanako QQ/微信只读桥接（未启用即零副作用）
        self._hanako_watcher = None
        try:
            from config import load_config as _load_config_hb
            _hb_cfg = _load_config_hb().get("hanako_bridge")
        except Exception as _e:  # noqa: BLE001
            logger.debug("读取 hanako_bridge 配置失败：%s", _e)
            _hb_cfg = None
        self.setup_hanako_bridge(_hb_cfg)

        # P7: 统一工具调度层——显式插件优先，其次静态能力、关键词直达，
        # 未命中兜底 LLM/Hanako 服务端。插件支持 30s 热刷新（新增/删除即生效，无需重启）。
        from .unified_tool_router import UnifiedToolRouter
        self._unified_router = UnifiedToolRouter(
            perception=self._perception,
            tool_executor=self._tool_executor,
        )
        self._tool_refresh_interval: float = 30.0  # 插件热刷新周期（秒）

        # ── M3: 记忆快照管理器 ──
        self._memory_snapshot_mgr = None
        try:
            from .memory_snapshot import MemorySnapshotManager
            self._memory_snapshot_mgr = MemorySnapshotManager(character_id)
            logger.info("MemorySnapshotManager initialized for %s", character_id)
        except Exception as e:
            logger.warning("MemorySnapshotManager not available: %s", e)

        # 回调（由 pet 设置）
        self.on_reply: callable = lambda reply, emotion, anim, audio_path, action_intent=None: None
        self.on_status: callable = lambda msg: None  # 状态提示
        self.on_progress: callable = lambda msg: None  # 长任务进度提示
        self.on_tts_ready: callable = lambda: None  # TTS 加载完成
        # P1: 流式 chunk 回调（边生成边显示气泡）
        self.on_llm_chunk: callable = lambda chunk, accumulated, emotion, gen: None
        # M4: 工具进度回调（Hanako WS 模式下，工具调用由服务端执行，这里只展示进度）
        # 参数: tool_name, phase ("start"/"progress"/"end"), display_text, success
        self.on_tool_progress: callable = lambda tool_name, phase, display_text, success: None
        
        # P0-1: 保存原始回调引用
        self._original_on_reply = self.on_reply
        self._original_on_status = self.on_status
        self._original_on_progress = self.on_progress
        self._original_on_tts_ready = self.on_tts_ready
        self._original_on_tool_progress = self.on_tool_progress
        
        # P0-1: 线程安全回调包装器——确保回调在主线程执行
        self._main_thread = None  # 将在 start() 时设置
        
        # P0-1: 回调包装——用 Qt Signal 绕回主线程
        from PySide6.QtCore import QObject, Signal
        
        class _CallbackDispatcher(QObject):
            """线程安全回调派发器"""
            reply_signal = Signal(str, str, str, str, object)
            status_signal = Signal(str)
            progress_signal = Signal(str)
            tts_ready_signal = Signal()
            tool_progress_signal = Signal(str, str, str, object)
        
        self._dispatcher = _CallbackDispatcher()
        # 连接信号到真实回调（在主线程执行）
        self._dispatcher.reply_signal.connect(self._real_on_reply)
        self._dispatcher.status_signal.connect(self._real_on_status)
        self._dispatcher.progress_signal.connect(self._real_on_progress)
        self._dispatcher.tts_ready_signal.connect(self._real_on_tts_ready)
        self._dispatcher.tool_progress_signal.connect(self._real_on_tool_progress)

    # ── 需求②：mc_bridge 结果/状态回调（WS 后台线程调用）──
    def _mc_on_result(self, res, src: str) -> None:
        """mc_bridge 结果回调（来自 WS 后台线程）。仅 mc_task 更新迷你画面窗状态。"""
        if self._mc_window is None or src != "mc_task":
            return
        text = res.value if res.ok else res.error
        status = getattr(res, "status", "ok")
        self._mc_window.status_received.emit(f"[{status}] {text}")

    def _mc_theme(self) -> str:
        """迷你画面窗主题；pet 可在构造后设置 self._engine._theme（默认 light）。"""
        return getattr(self, "_theme", "light")

    def setup_mc_bridge(self, mc_cfg=None):
        """按给定的 mc 配置（重）建 Minecraft 桥接能力。可重复调用。

        设置面板改完开关后由 pet._apply_settings() 再次调用，做到不用重启桌宠。
        关闭时会摘掉能力并隐藏画面窗。返回桥接实例或 None（未启用/不可用）。
        """
        try:
            from core.mc_bridge import init_mc_bridge
            bridge = init_mc_bridge(mc_cfg)
        except Exception as e:  # noqa: BLE001
            self._mc_bridge = None
            logger.warning("mc_bridge 初始化跳过（不可用）：%s", e)
            return None

        if bridge is None:
            # 已关闭：能力已被 init_mc_bridge 内部摘除，这里收掉画面窗
            self._mc_bridge = None
            if self._mc_window is not None:
                try:
                    self._mc_window.hide()
                except Exception:  # noqa: BLE001
                    logger.debug("conversation_engine: 非致命异常(已静默吞掉)", exc_info=True)
            logger.info("mc_bridge 未启用（在设置「🎮 Minecraft」页打开开关即可）")
            return None

        self._mc_bridge = bridge
        logger.info("mc_bridge 能力已注册（transport=%s）", bridge.transport_kind)

        # P3：「看 bot 玩」迷你画面窗——已有则复用，只重绑回调
        try:
            from ui.mc_stream_window import MCStreamWindow
            if self._mc_window is None:
                # 主线程；QApplication 已于 main.py 先于 engine 创建
                self._mc_window = MCStreamWindow(theme=self._mc_theme())
                self._mc_window.close_requested.connect(self._mc_window.hide)
            # 截图帧/状态来自 WS 后台线程 → 经窗口信号跨线程安全投递到主线程
            bridge.on_frame(lambda payload, src: self._mc_window.frame_received.emit(payload))
            bridge.on_result(self._mc_on_result)
            logger.info("mc_bridge 迷你画面窗已就绪")
        except Exception as e:  # noqa: BLE001
            logger.warning("mc_bridge 画面窗创建失败（仅日志，能力仍可用）：%s", e)
        return bridge

    # ── 需求③：Hanako QQ/微信只读桥接 ──
    def _hb_on_incoming(self, items: list) -> None:
        """收到新消息 → 交给 LLM 生成一句提醒，走 proactive 通道让桌宠说出来。

        之所以给 LLM 而不是硬编码模板：桌宠的口吻本来就由提示词决定，硬模板会串味。
        顺带把多条合并成一次提醒，避免连续刷屏。
        """
        lines = []
        for it in items[:3]:
            plat_raw = str(it.get("platform") or "").lower()
            plat = {"qq": "QQ", "wechat": "微信"}.get(plat_raw, plat_raw or "消息")
            who = str(it.get("from") or "某人")
            content = str(it.get("content") or "").strip()
            if not content and it.get("has_media"):
                content = "[图片/文件]"
            if len(content) > 120:
                content = content[:120] + "…"
            lines.append(f"[{plat}] {who}：{content}")
        if not lines:
            return
        prompt = (
            "（这是 Hanako 桥接转发来的消息提醒，不是我正在跟你说话。）\n"
            + "\n".join(lines)
            + "\n请用一句简短自然的话提醒我有新消息，不要复述过长。"
        )
        try:
            self.send(prompt, source="proactive")
        except Exception as e:  # noqa: BLE001
            logger.warning("[hanako_bridge] 投递主动提醒失败：%s", e)

    def setup_hanako_bridge(self, hb_cfg=None):
        """按给定的 hanako_bridge 配置（重）启只读观察者。可重复调用。"""
        old = getattr(self, "_hanako_watcher", None)
        if old is not None:
            try:
                old.stop()
            except Exception:  # noqa: BLE001
                logger.debug("conversation_engine: 非致命异常(已静默吞掉)", exc_info=True)
            self._hanako_watcher = None
        try:
            from core.hanako_bridge import init_hanako_bridge
            watcher = init_hanako_bridge(hb_cfg, on_incoming=self._hb_on_incoming)
        except Exception as e:  # noqa: BLE001
            self._hanako_watcher = None
            logger.warning("hanako_bridge 初始化跳过（不可用）：%s", e)
            return None
        self._hanako_watcher = watcher
        return watcher

    @property
    def tts_ready(self) -> bool:
        return self._tts_ready
    
    # P0-1: 真实回调方法（在主线程执行）
    def _real_on_reply(self, reply: str, emotion: str, anim: str, audio_path: str, action_intent=None):
        """真实 on_reply 回调（主线程）"""
        try:
            if hasattr(self, '_original_on_reply') and callable(self._original_on_reply):
                _call_reply_cb(self._original_on_reply, reply, emotion, anim, audio_path, action_intent)
        except Exception as e:
            logger.error("on_reply callback error: %s", e)
    
    def _real_on_status(self, msg: str):
        """真实 on_status 回调（主线程）"""
        try:
            if hasattr(self, '_original_on_status') and callable(self._original_on_status):
                self._original_on_status(msg)
        except Exception as e:
            logger.error("on_status callback error: %s", e)
    
    def _real_on_progress(self, msg: str):
        """真实 on_progress 回调（主线程）"""
        try:
            if hasattr(self, '_original_on_progress') and callable(self._original_on_progress):
                self._original_on_progress(msg)
        except Exception as e:
            logger.error("on_progress callback error: %s", e)
    
    def _real_on_tts_ready(self):
        """真实 on_tts_ready 回调（主线程）"""
        try:
            if hasattr(self, '_original_on_tts_ready') and callable(self._original_on_tts_ready):
                self._original_on_tts_ready()
        except Exception as e:
            logger.error("on_tts_ready callback error: %s", e)
    
    def _real_on_tool_progress(self, tool_name: str, phase: str, display_text: str, success: object):
        """真实 on_tool_progress 回调（主线程）"""
        try:
            if hasattr(self, '_original_on_tool_progress') and callable(self._original_on_tool_progress):
                self._original_on_tool_progress(tool_name, phase, display_text, success)
        except Exception as e:
            logger.error("on_tool_progress callback error: %s", e)

    def start(self):
        """启动引擎（后台线程）"""
        # P2: 触发 OnAwake（初始化后、start 前）
        if self._on_awake:
            try:
                self._on_awake()
            except Exception as e:
                logger.debug("OnAwake 钩子异常: %s", e)
        
        self._running = True
        
        # P2: 触发 OnStart（start 开始时）
        if self._on_start:
            try:
                self._on_start()
            except Exception as e:
                logger.debug("OnStart 钩子异常: %s", e)
        # P0-1: 记录主线程 ID，用于线程安全回调
        from PySide6.QtCore import QThread
        self._main_thread = QThread.currentThread()
        # P0-1: 创建回调派发器并连接到真实回调
        from PySide6.QtCore import QObject, Signal
        
        class _CallbackDispatcher(QObject):
            """线程安全回调派发器"""
            reply_signal = Signal(str, str, str, str, object)
            status_signal = Signal(str)
            progress_signal = Signal(str)
            tts_ready_signal = Signal()
            tool_progress_signal = Signal(str, str, str, object)
        
        self._dispatcher = _CallbackDispatcher()
        # P0-1: 保存“当前”真实回调——此时 pet 已把自己的 _on_engine_reply 等挂上来，
        # 供 _real_on_* 经信号绕回主线程后调用。过去在 __init__ 保存的是空 lambda，
        # 导致“有回复无气泡/状态提示丢空函数上”的根因。
        self._original_on_reply = self.on_reply
        self._original_on_status = self.on_status
        self._original_on_progress = self.on_progress
        self._original_on_tts_ready = self.on_tts_ready
        self._original_on_tool_progress = self.on_tool_progress
        # 连接信号到真实回调（在主线程执行）
        self._dispatcher.reply_signal.connect(self._real_on_reply)
        self._dispatcher.status_signal.connect(self._real_on_status)
        self._dispatcher.progress_signal.connect(self._real_on_progress)
        self._dispatcher.tts_ready_signal.connect(self._real_on_tts_ready)
        self._dispatcher.tool_progress_signal.connect(self._real_on_tool_progress)
        # P0-1: 将包装后的回调赋值回去（后台线程调用时通过信号绕回主线程）
        self.on_reply = self._dispatcher.reply_signal.emit
        self.on_status = self._dispatcher.status_signal.emit
        self.on_progress = self._dispatcher.progress_signal.emit
        self.on_tts_ready = self._dispatcher.tts_ready_signal.emit
        self.on_tool_progress = self._dispatcher.tool_progress_signal.emit

        # 初始化 LLM 适配器
        try:
            # builtin 仅当对话 agent == 本地内置角色时成立；
            # 若 dialog.agent_id 指向服务端 agent（非本地角色），必须 builtin=False
            # 才读 ~/.hanako/agents/<id>/，否则会误读 characters/<id>/ 导致空设定。
            _adapter_builtin = self._builtin and (self._agent_id == self._character_id)
            self._adapter = HanakoPetAdapter(agent_id=self._agent_id, builtin=_adapter_builtin)
            logger.info("LLM 适配器就绪 | model=%s | transport_mode=%s | agent=%s | builtin=%s",
                        self._adapter.model_config.get("model", "?"), self._adapter.transport_mode,
                        self._agent_id, _adapter_builtin)
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

        # 初始化 TTS（如果未注入才走默认回退）
        if not self._tts:
            # 注意：pet.py 总是会把配置对应的 provider 注入进来（mimo/api/cosyvoice）。
            # 这里只有在注入为 None 时才兜底。过去这里无条件实例化 CosyVoiceProvider，
            # 会拉起 cosyvoice -> funasr -> onnxruntime(CPU) -> modelscope 下载 wetext 模型，
            # 在主线程造成数秒卡顿，且完全无视用户配置的 mimo/api。现改为配置感知：
            # 仅当配置确实为 cosyvoice 时才回退 CosyVoice，其余情况直接禁用 TTS，
            # 避免误拉起重型本地依赖链。
            provider_name = "cosyvoice"
            try:
                from config import load_config
                provider_name = load_config().get("tts", {}).get("provider", "cosyvoice")
            except Exception:
                provider_name = "cosyvoice"
            if provider_name == "cosyvoice":
                try:
                    from tts_provider.cosyvoice import CosyVoiceProvider
                    self._tts = CosyVoiceProvider()
                except Exception as e:
                    logger.warning("TTS 初始化失败，禁用 TTS: %s", e)
                    self._tts = None
            else:
                logger.warning(
                    "TTS provider 注入为 None 且配置为 '%s'（非 cosyvoice），"
                    "不回退到 CosyVoice，直接禁用 TTS（避免误拉起 funasr/wetext 造成卡顿）。"
                    "若需要语音，请检查 %s 的 TTS 配置/网络。",
                    provider_name, provider_name
                )
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
        
        # P2: 触发 OnDestroy（stop 时）
        if self._on_destroy:
            try:
                self._on_destroy()
            except Exception as e:
                logger.debug("OnDestroy 钩子异常: %s", e)
        
        # P0-1: 清理派发器
        if hasattr(self, '_dispatcher') and self._dispatcher is not None:
            try:
                self._dispatcher.deleteLater()
            except Exception:
                logger.debug("conversation_engine: 非致命异常(已静默吞掉)", exc_info=True)
            self._dispatcher = None
        self._perception.stop_screen()
        with self._lock:
            self._queue.clear()
        self._clear_session_subscriptions()

        # 关闭 TTS 线程池（等待在途合成结束，避免回调打到已关闭的引擎）
        try:
            self._tts_executor.shutdown(wait=False)
        except Exception:
            logger.debug("conversation_engine: 非致命异常(已静默吞掉)", exc_info=True)

        # 释放 TTS 资源：CosyVoice 在子进程里挂着 4.6GB 模型，不回收就是内存泄漏
        tts = getattr(self, "_tts", None)
        if tts is not None:
            try:
                tts.cleanup()
            except Exception:
                logger.debug("conversation_engine: 非致命异常(已静默吞掉)", exc_info=True)

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
        - proactive/idle: 始终允许，插队到最前面（走直接 LLM，不碰 Hanako）
        - user: 正常排队 + 走 capability 路由

        P1 打断：用户消息会推进代际，使正在处理的旧消息失效（打断）。
        """
        with self._lock:
            # 用户消息：推进代际，打断当前正在处理的旧消息
            if source == "user":
                self._generation += 1
                self._interrupt_event.set()
            item = {
                "text": text,
                "character": character or self._character_id,
                "time": time.time(),
                "source": source,
                "gen": self._generation,
            }
            if source in ("proactive", "idle"):
                # 插队到最前面；deque 有界时丢最旧（与 append 的丢弃策略一致）
                self._queue.appendleft(item)
            else:
                self._queue.append(item)
            
            # P2: 触发消息入队事件
            self.emit(self.EVT_MESSAGE_RECEIVED, text=text, character=character, source=source, gen=self._generation)

    def poke(self, text: str, character: str = "", source: str = "system"):
        """P0: 系统耳语 — 去重 + 防抖 500ms，空闲时才合并发送。

        用于结果回传、状态提示等非对话消息。与 send() 的区别：
        - 不推进代际（不打断当前对话）
        - 500ms 防抖合并（多条 poke 合并为一条）
        - 去重（相同文本不重复发送）
        - 空闲时才发送（不占用 LLM 队列）

        借鉴 Alife 的 Interactor.Poke 机制。
        """
        if not text or not text.strip():
            return
        with self._lock:
            # 去重：相同文本已在缓存中则跳过
            if any(m.get("text") == text for m in self._poke_cache):
                return
            self._poke_cache.append({
                "text": text,
                "character": character or self._character_id,
                "time": time.time(),
                "source": source,
            })
            # 防抖：500ms 后合并发送
            if self._poke_timer is None or not self._poke_timer.is_alive():
                self._poke_timer = threading.Thread(
                    target=self._flush_poke, daemon=True, name="poke-flush"
                )
                self._poke_timer.start()

    def _flush_poke(self):
        """P0: 合并发送缓存的 poke 消息（500ms 防抖后调用）"""
        time.sleep(0.5)
        with self._lock:
            if not self._poke_cache:
                return
            texts = [m["text"] for m in self._poke_cache]
            self._poke_cache.clear()
        # 合并为一条消息，带来源标记
        merged = "\n".join(f"[系统推送] {t}" for t in texts)
        self.send(merged, source="system")

    def _is_correction_message(self, text: str) -> bool:
        """P0: 检查消息是否为格式纠正消息（防死循环）"""
        return "[回复格式纠正]" in text
    
    def _record_emotion(self, emotion: str) -> None:
        """P2: 记录情绪到历史（用于分布统计）"""
        if emotion:
            # 延迟初始化（测试可能用 object.__new__ 跳过 __init__）
            if not hasattr(self, '_emotion_history'):
                import collections
                self._emotion_history = collections.deque(maxlen=100)
            self._emotion_history.append(emotion)
    
    def get_emotion_distribution(self, last_n: int = 100) -> dict:
        """P2: 获取最近 N 次对话的情绪分布
        
        Returns:
            {emotion: count} 字典，按 count 降序排列
        """
        from collections import Counter
        recent = list(self._emotion_history)[-last_n:]
        if not recent:
            return {}
        counter = Counter(recent)
        # 按 count 降序排列
        return dict(counter.most_common())
    
    def get_emotion_stats(self) -> dict:
        """P2: 获取情绪统计摘要（供 UI 显示）
        
        Returns:
            {
                "total": 100,
                "distribution": {"happy": 30, "sad": 20, ...},
                "percentages": {"happy": "30%", "sad": "20%", ...},
                "dominant": "happy",
            }
        """
        dist = self.get_emotion_distribution()
        total = sum(dist.values())
        if total == 0:
            return {"total": 0, "distribution": {}, "percentages": {}, "dominant": None}
        percentages = {k: f"{v * 100 // total}%" for k, v in dist.items()}
        dominant = max(dist, key=dist.get) if dist else None
        return {
            "total": total,
            "distribution": dist,
            "percentages": percentages,
            "dominant": dominant,
        }

    def interrupt(self, reason: str = "user_interrupt") -> str:
        """主动打断当前对话（用户点停止 / 语音输入开始 / 发新消息）。

        P1 打断状态机：每次打断记录原因，供消费方（pet.py）决定后续行为。
        状态：
          - new_message: 用户发新消息 → 旧回复作废，转入新对话
          - voice_start: 语音输入开始 → 旧回复作废，进入聆听
          - user_stop:   用户点停止   → 旧回复保留待恢复（不粗暴丢弃）

        效果：
          - 推进代际，使正在处理的旧消息失效
          - 清理待处理队列（主动消息），保留最新用户消息
          - 触发打断信号，供 LLM/TTS 层检查
          - 若走 Hanako WS，调用 session_manager.abort 真正取消 LLM 思考

        Returns:
            本次打断状态（"interrupted" / "cancelled" / "completed"）
        """
        # 状态映射：打断原因 → 状态
        state_map = {
            "new_message": "cancelled",   # 旧回复作废，转入新对话
            "voice_start": "interrupted", # 进入聆听，旧回复让位
            "user_stop": "interrupted",   # 停止，保留待恢复
        }
        state = state_map.get(reason, "interrupted")
        with self._lock:
            self._generation += 1
            self._interrupt_event.set()
            self._last_interrupt_state = state
            self._last_interrupt_reason = reason
            # 清掉非用户消息（proactive/idle 的可丢），保留最新用户消息
            # 注意：_queue 是有界 deque，必须重建为 deque 而不是 list，
            # 否则后续 popleft/maxlen 语义全丢。
            self._queue = collections.deque(
                (m for m in self._queue if m.get("source") == "user"),
                maxlen=200,
            )
        # 打断 Hanako WS 的 LLM 思考（若当前在 Hanako 上）
        try:
            sm = self._session_manager
            adapter = self._adapter
            if sm is not None and adapter is not None:
                session = getattr(adapter, "_current_session", None)
                if session is not None and hasattr(sm, "abort"):
                    sm.abort(session, reason=reason)
        except Exception as e:
            logger.warning("interrupt: abort Hanako session failed: %s", e)
        logger.info("对话打断: gen=%d state=%s reason=%s", self._generation, state, reason)
        return state

    @property
    def last_interrupt_state(self) -> str | None:
        """最近一次打断的状态（interrupted / cancelled / completed）"""
        with self._lock:
            return getattr(self, "_last_interrupt_state", None)

    def _run(self):
        """后台线程主循环"""
        # 预加载 TTS（读取/写 ready 加锁，preload 锁外）
        self.on_status("正在准备声音...")
        with self._lock:
            tts = self._tts
        if tts:
            # 单步保护：preload 失败只告警，不阻断引擎启动
            try:
                tts.preload()
            except Exception as e:
                logger.warning("TTS 预加载失败（继续启动，语音稍后不可用）: %s", e)
            try:
                with self._lock:
                    self._tts_ready = tts.is_ready
            except Exception as e:
                logger.warning("TTS 就绪状态读取失败: %s", e)
        self.on_status("")
        self.on_tts_ready()

        # 刷新日程 + 启动屏幕感知（interval 从 config 读，支持随机范围）
        try:
            self._perception.tick()
        except Exception as e:
            logger.warning("日程感知刷新失败（继续启动）: %s", e)
        try:
            from config import load_config
            screen_cfg = load_config().get("screen", {}) or {}
            interval = int(screen_cfg.get("interval", 120) or 120)
        except Exception:
            interval = 120
        try:
            self._perception.start_screen(interval=interval)
        except Exception as e:
            logger.warning("屏幕感知启动失败（继续启动）: %s", e)

        # 发现插件工具
        try:
            self._tool_registry.discover()
        except Exception as e:
            logger.warning("插件工具发现失败（继续启动）: %s", e)
        try:
            self._tools = self._tool_registry.get_tools()
        except Exception as e:
            logger.warning("读取工具列表失败: %s", e)
            self._tools = []
        if self._tools:
            logger.info("Plugin tools available: %d", len(self._tools))
        # P7: 构建统一路由关键词索引（插件工具显式/关键词直达）
        try:
            self._unified_router.refresh(self._tool_registry)
        except Exception as e:
            logger.warning("统一路由索引刷新失败（继续启动）: %s", e)

        logger.info("对话引擎启动完成")

        while self._running:
            # 整个循环体包 try/except：任何单条消息处理异常都不能杀死引擎线程
            #（线程死亡 = 桌宠永久失语）。记录堆栈后继续跑下一条。
            try:
                # P7: 插件热刷新（每 30s 检测；新增/删除插件无需重启）
                try:
                    if self._unified_router.should_refresh(interval=self._tool_refresh_interval):
                        self._hot_refresh_tools()
                except Exception as e:
                    logger.warning("插件热刷新检测失败: %s", e)
                # 取消息
                msg = None
                with self._lock:
                    if self._queue:
                        msg = self._queue.popleft()

                if msg:
                    # P2: 触发 OnUpdate（每次消息处理时，限频 1 秒）
                    now = time.time()
                    if self._on_update and (now - self._last_update_time) >= 1.0:
                        try:
                            self._on_update()
                            self._last_update_time = now
                        except Exception as e:
                            logger.debug("OnUpdate 钩子异常: %s", e)
                    
                    self._process_message(msg)
                else:
                    time.sleep(0.2)
            except Exception:
                logger.exception("对话引擎主循环异常，已记录并继续")
                time.sleep(0.2)

    def _hot_refresh_tools(self) -> None:
        """热刷新插件工具：重新 discover + 重建统一路由索引 + 刷新 LLM 工具列表。

        P7：新增/删除插件 30 秒内生效，无需重启桌宠。
        """
        try:
            before = self._tool_registry.tool_count
            self._tool_registry.refresh()
            self._unified_router.refresh(self._tool_registry)
            self._tools = self._tool_registry.get_tools()
            after = self._tool_registry.tool_count
            if before != after:
                logger.info("插件热刷新: 工具数 %d -> %d", before, after)
        except Exception as e:
            logger.warning("插件热刷新失败: %s", e)

    def _is_stale(self, gen: int) -> bool:
        """检查消息代际是否已过期（用户已打断/发新消息）。

        过期 → 应丢弃该消息的 LLM/TTS/回调结果。
        判断：gen 小于当前代际则视为过期（当前代际仍有效）。
        """
        with self._lock:
            return gen < self._generation

    def _chat_stream_wrapper(self, message: str, extra_context: str, source: str, gen: int) -> tuple:
        """P2: 流式调用包装器 — 边生成边解析 emotion/action。

        调用 adapter.chat_stream() 收集 chunks，同时解析标签。
        返回 (完整回复, emotion) 供后续逻辑使用。

        优势：
        - 用户更早看到气泡（边生成边显示）
        - 情绪/动作在生成过程中就能触发（不用等完整回复）
        """
        accumulated = ""
        emotion = "neutral"
        
        try:
            for chunk in self._adapter.chat_stream(
                message=message, inject_memory=True,
                extra_context=extra_context,
                tools=self._tools if self._tools else None,
                source=source,
            ):
                # 解包 (chunk_text, accumulated_text, emotion, action_intent)
                if isinstance(chunk, tuple) and len(chunk) == 4:
                    chunk_text, acc_text, stream_emotion, action_intent = chunk
                    accumulated = acc_text
                    emotion = stream_emotion or "neutral"
                    
                    # P1: 流式 chunk 事件 — 通知 UI 边生成边显示
                    if chunk_text and not self._is_stale(gen):
                        self.emit(self.EVT_LLM_CHUNK, chunk=chunk_text, accumulated=accumulated, emotion=emotion, gen=gen)
                        # 直接调用回调（线程安全：pet 会通过信号切主线程）
                        if self.on_llm_chunk:
                            try:
                                self.on_llm_chunk(chunk_text, accumulated, emotion, gen)
                            except Exception:
                                logger.debug("conversation_engine: 非致命异常(已静默吞掉)", exc_info=True)
                    
                    # P2: 边生成边触发情绪（如果已解析到标签）
                    if action_intent and not self._is_stale(gen):
                        logger.debug("流式检测到动作: %s", action_intent)
                else:
                    # 兼容非流式返回
                    accumulated = chunk or ""
                    self.emit(self.EVT_LLM_CHUNK, chunk=chunk, accumulated=accumulated, emotion=emotion, gen=gen)
        except Exception as e:
            logger.error("流式调用失败: %s", e)
            accumulated = "（嗯…让我缓一下）"
            emotion = "neutral"
        
        # 最终解析 emotion（如果流式没有解析到）
        if emotion == "neutral" and accumulated:
            try:
                parsed_text, parsed_emotion = self._adapter.parse_emotion(accumulated)
                if parsed_emotion:
                    emotion = parsed_emotion
                    accumulated = parsed_text or accumulated
            except Exception:
                logger.debug("conversation_engine: 非致命异常(已静默吞掉)", exc_info=True)
        
        return accumulated, emotion

    def _process_message(self, msg: dict):
        """处理一条消息：LLM -> 工具调用（可选）-> 回调文字 -> TTS"""
        text = msg["text"]
        character = msg["character"]
        source = msg.get("source", "user")
        gen = msg.get("gen", self._generation)

        # P0: 消息来源 Tag — 让 LLM 知道消息从哪来
        source_tags = {
            "user": "[消息来源(user)]",
            "proactive": "[消息来源(proactive)]",
            "idle": "[消息来源(idle)]",
            "system": "[系统推送]",
        }
        source_tag = source_tags.get(source, f"[消息来源({source})]")
        tagged_text = f"{source_tag} {text}"

        # P1：若消息在入队后被新消息打断（代际过期），直接跳过
        if self._is_stale(gen):
            logger.info("跳过过期消息: gen=%d 当前=%d（用户已发新消息，旧消息作废）",
                        gen, self._generation)
            return

        logger.info("处理消息 [%s]: %s", character, text[:50])

        # 内置使用说明：当用户问“你能干什么”时，返回桌宠自身的功能说明
        help_keywords = ["你能干什么", "你会什么", "你有什么功能", "你能做什么", "怎么用你", "使用说明", "功能介绍"]
        if any(keyword in text for keyword in help_keywords):
            help_text = self._get_builtin_help_text()
            anim = "extra"
            emotion = "happy"
            logger.info("内置使用说明 [emotion:%s]: %s", emotion, help_text)
            # 直接回调，不调用 LLM
            _call_reply_cb(self.on_reply, help_text, emotion, anim, "", None)
            return

        # 快速路径：统一工具调度（仅用户消息，主动/idle 消息跳过）
        # P7: 显式插件优先 → 静态能力 → 关键词 → 兜底 LLM/Hanako 服务端
        is_user_msg = msg.get("source", "user") == "user"
        if is_user_msg:
            route_result = self._unified_router.route(
                text,
                tool_registry=self._tool_registry,
                static_router=self._capability_router,
            )
        else:
            route_result = None
        if route_result:
            anim = route_result.anim or "idle"
            display_text = self._friendly_tool_text(route_result)
            logger.info("Unified routed: %s -> %s", route_result.capability, display_text[:50])
            _call_reply_cb(self.on_reply, display_text, route_result.emotion, anim, route_result.audio_path, None)
            return

        # 1. LLM 回复（可能返回 tool_calls）
        # BugFix #4：LLM/Hanako 可能耗时数十秒（工具链 / tool-silent 恢复前 /
        # 慢模型 inference），用户只看到静止的"思考中..."气泡。这里起一个心跳
        # 线程：每 6 秒推送一次"还在想..."续期气泡，避免 27s 静默无反馈。
        _progress_stop = threading.Event()

        def _progress_heartbeat():
            while not _progress_stop.wait(6.0):
                try:
                    self.on_progress("还在想...")
                except Exception:
                    logger.debug("conversation_engine: 非致命异常(已静默吞掉)", exc_info=True)

        _heartbeat = threading.Thread(
            target=_progress_heartbeat, daemon=True, name="llm-progress-heartbeat"
        )
        _heartbeat.start()
        try:
            try:
                # 2026-09-09: 传递 source 参数，区分用户对话和 proactive 场景
                perception_ctx = self._perception.build_context(source=source)
                
                # P2: 触发 LLM 开始事件
                self.emit(self.EVT_LLM_START, text=tagged_text, source=source, gen=gen)
                
                # P2: 检查是否支持流式 — 边生成边解析 emotion/action
                if hasattr(self._adapter, 'chat') and getattr(self._adapter, 'transport_mode', None) in ('prefer_hanako', 'hanako_only'):
                    # 优先走 Hana 通道（chat_via_hanako）
                    reply, emotion = self._adapter.chat(
                        message=tagged_text, inject_memory=True,
                        extra_context=perception_ctx,
                        tools=self._tools if self._tools else None,
                        source=source,
                    )
                elif hasattr(self._adapter, 'chat_stream'):
                    reply, emotion = self._chat_stream_wrapper(
                        tagged_text, perception_ctx, source, gen
                    )
                else:
                    # 回退到非流式
                    reply, emotion = self._adapter.chat(
                        message=tagged_text, inject_memory=True,
                        extra_context=perception_ctx,
                        tools=self._tools if self._tools else None,
                        source=source,
                    )
                
                # P2: 触发 LLM 完成事件
                self.emit(self.EVT_LLM_COMPLETE, reply=reply, emotion=emotion, gen=gen)
            finally:
                # chat 返回（或异常）即停心跳，避免 "还在想..." 覆盖后续回复气泡
                _progress_stop.set()

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
                        reply, text, character, perception_ctx, gen
                    )

            if not reply:
                reply = "…"
            logger.info("LLM 回复: %s [emotion:%s]", reply, emotion)
            
            # BugFix: 确保剥离 [emotion:xxx] / [emotion=xxx] 标签（chat_direct 已解析，但 chat_via_hanako 等路径可能漏）
            if "[emotion:" in reply or "[emotion=" in reply:
                reply = self._adapter.parse_emotion(reply)[0]
            
            # P2: 记录情绪到历史（用于分布统计）
            self._record_emotion(emotion)
        except Exception as e:
            logger.error("LLM 失败: %s", e)
            reply = "（嗯…让我缓一下）"
            emotion = "neutral"

        # P0: 格式纠正 — AI 回复缺少 emotion/action 标签时，内部补默认标签
        # 不再 poke 纠正消息给 LLM（避免 LLM 把纠正消息当用户输入回复，造成来源混淆）
        if not self._is_correction_message(tagged_text):
            logger.info("自动补充检查: reply=%r", reply[:80])
            has_emotion_tag = "[emotion:" in reply or "[emotion=" in reply
            has_action_tag = "[action:" in reply
            has_expression_tag = "[expression:" in reply
            has_duration_tag = "[duration:" in reply
            logger.info("标签检测: emotion=%s action=%s expression=%s duration=%s",
                        has_emotion_tag, has_action_tag, has_expression_tag, has_duration_tag)
            
            if not has_emotion_tag and not has_action_tag:
                # 内部补默认 emotion 标签，不上送 LLM
                if not reply.strip().startswith("（嗯"):
                    reply = reply.strip() + " [emotion:neutral]"
                    logger.debug("格式纠正: 内部补充 emotion:neutral 标签")
            
            # P1: 自动补充 [expression:xxx] 和 [duration:xxx]（如果 LLM 没输出）
            # 基于情绪推断默认表情参数
            if not has_expression_tag:
                # 根据情绪推断默认表情参数
                expression_defaults = {
                    "happy": "smile=80,blush=40",
                    "sad": "mouth_form=-0.3",
                    "angry": "mouth_form=0.5",
                    "surprised": "eye_open=0.8",
                    "thinking": "eye_open=0.6",
                    "neutral": "smile=30",
                }
                expr_params = expression_defaults.get(emotion, "smile=50")
                reply = reply + f" [expression:{expr_params}]"
                logger.info("自动补充 expression: %s", expr_params)
            
            # P1: 自动补充 [action:{...}]（如果 LLM 没输出）
            # 基于情绪推断默认动作
            if not has_action_tag:
                # 根据情绪推断默认动作
                action_defaults = {
                    "happy": {"gesture": "happy", "intensity": 0.6},
                    "sad": {"gesture": "sad", "intensity": 0.5},
                    "angry": {"gesture": "angry", "intensity": 0.7},
                    "surprised": {"gesture": "surprised", "intensity": 0.8},
                    "thinking": {"gesture": "thinking", "intensity": 0.5},
                    "neutral": {"gesture": "idle", "intensity": 0.3},
                }
                action_params = action_defaults.get(emotion, {"gesture": "idle", "intensity": 0.3})
                import json
                reply = reply + f" [action:{json.dumps(action_params)}]"
                logger.info("自动补充 action: %s", action_params)
            
            if not has_duration_tag:
                # 默认持续时间：3 秒
                reply = reply + " [duration:3]"
                logger.info("自动补充 duration: 3")

        # P1：LLM 调用后检查——若已打断（代际过期），不再继续 TTS/回调
        if self._is_stale(gen):
            logger.info("LLM 后已打断，丢弃结果: gen=%d 当前=%d（旧回复不再上气泡）",
                        gen, self._generation)
            return

        # M6: 解析 [action:{...}] 结构化动作意图（任意动作；向后兼容 [emotion:xxx]）
        action_intent = None
        try:
            _ai_text, _ai_intent = self.parse_action_intent(reply)
            if _ai_intent is not None:
                reply = _ai_text
                action_intent = _ai_intent
        except Exception:
            logger.debug("conversation_engine: 非致命异常(已静默吞掉)", exc_info=True)

        # 2. 动画映射
        anim = map_emotion_to_anim(emotion)

        # M5-ACT: 解析回复里的 JSON 动作指令，如 `{"action":"wave"}`。
        # - 命中 → 覆盖 anim（AI 主动指定动作），并从回复文本中剥掉标记，气泡只显示正文
        # - 未命中 → 维持情绪映射的 anim
        try:
            _act_re, _act_anim = self._parse_action_directive(reply)
            if _act_re is not None:
                reply = _act_re
                anim = _act_anim or anim
        except Exception:
            logger.debug("conversation_engine: 非致命异常(已静默吞掉)", exc_info=True)

        # P2: 桌宠输出分析 — AI 回复内容自动匹配动作（不用显式标签）
        # 规则：有 [emotion:xxx] 或 [action:{...}] 标签时，不再做语义分析（有标记就不猜）
        if action_intent is None and anim == map_emotion_to_anim(emotion):
            # 检查是否有显式情绪标签
            has_explicit_emotion = "[emotion:" in reply
            if not has_explicit_emotion:
                try:
                    _semantic_action = self._analyze_reply_content(reply, emotion)
                    if _semantic_action:
                        action_intent = _semantic_action
                        anim = _semantic_action.get("anim", anim)
                        logger.debug("语义分析触发动作: %s", _semantic_action)
                except Exception:
                    logger.debug("conversation_engine: 非致命异常(已静默吞掉)", exc_info=True)

        # P1-6: 文字先行——LLM 回复立即上气泡，不等 TTS 合成
        # （本地 CosyVoice 合成需数秒；若等音频做好才回调，用户看到的是
        #  长时间只有“思考中”气泡甚至无气泡，像“没回话”。
        #  先显示文字（audio_path=""），TTS 完成后同文本再回调只会续期
        #  气泡时长并播放音频，不重复闪烁。）
        try:
            # BugFix: 剥离 [emotion:xxx] 标签，回调时只显示纯文本
            import re
            clean_reply = re.sub(r'\s*\[\s*emotion\s*:\s*\w+\s*\]\s*', ' ', reply, flags=re.IGNORECASE).strip()
            _call_reply_cb(self.on_reply, clean_reply, emotion, anim, "", action_intent)
        except Exception:
            logger.debug("conversation_engine: 非致命异常(已静默吞掉)", exc_info=True)

        # 3. TTS 合成 + 回调：提交到专用线程池，避免同步合成卡住消息队列（P1-4）
        # 把合成与回传从 _run 主循环解耦，_run 可立即处理下一条消息。
        instruct_map = {
            "happy": "开心", "sad": "难过", "angry": "生气",
            "cute": "可爱", "thinking": "思考",
        }
        instruct = instruct_map.get(emotion, "")
        try:
            self._tts_executor.submit(
                self._synth_and_reply, reply, emotion, anim, character,
                instruct, source, gen,
            )
        except RuntimeError:
            # 线程池已关闭（引擎停止中），直接丢弃本次合成
            logger.debug("TTS 线程池已关闭，跳过合成: gen=%d", gen)

    # 内部总线/日志标签（不应上气泡）：截获打印进 tool_result 的 [bus]/[WS]/[httpx] 等
    _INTERNAL_TAG_RE = re.compile(r'^\[\w+\]')
    _JSON_SUBSTR_RE = re.compile(r'[\{\[][\s\S]*[\}\]]')

    def _friendly_tool_text(self, route_result) -> str:
        """把工具路由结果转成可读文案，避免原始 JSON / 内部日志直接上气泡。

        R3 结果友好化：工具执行结果（tool_result）先做内部日志清洗，再尝试解析成
        JSON 提取关键字段；解析不出时按能力名兜底友好话，绝不把 [bus]/[WS] 等
        内部日志原样展示给用户。规则：
        - 无 tool_result → text 已是可读文案（内部能力如日报/会话信息），原样展示
        - 清洗后仍为空 / 仍是 JSON-ish 噪声（[ { 开头但解析失败）→ 能力兜底话术
        - tool_result 是 JSON 对象 → _summarize_dict_result 提取 name/id/action/状态
        - tool_result 是 JSON 数组 → "共 N 项结果…"（N 为元素数）
        - 其余可读文本 → 截断 80 字（兼容正常自然语言返回）
        """
        raw = (route_result.tool_result or "").strip()
        if not raw:
            # 内部能力/静态能力：text 就是最终可读文案（日报/会话信息等），不截断
            return route_result.text or "执行完成"

        # 1) 清洗内部日志标签（[bus]/[WS]/[httpx]…），保留可能的 JSON 片段
        cleaned = self._sanitize_tool_result(raw)
        if not cleaned:
            # 整段都是内部噪声 → 直接走能力兜底话术
            return self._capability_fallback(route_result.capability)

        # 2) 尝试 JSON 解析（整段，或其中 JSON 子串）
        data = None
        try:
            data = json.loads(cleaned)
        except (json.JSONDecodeError, ValueError):
            m = self._JSON_SUBSTR_RE.search(cleaned)
            if m:
                try:
                    data = json.loads(m.group(0))
                except (json.JSONDecodeError, ValueError):
                    data = None

        if isinstance(data, dict):
            summary = self._summarize_dict_result(data, route_result.capability)
            if summary:
                return summary[:120]
        elif isinstance(data, list):
            if data:
                return f"共 {len(data)} 项结果，已为你整理好～"
            return "暂无结果"

        # 3) 非 JSON：清洗后仍是 JSON-ish / 内部噪声（[ { 开头）→ 能力兜底话术，
        #    否则保留可读文本截断（兼容正常的自然语言返回）
        if cleaned[0] in "{[":
            return self._capability_fallback(route_result.capability)
        if len(cleaned) <= 80:
            return cleaned
        return cleaned[:80] + "…"

    def _sanitize_tool_result(self, raw: str) -> str:
        """剥离内部总线/日志噪声，返回可展示的干净文本。

        处理 [bus] Queue migrated… / [WS]… / [httpx]… 这类打印进 tool_result 的
        内部日志：整行是标签日志则丢弃；若同行的标签后还有 JSON，则只保留 JSON。
        """
        kept: list[str] = []
        for ln in raw.splitlines():
            s = ln.strip()
            if not s:
                continue
            if self._INTERNAL_TAG_RE.match(s):
                # 先剥掉开头的 [tag]，再在剩余部分找 JSON 括号
                # （否则会命中标签自身的 [，误把整行当 JSON 片段保留）
                rest = self._INTERNAL_TAG_RE.sub("", s, count=1).lstrip()
                m = re.search(r'[\{\[]', rest)
                if not m:
                    continue  # 纯内部日志行，丢弃
                s = rest[m.start():]  # 仅保留标签后的 JSON 片段
            kept.append(s)
        return "\n".join(kept).strip()

    @staticmethod
    def _capability_fallback(capability: str) -> str:
        """按能力名兜底一句友好话，避免把内部日志/破损 JSON 泄漏到气泡。

        注意：动态路由后 capability 已是工具名（如 audio_bus / phone_peek_screen），
        不再使用旧的静态能力名（next_track/pause_music 等）。
        """
        friendly = {
            "audio_bus": "好的，已为你处理音乐～",
            "phone_peek_screen": "已帮你看了手机屏幕～",
            "phone_state": "已查看手机状态～",
            "phone_life_state": "已查看手机状态～",
            "phone_open_app": "已帮你打开应用～",
            "phone_home": "已回到手机桌面～",
            "phone_notification": "已查看通知～",
            "phone_alarm": "已设置闹钟～",
            "phone_status": "已查看手机状态～",
            "play_music": "给你放首歌～",
            "daily_diary": "今天的日报来啦～",
            "screenshot_now": "已帮你截图～",
        }
        if capability in friendly:
            return friendly[capability]
        return "好的，已经帮你处理好啦～"

    def _summarize_dict_result(self, data: dict, capability: str) -> str:
        """从工具返回的 JSON 对象中提取关键字段，拼一句自然语言摘要。

        字段优先级：计数(count/total) → 名称(name/title) → 状态(success/status/state)
        → 动作(action/type)。全部缺失时截断原始 JSON。
        """
        parts: list[str] = []

        count = data.get("count")
        if count is None:
            count = data.get("total")
        if isinstance(count, (int, float)) and not isinstance(count, bool):
            parts.append(f"共 {int(count)} 项")

        name = data.get("name") or data.get("title") or data.get("tool_name") or data.get("query")
        if name:
            parts.append(str(name)[:40])

        ok = data.get("success")
        if ok is None:
            ok = data.get("ok")
        if ok is True:
            parts.append("执行成功")
        elif ok is False:
            parts.append("执行失败")

        status = data.get("status") or data.get("state") or data.get("result")
        if status and not isinstance(status, (dict, list)):
            parts.append(f"状态：{str(status)[:40]}")

        action = data.get("action") or data.get("type")
        if action and not isinstance(action, (dict, list)):
            parts.append(f"动作：{str(action)[:40]}")

        if parts:
            return "，".join(parts)

        # 完全无法提取 → 截断原始 JSON
        try:
            raw = json.dumps(data, ensure_ascii=False)
        except (TypeError, ValueError):
            raw = str(data)
        return raw[:80] + "…"

    def _parse_action_directive(self, reply: str):
        """从回复中解析 JSON 动作指令。

        格式：正文末尾或任意位置附带 `{"action":"wave"}`（LLM 友好的轻量标记）。
        返回 (去标记后的正文, 动作名)；无指令返回 (None, None)。

        安全：只接受白名单动作名（避免任意字符串直接进 play_anim，
        也防止 LLM 幻觉出不存在的动作）。
        """
        if not reply or "action" not in reply:
            return None, None
        import re as _re
        # 匹配 {..., "action": "xxx", ...} / {"action":"xxx"}
        m = _re.search(r'\{\s*"action"\s*:\s*"([a-z_]+)"\s*\}', reply)
        if not m:
            return None, None
        act = m.group(1)
        if act not in self._ACTION_WHITELIST:
            logger.debug("动作指令不在白名单，忽略: %s", act)
            return None, None
        cleaned = _re.sub(r'\{\s*"action"\s*:\s*"[a-z_]+"\s*\}', "", reply).strip()
        # 剥掉标记后可能留下尾巴标点/空格；全标记则正文为空
        cleaned = (cleaned
                   .replace("\n\n", "\n")
                   .rstrip("，。,.。!！ "))
        return (cleaned, act)

    # AI 可调用的动作白名单（对应 live2d renderer / 精灵动画名）
    _ACTION_WHITELIST = frozenset({
        "wave", "happy", "thinking", "working", "sleep", "surprised",
        "sad", "angry", "waving", "walk", "mail", "complete", "special",
        "login", "wedding", "touch", "pat", "stroke", "cute", "idle",
    })

    def parse_action_intent(self, reply: str) -> tuple:
        """解析 [action:{...}] / [expression:xxx] / [duration:xxx] 结构化动作意图。

        格式示例：
        ``[action:{"gesture":"wave","intensity":0.8,"params":{"ParamAngleX":15,"ParamMouthOpenY":0.6}}]``
        ``[expression:smile=80,eye_smile=50]``  # 直接指定表情参数
        ``[duration:3]``  # 持续时间（秒）

        - 命中 → 返回 (去标记后的正文, intent_dict)，intent_dict ∈
          ``{"gesture": str, "intensity": float, "params": dict, "duration": float}``。
        - 无指令 / 标签非法 JSON → 返回 (正文, None)。

        与 [emotion:xxx] 解析互不干扰（后者由 adapter.parse_emotion 处理）。
        解析失败只剥掉标签、绝不抛异常（容错同 _read_file）。
        """
        if not reply:
            return reply, None
        import re as _re
        import json as _json

        # ── 先检查 [expression:xxx] / [duration:xxx]（可能没有 [action:xxx]）──
        # 这样 LLM 可以只输出 [expression:smile=80] 而不需要完整 [action:{...}]
        expr_params = {}
        duration = 0.0

        # 解析 [expression:smile=80,eye_smile=50] 格式
        expr_matches = _re.findall(r'\[expression:([^\]]+)\]', reply, flags=_re.IGNORECASE)
        for expr_str in expr_matches:
            for part in expr_str.split(','):
                part = part.strip()
                if '=' in part:
                    key, value = part.split('=', 1)
                    key = key.strip().lower()
                    try:
                        value = float(value.strip())
                        expr_params[key] = value
                    except ValueError:
                        logger.debug("conversation_engine: 非致命异常(已静默吞掉)", exc_info=True)
        # 剥离 [expression:...] 标签
        cleaned = _re.sub(r'\s*\[expression:[^\]]*\]\s*', ' ', reply, flags=_re.IGNORECASE)

        # 解析 [duration:3] 格式
        dur_match = _re.search(r'\[duration:([\d.]+)\]', reply, flags=_re.IGNORECASE)
        if dur_match:
            try:
                duration = float(dur_match.group(1))
            except ValueError:
                logger.debug("conversation_engine: 非致命异常(已静默吞掉)", exc_info=True)
        # 剥离 [duration:...] 标签
        cleaned = _re.sub(r'\s*\[duration:[^\]]*\]\s*', ' ', cleaned, flags=_re.IGNORECASE)

        # ── 解析 [action:{...}]（如果存在）──
        # 逐个扫描 [action:{...}]：按大括号配平定位闭合 }，再要求其后紧跟 ]。
        # 不用正则贪婪（``\{.*\}`` 配 DOTALL 会在一条回复含多个标签时把所有
        # 标签吞成一个非法 JSON，导致 intent=None、动态参数被静默丢弃并退化 emotion
        # 路径）。配平扫描可正确处理嵌套 params 与多标签（取最后一个合法标签）。
        intent = None
        if "[action:" in cleaned:
            i = 0
            n = len(reply)
            while True:
                start = reply.find("[action:", i)
                if start == -1:
                    break
                b = reply.find("{", start)
                if b == -1:
                    break
                depth = 0
                closed = -1
                j = b
                while j < n:
                    c = reply[j]
                    if c == "{":
                        depth += 1
                    elif c == "}":
                        depth -= 1
                        if depth == 0:
                            closed = j
                            break
                    j += 1
                # 未配平，或闭合 } 后不是 ] → 视为残缺标签，跳过继续向后找
                if closed == -1 or closed + 1 >= n or reply[closed + 1] != "]":
                    i = start + 1
                    continue
                full = reply[start:closed + 2]
                raw = reply[b:closed + 1]
                try:
                    obj = _json.loads(raw)
                except Exception:
                    obj = None
                if isinstance(obj, dict) and (obj.get("gesture") or obj.get("params")):
                    intent = obj
                # 无论 JSON 是否合法，都剥掉该标签
                cleaned = cleaned.replace(full, " ")
                i = closed + 2
        cleaned = _re.sub(r"\s{2,}", " ", cleaned).strip()
        
        # ── 合并 [expression:xxx] 和 [duration:xxx] 到 intent ──
        if expr_params or duration > 0:
            if intent is None:
                intent = {}
            # 合并 params（[expression:xxx] 的参数）
            if expr_params:
                existing_params = intent.get("params", {})
                if isinstance(existing_params, dict):
                    existing_params.update(expr_params)
                    intent["params"] = existing_params
                else:
                    intent["params"] = expr_params
            # 设置 duration
            if duration > 0:
                intent["duration"] = duration
        
        if intent is None:
            return cleaned, None
        return cleaned, intent

    def _analyze_reply_content(self, reply: str, emotion: str) -> dict | None:
        """P2: 桌宠输出分析 — AI 回复内容自动匹配动作（不用显式标签）。

        如果 AI 回复中没有 [action:{...}] 标签，尝试语义分析回复内容，
        根据关键词/短语触发对应动作。

        Args:
            reply: AI 回复文本
            emotion: 当前情绪

        Returns:
            动作意图字典（如果匹配到），否则 None
            格式: {"gesture": str, "intensity": float, "anim": str}
        """
        if not reply:
            return None

        # 定义语义分析规则（短语 → 动作映射）
        # 格式: (关键词列表, 动作ID, 强度)
        semantic_rules = [
            # 摸头/互动类
            (["摸摸", "揉揉", "顺毛", "拍头"], "touch", 0.7),
            (["抱", "拥抱", "抱一个"], "happy", 0.8),
            (["击掌", "give me five"], "waving", 0.9),
            
            # 工作/学习类
            (["工作", "学习", "写代码", "coding"], "thinking", 0.6),
            (["思考", "想想", "考虑"], "thinking", 0.5),
            
            # 休息/睡眠类
            (["睡觉", "休息", "困了"], "sleep", 0.5),
            (["累", "疲惫", "困"], "sleep", 0.4),
            
            # 情绪类
            (["开心", "快乐", "高兴"], "happy", 0.8),
            (["难过", "伤心", "哭"], "sad", 0.6),
            (["生气", "愤怒", "气死"], "angry", 0.7),
            (["害羞", "脸红", "不好意思"], "cute", 0.7),
            
            # 活动类
            (["散步", "走走", "出门"], "walk", 0.6),
            (["游戏", "开黑", "打游戏"], "happy", 0.7),
            (["喝茶", "咖啡"], "thinking", 0.6),
            
            # 特殊动作
            (["跳舞", "dance"], "special", 0.8),
            (["变身", "transform"], "special", 0.9),
        ]

        reply_lower = reply.lower()
        for keywords, action_id, intensity in semantic_rules:
            if any(kw.lower() in reply_lower for kw in keywords):
                # 映射到 renderer 可用的动作名
                anim_map = {
                    "touch": "touch",
                    "happy": "happy",
                    "waving": "waving",
                    "thinking": "thinking",
                    "sleep": "sleep",
                    "sad": "sad",
                    "angry": "angry",
                    "cute": "cute",
                    "walk": "walk",
                    "special": "special",
                }
                anim = anim_map.get(action_id, "idle")
                return {
                    "gesture": action_id,
                    "intensity": intensity,
                    "anim": anim,
                }

        return None

    def speak(self, text: str, emotion: str = "neutral", character_id: str = None, on_audio=None) -> None:
        """直接触发 TTS 合成（供 BubbleMixin 等外部调用）。
        
        与 _synth_and_reply 的区别：不回调 on_reply（气泡已显示，只需合成音频）。
        线程安全：异步提交到 TTS 线程池，不阻塞调用方。
        
        Args:
            on_audio: 音频合成完成后的回调函数，接收 audio_path 参数。
                      如果为 None，音频不会播放（仅合成，不触发口型）。
        """
        if not text or not text.strip() or text.strip() in ("…", "..."):
            return
        character_id = character_id or self._character_id
        instruct_map = {
            "happy": "开心", "sad": "难过", "angry": "生气",
            "cute": "可爱", "thinking": "思考",
        }
        instruct = instruct_map.get(emotion, "")
        try:
            self._tts_executor.submit(
                self._synth_only, text, emotion, character_id, instruct, on_audio,
            )
        except RuntimeError:
            logger.debug("TTS 线程池已关闭，跳过合成")
    
    def _synth_only(self, reply, emotion, character, instruct, on_audio=None):
        """在 TTS 线程池中执行：仅合成，不回调（供 speak 方法使用）。
        
        Args:
            on_audio: 音频合成完成后的回调函数，接收 audio_path 参数。
        """
        with self._lock:
            tts = self._tts
            tts_ready = self._tts_ready
            if tts is not None:
                self._tts_in_use += 1
        try:
            if tts and tts_ready and reply and reply.strip():
                try:
                    voice = ""
                    resolver = getattr(self, "_voice_resolver", None)
                    if callable(resolver):
                        try:
                            voice = resolver(character, emotion) or ""
                        except Exception:
                            logger.debug("conversation_engine: 非致命异常(已静默吞掉)", exc_info=True)
                    # 移除 emotion 标签
                    tts_text = reply
                    if reply:
                        try:
                            tts_text = self._adapter.parse_emotion(reply)[0]
                        except (AttributeError, Exception):
                            import re
                            tts_text = re.sub(r"\s*\[\s*emotion\s*[:=]\s*\w+\s*\]\s*", " ", reply, flags=re.IGNORECASE).strip()
                    audio_path = tts.synthesize(
                        tts_text, character_id=character, instruct=instruct, voice=voice,
                        emotion=emotion,
                    ) or ""
                    if audio_path:
                        logger.info("TTS done (speak): %s", os.path.basename(audio_path))
                        # 调用回调（让调用方决定如何播放音频）
                        if on_audio:
                            try:
                                on_audio(audio_path)
                            except Exception as e:
                                logger.debug("on_audio callback failed: %s", e)
                    else:
                        _err = getattr(tts, "last_error", "") or "未知"
                        logger.warning("TTS 合成失败 (speak)：原因=%s", _err)
                except Exception as e:
                    logger.warning("TTS error (speak): %s", e)
        finally:
            with self._lock:
                if self._tts_in_use > 0:
                    self._tts_in_use -= 1
    
    def _synth_and_reply(self, reply, emotion, anim, character, instruct, source, gen):
        """在 TTS 线程池中执行：合成 + 回调（on_reply 仍带 audio_path，口型链路不变）。"""
        # synth 前检查：已打断则不浪费算力
        if self._is_stale(gen):
            logger.debug("TTS 前已打断，丢弃: gen=%d", gen)
            return
        # 锁内捕获 tts/ready，避免中途被 TTSReload 换成 None/新实例
        with self._lock:
            tts = self._tts
            tts_ready = self._tts_ready
            if tts is not None:
                self._tts_in_use += 1  # 引用计数：让 TTSReload 知道此实例正在使用
        audio_path = ""
        # 已配置 provider 但未就绪（依赖缺失/网络不可用）：明确告警，避免“静默无语音”。
        if tts and not tts_ready:
            _reason = getattr(tts, "last_error", "") or "preload 未完成或依赖缺失"
            logger.warning(
                "TTS 已配置(provider=%s)但未就绪，跳过语音合成：%s",
                getattr(tts, "name", "?"), _reason,
            )
        try:
            if tts and tts_ready and reply and reply.strip() and reply.strip() not in ("\u2026", "..."):
                try:
                    # P2-7: 按「角色 + 情绪」解析本句生效音色（空 = provider 默认）。
                    # 解析器由 PetWindow 注入（多桌宠各自独立音色）；失败回退默认。
                    voice = ""
                    resolver = getattr(self, "_voice_resolver", None)
                    if callable(resolver):
                        try:
                            voice = resolver(character, emotion) or ""
                        except Exception as _ve:
                            logger.debug("voice 解析失败，使用默认音色: %s", _ve)
                            voice = ""
                    # 合成提示：本地 CosyVoice 一句要几十秒到两分钟（GPU 推理），
                    # 期间不提示的话，用户会误以为“没有语音”。
                    # 但是 on_status 信号跨线程 emit 可能顺序反转：先接到复位
                    # （hide_bubble），后接到“语音生成中”（_show_bubble），
                    # 导致气泡挂在界面上一去不回。文字先行（P1-6）已显示回复文本，
                    # 用户不需要“语音生成中”气泡——TTS 完成后直接播音频即可。
                    # 2026-08-22 修复：去掉合成前的 on_status 调用。
                    # 若需本地 CosyVoice 耗时提示，改用 on_progress。
                    # self.on_status("\U0001f50a 语音生成中…")
                    # BugFix: TTS 合成前移除 emotion 标签（避免读出 [emotion:xxx]）
                    tts_text = reply
                    if reply:
                        try:
                            tts_text = self._adapter.parse_emotion(reply)[0]
                        except (AttributeError, Exception):
                            # _adapter 可能不存在（测试环境），回退原始 reply
                            import re
                            tts_text = re.sub(r"\s*\[\s*emotion\s*[:=]\s*\w+\s*\]\s*", " ", reply, flags=re.IGNORECASE)
                            tts_text = re.sub(r"\s*\[expression:[^\]]*\]\s*", " ", tts_text, flags=re.IGNORECASE)
                            tts_text = re.sub(r"\s*\[duration:[^\]]*\]\s*", " ", tts_text, flags=re.IGNORECASE)
                            tts_text = tts_text.strip()
                    audio_path = tts.synthesize(
                        tts_text, character_id=character, instruct=instruct, voice=voice,
                        emotion=emotion,
                    ) or ""
                    if audio_path:
                        logger.info("TTS done: %s", os.path.basename(audio_path))
                    else:
                        # 合成未产出音频：把 provider 的真实错误（如 edge 网络不可达/
                        # edge-tts 未安装）亮出来，而不是静默只显示文字气泡。
                        _err = getattr(tts, "last_error", "") or "未知（网络不可达或服务不可用）"
                        logger.warning(
                            "TTS 合成失败：provider=%s 原因=%s",
                            getattr(tts, "name", "?"), _err,
                        )
                except Exception as e:
                    logger.warning("TTS error: %s", e)
                finally:
                    # 复位“语音生成中”状态，避免气泡长期卡在提示
                    try:
                        self.on_status("")
                    except Exception:
                        logger.debug("conversation_engine: 非致命异常(已静默吞掉)", exc_info=True)
        finally:
            with self._lock:
                if self._tts_in_use > 0:
                    self._tts_in_use -= 1
        # synth 后检查：已打断则丢弃脏音频，但文字气泡仍要显示——
        # 否则 proactive 慢回复(LLM 耗时数秒)期间用户一开口，整条回复连字都看不到。
        # 打断的语义是"旧话不继续说"，不是"旧话没说过"。
        if self._is_stale(gen):
            logger.debug("TTS 后已打断，仅保留文字气泡（丢弃音频）: gen=%d", gen)
            try:
                _call_reply_cb(self.on_reply, reply, emotion, anim, "", None)  # 空 audio_path → 只显示气泡不播音频
            except Exception as _e:
                logger.warning("打断后 on_reply 回调失败: %s", _e)
            return
        # 二次回调（文字 + 音频一起，从池线程 emit 信号 → 主线程播放口型）。
        # 仅当真正合成了音频才回调——文字已在文字先行阶段(P1-6)显示过，
        # 无音频时重复回调只会在 UI 侧造成冗余刷新（气泡续期 + 动画重放），
        # 且测试断言正常消息只回调一次（replies == ["hi"]）。
        if audio_path:
            try:
                _call_reply_cb(self.on_reply, reply, emotion, anim, audio_path, None)
            except Exception as _e:
                logger.warning("on_reply 回调失败: %s", _e)

    def _handle_tool_calls(self, resp: dict, user_text: str, character: str, perception_ctx: str, gen: int = None, max_iterations: int = 3) -> tuple:
        """处理 LLM 的 tool_calls：执行工具 → 结果回传 → 再次调用 LLM（最多 max_iterations 轮）

        Args:
            gen: 消息代际。工具执行中若被打断（代际过期），停止后续工具。
            max_iterations: 最大工具调用轮次，超过后强制终止（防死循环）。
        """
        iteration = 0
        while iteration < max_iterations:
            iteration += 1
            tool_calls = resp.get("tool_calls")
            if not tool_calls:
                # LLM 已返回纯文本，结束
                reply = (resp.get("message", {}) or {}).get("content", "") or "…"
                return reply, "neutral"

            assistant_message = resp.get("message", {})

            # 将 assistant 消息（含 tool_calls）加入历史
            self._adapter._history.append({
                "role": "assistant",
                "content": assistant_message.get("content", ""),
                "tool_calls": tool_calls,
            })

            # 逐个执行工具
            for tc in tool_calls:
                # P1：工具执行中被打断 → 停止后续工具
                if gen is not None and self._is_stale(gen):
                    logger.debug("工具执行中被打断，停止: gen=%d", gen)
                    return "已中断", "neutral"
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

            if iteration >= max_iterations:
                logger.warning("工具调用达到最大轮次(%d)，强制终止: %s", max_iterations, user_text[:30])
                return "操作已完成。", "neutral"

            # 再次调用 LLM，让模型基于工具结果生成最终回复
            try:
                follow_up, emotion = self._adapter.chat(
                    message="[工具执行完成，请根据结果用自然语言回复用户]",
                    inject_memory=False,
                    extra_context=perception_ctx,
                )
                # 构造下一轮 resp，检查是否还有 tool_calls
                if isinstance(follow_up, dict) and follow_up.get("tool_calls"):
                    resp = follow_up
                else:
                    return follow_up or "…", emotion or "neutral"
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
                logger.debug("conversation_engine: 非致命异常(已静默吞掉)", exc_info=True)
        self._session_unsubscribers.clear()

    def set_session(self, session_ref) -> None:
        """注入当前 Session 引用"""
        if self._adapter is not None:
            try:
                self._adapter.set_session(session_ref)
            except Exception as e:
                logger.warning("adapter.set_session 失败: %s", e)

    def create_new_session(self, agent_id: str = None, **kwargs) -> "object | None":
        """创建新 Session（供 pet.py 菜单"新建对话"调）

        Returns:
            SessionRef 或 None（创建失败）
        """
        if self._session_manager is None or not hasattr(self._session_manager, "create_session"):
            logger.warning("SessionManager 不可用，无法创建新 Session")
            return None
        try:
            aid = agent_id or self._agent_id
            # 2026-09-07: 从配置读取 session_memory_enabled（默认关闭，避免污染主会话）
            if "memory_enabled" not in kwargs:
                try:
                    from config import load_config
                    kwargs["memory_enabled"] = load_config().get("session_memory_enabled", False)
                except Exception:
                    kwargs["memory_enabled"] = False
            session = self._session_manager.create_session(agent_id=aid, **kwargs)
            self.set_session(session)
            # P2-10 修复：更新 adapter 的 pin 缓存，否则 chat_via_hanako
            # 还是复用旧 session（记忆被污染）。
            if self._adapter is not None:
                self._adapter._current_session = session
                self._adapter._agent_sessions[aid] = session
                self._adapter._agent_pinned[aid] = getattr(session, 'session_id', None)
                self._adapter._pinned_session_id = getattr(session, 'session_id', None)
                # 清空本地历史，避免旧上下文注入
                if hasattr(self._adapter, '_history'):
                    self._adapter._history.clear()
            logger.info("新 Session 已创建: %s (agent=%s, memory_enabled=%s)", 
                        getattr(session, "session_id", "?"), aid, kwargs.get("memory_enabled", "?"))
            return session
        except Exception as e:
            logger.error("create_session 失败: %s", e)
            return None

    @property
    def agent_id(self) -> str:
        """当前对话后端 agent（F2）"""
        return self._agent_id

    def set_agent(self, agent_id: str) -> bool:
        """设置对话后端 agent（F2，供 pet.py 从配置注入）"""
        if not agent_id:
            return False
        self._agent_id = str(agent_id).strip()
        return True

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
        """镜像来自 Hanako 主窗口或插件的外部回复。

        2026-09-08: 处理所有来源的回复（oc_pet/external），避免 Hana 会话回复后无气泡。
        
        缺陷③ 修复：打断/插队语义。当本地队列还有用户消息待处理（用户在等
        本地回复）时，外部镜像让位，避免两条回复音轨/气泡打架；本地空闲时
        镜像正常同步（用户在主窗口跟同一 agent 聊天，桌宠跟随显示）。
        """
        origin = getattr(result, "origin", "oc_pet")
        logger.info("[reply] _handle_session_reply called | origin=%s", origin)
        # 2026-09-08: 处理所有来源的回复（不再只处理 external）
        # if origin != "external":
        #     return
        if not self._is_current_session(getattr(result, "session", None)):
            return
        # 本地有 pending 用户消息（含正在处理的）→ 镜像让位
        with self._lock:
            pending_user = any(m.get("source") == "user" for m in self._queue)
        if pending_user:
            logger.debug("镜像回复让位（本地有 pending 用户消息）")
            return
        text, emotion = self._adapter.parse_emotion(getattr(result, "text", "") or "")
        _call_reply_cb(self.on_reply, text or "…", emotion, map_emotion_to_anim(emotion), "", None)

    def _handle_session_tool_progress(self, progress: "object") -> None:
        """接收 SessionManager 的 ToolProgress 事件，转发给 UI

        参考小蕾米插件的日志节流：同类(工具+phase)事件在节流窗口内合并，
        避免密集工具流（如 tool_progress 高频上报）把 UI/日志刷爆。
        """
        try:
            if not self._is_current_session(getattr(progress, "session", None)):
                return
            tool_name = getattr(progress, "tool_name", "tool")
            phase = getattr(progress, "phase", "progress")
            display = getattr(progress, "display_text", "") or self._tool_display(tool_name)
            success = getattr(progress, "success", None)
            # 节流：同 工具+phase 在窗口(默认 500ms)内只转发一次，
            # 高频 tool_progress 合并，UI 不闪屏。首条立即转发。
            try:
                _tl = getattr(self, "_tool_progress_throttle", {})
                now = time.time()
                key = f"{tool_name}|{phase}"
                last = _tl.get(key, 0)
                if now - last < self._tool_progress_throttle_ms:
                    return  # 节流窗口内，跳过（保留最新状态由 UI 自行刷新）
                _tl[key] = now
                self._tool_progress_throttle = _tl
            except Exception:
                logger.debug("conversation_engine: 非致命异常(已静默吞掉)", exc_info=True)
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
            self._adapter = HanakoPetAdapter(agent_id=character_id, builtin=self._builtin)
            if self._session_manager is not None:
                self._adapter.set_session_manager(self._session_manager)
            if hasattr(self._adapter, '_history'):
                self._adapter._history.clear()
            logger.info("角色切换: %s", character_id)
        except Exception as e:
            logger.error("角色切换失败: %s", e)

    def switch_agent(self, agent_id: str) -> bool:
        """切换对话后端 agent（F2/F4）。

        与 switch_character 不同：switch_agent 只换对话后端，不动显示角色。
        - 若 agent 与当前相同时无操作
        - 更新 adapter.agent_id，并恢复该 agent 的 session（F3 续聊）
        - 清空本地 history 防串味
        """
        if not agent_id or not str(agent_id).strip():
            return False
        agent_id = str(agent_id).strip()
        if agent_id == self._agent_id:
            return True
        with self._lock:
            self._queue.clear()
        self._agent_id = agent_id
        try:
            if self._adapter is not None:
                ok = self._adapter.switch_agent(agent_id)
                if ok:
                    logger.info("对话 agent 切换: %s", agent_id)
                    return True
            # adapter 未就绪或切换失败：重建（保留 agent_sessions 缓存）
            _adapter_builtin = self._builtin and (agent_id == self._character_id)
            new_adapter = HanakoPetAdapter(agent_id=agent_id, builtin=_adapter_builtin)
            if self._session_manager is not None:
                new_adapter.set_session_manager(self._session_manager)
                # 把旧 adapter 的 per-agent 会话缓存迁移过来
                old = self._adapter
                if old is not None:
                    new_adapter._agent_sessions = getattr(old, '_agent_sessions', {})
                    new_adapter._agent_pinned = getattr(old, '_agent_pinned', {})
            self._adapter = new_adapter
            # 恢复该 agent 的 session（若有）
            self._adapter._current_session = self._adapter._agent_sessions.get(agent_id)
            self._adapter._pinned_session_id = self._adapter._agent_pinned.get(agent_id)
            logger.info("对话 agent 切换(重建): %s", agent_id)
            return True
        except Exception as e:
            logger.error("agent 切换失败: %s", e)
            return False

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
