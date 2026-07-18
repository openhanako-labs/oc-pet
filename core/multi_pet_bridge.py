"""多桌宠协作桥接模块 (M4)

管理桌宠之间的事件通信、订阅/发布、状态管理和社交事件生成。

设计依据: docs/architecture-5-modules.md § M4
技术约束: 使用 threading + queue.Queue，不用 asyncio

用法:
    bridge = MultiPetBridge(pet_manager)
    bridge.start()

    # 注册桌宠
    bridge.register_pet("yuexinmiao", pet_window)

    # 手动发布事件
    from dataclasses import replace
    event = PetEvent(source_agent="yuexinmiao", event_type="cross_pet_chat",
                     payload={"text": "嘿，你在忙什么？"})
    bridge.publish(event)

    # 协作事件
    bridge.generate_social_event()

    # 停止
    bridge.stop()
"""
from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass, field
from queue import Empty, Queue
from typing import Any, Callable

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# 事件类型常量
# ──────────────────────────────────────────────

EVENT_TYPE_CHAT = "cross_pet_chat"
EVENT_TYPE_REACTION = "cross_pet_reaction"
EVENT_TYPE_COLLAB_CARE = "collab_care"
EVENT_TYPE_COLLAB_GIFT = "collab_gift"
EVENT_TYPE_PET_ENTER = "pet_enter"
EVENT_TYPE_PET_LEAVE = "pet_leave"

ALL_EVENT_TYPES = [
    EVENT_TYPE_CHAT,
    EVENT_TYPE_REACTION,
    EVENT_TYPE_COLLAB_CARE,
    EVENT_TYPE_COLLAB_GIFT,
    EVENT_TYPE_PET_ENTER,
    EVENT_TYPE_PET_LEAVE,
]

# ──────────────────────────────────────────────
# 数据类
# ──────────────────────────────────────────────


@dataclass
class PetEvent:
    """桌宠间事件

    Attributes:
        source_agent: 触发事件的桌宠 ID
        event_type:   事件类型 (见 EVENT_TYPE_*)
        payload:      事件数据 dict
        timestamp:    创建时间戳
        target_agent: 指定目标桌宠 (None = 广播给所有)
    """
    source_agent: str
    event_type: str
    payload: dict = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    target_agent: str | None = None


# ──────────────────────────────────────────────
# 事件总线
# ──────────────────────────────────────────────


class EventBus:
    """桌宠事件总线

    轻量级发布-订阅，进程内通信。
    线程安全：subscribe/publish 用锁保护，事件分发由后台线程串行执行。
    """

    def __init__(self, max_history: int = 50):
        self._subscribers: dict[str, list[tuple[str, Callable]]] = {}
        self._history: list[PetEvent] = []
        self._max_history = max_history
        self._lock = threading.Lock()

    # ── 订阅 / 取消订阅 ──

    def subscribe(self, event_type: str, handler: Callable, agent_id: str) -> None:
        """注册事件处理器

        Args:
            event_type: 事件类型
            handler:    回调函数，签名 handler(event: PetEvent) -> None
            agent_id:   订阅者标识（用于路由过滤）
        """
        with self._lock:
            self._subscribers.setdefault(event_type, []).append((agent_id, handler))
        logger.debug("Subscribed %s(%s) → %s", agent_id, event_type, handler.__qualname__)

    def unsubscribe(self, event_type: str, agent_id: str) -> None:
        """取消某 agent 对某事件类型的订阅"""
        with self._lock:
            subs = self._subscribers.get(event_type, [])
            self._subscribers[event_type] = [(aid, h) for aid, h in subs if aid != agent_id]
            if not self._subscribers[event_type]:
                del self._subscribers[event_type]
        logger.debug("Unsubscribed %s from %s", agent_id, event_type)

    # ── 发布 ──

    def publish(self, event: PetEvent) -> None:
        """将事件放入队列，由后台线程处理分发

        不直接调用 handler，而是入队后由 _dispatch 线程安全地串行处理。
        """
        with self._lock:
            self._history.append(event)
            if len(self._history) > self._max_history:
                self._history.pop(0)

        logger.info("[EventBus] Publish: %s from %s (target=%s)",
                     event.event_type, event.source_agent, event.target_agent)

    def get_history(self) -> list[PetEvent]:
        """返回最近事件历史（只读快照）"""
        with self._lock:
            return list(self._history)

    def get_subscribers(self, event_type: str) -> list[tuple[str, Callable]]:
        """获取某事件类型的全部订阅者"""
        with self._lock:
            return list(self._subscribers.get(event_type, []))


# ──────────────────────────────────────────────
# 协作场景库
# ──────────────────────────────────────────────

COLLABORATION_SCENARIOS: list[dict[str, Any]] = [
    {
        "name": "送水事件",
        "trigger": "user_busy_long + pet_idle",
        "description": "A 桌宠发现用户在忙很久，让空闲的 B 桌宠送去一杯咖啡",
        "dialogue": [
            {"from": "pet_a", "text": "主人好像很忙呢...", "emotion": "thinking"},
            {"from": "pet_b", "text": "我去给主人倒杯水吧！☕", "emotion": "happy"},
        ],
    },
    {
        "name": "一起玩耍",
        "trigger": "user_gaming + multiple_pets",
        "description": "用户在玩游戏，多个桌宠一起评论",
        "dialogue": [
            {"from": "pet_a", "text": "哇，你在打游戏！带我一个～", "emotion": "happy"},
            {"from": "pet_b", "text": "我也想看！让我坐在你肩膀上", "emotion": "cute"},
        ],
    },
    {
        "name": "深夜闲聊",
        "trigger": "late_night + idle_pets",
        "description": "深夜两个桌宠偷偷聊天",
        "dialogue": [
            {"from": "pet_a", "text": "你也还没睡呀...", "emotion": "thinking"},
            {"from": "pet_b", "text": "嗯，在数星星呢 ✨", "emotion": "cute"},
        ],
    },
    {
        "name": "互相吐槽",
        "trigger": "multiple_pets + user_away",
        "description": "用户不在时，桌宠们开始闲聊吐槽",
        "dialogue": [
            {"from": "pet_a", "text": "你说主人今天会早点回来吗？", "emotion": "thinking"},
            {"from": "pet_b", "text": "别想了，他上次这么说已经是上周了 😤", "emotion": "annoyed"},
        ],
    },
    {
        "name": "合作关怀",
        "trigger": "user_working_late",
        "description": "多个桌宠分工协作关心用户",
        "dialogue": [
            {"from": "pet_a", "text": "都凌晨两点了还在工作...", "emotion": "concern"},
            {"from": "pet_b", "text": "我放个轻音乐吧，你帮我盯着进度条～", "emotion": "care"},
        ],
    },
    {
        "name": "礼物接力",
        "trigger": "special_day + multiple_pets",
        "description": "节日/特殊日子，桌宠们接力送祝福",
        "dialogue": [
            {"from": "pet_a", "text": "今天是主人的生日！第一个祝福我来！", "emotion": "excited"},
            {"from": "pet_b", "text": "那我准备第二个！🎁", "emotion": "happy"},
        ],
    },
]


# ──────────────────────────────────────────────
# 社交事件生成器
# ──────────────────────────────────────────────


class SocialEventGenerator:
    """社交事件生成器

    基于场景库 + 随机策略生成桌宠间对话事件。
    支持冷却间隔控制，避免刷屏。
    """

    def __init__(self, min_interval: float = 30 * 60):
        self._min_interval = min_interval
        self._last_social_event = 0.0
        self._scenario_pool = COLLABORATION_SCENARIOS
        logger.info("[SocialEventGenerator] Initialized (cooldown=%.0fs, scenarios=%d)",
                     min_interval, len(self._scenario_pool))

    def generate(
        self,
        available_agents: list[str],
        bridge: MultiPetBridge,
        event_type: str = "random",
    ) -> PetEvent | None:
        """生成一个社交事件并发布

        Args:
            available_agents: 当前在线桌宠 ID 列表
            bridge:           MultiPetBridge 引用（用于发布事件）
            event_type:       强制指定事件类型，"random" 则随机选择

        Returns:
            生成的 PetEvent，若冷却中或参数不足则返回 None
        """
        now = time.time()
        if now - self._last_social_event < self._min_interval:
            remaining = self._min_interval - (now - self._last_social_event)
            logger.debug(
                "[SocialEventGenerator] Cooldown active, %.0fs remaining", remaining
            )
            return None

        if len(available_agents) < 2:
            logger.debug(
                "[SocialEventGenerator] Need ≥2 agents, got %d", len(available_agents)
            )
            return None

        # 选场景
        scenario = self._pick_scenario(event_type)
        if not scenario:
            return None

        # 选参与者
        participants = random.sample(available_agents, min(2, len(available_agents)))

        # 构建对话文本
        dialogue_parts = []
        for turn in scenario.get("dialogue", []):
            pet_idx = 0 if turn["from"] == "pet_a" else 1
            if pet_idx < len(participants):
                dialogue_parts.append(
                    f"{participants[pet_idx]}: {turn['text']} ({turn['emotion']})"
                )

        text = "\n".join(dialogue_parts) if dialogue_parts else scenario["description"]

        # 发布协作对话事件
        event = PetEvent(
            source_agent="system",
            event_type=EVENT_TYPE_CHAT,
            payload={
                "scenario": scenario["name"],
                "description": scenario["description"],
                "dialogue": scenario["dialogue"],
                "participants": participants,
                "text": text,
            },
        )

        bridge.publish(event)
        self._last_social_event = now
        logger.info(
            "[SocialEventGenerator] Generated scenario '%s' with %s",
            scenario["name"], participants,
        )
        return event

    def _pick_scenario(self, event_type: str) -> dict | None:
        """选择一个场景"""
        if event_type == "random":
            return random.choice(self._scenario_pool)
        # 按名称匹配
        for s in self._scenario_pool:
            if s["name"] == event_type:
                return s
        return None


# ──────────────────────────────────────────────
# 多宠协作桥接器 (核心)
# ──────────────────────────────────────────────


class MultiPetBridge:
    """多桌宠协作桥接器

    管理桌宠间的通信和协作事件。由 PetManager 持有，所有 PetWindow 共享。

    架构:
        PetManager ──owns──> MultiPetBridge
                                │
                                ├── EventBus (内存发布-订阅)
                                ├── EventDispatcher (后台线程处理队列)
                                ├── PetRegistry (在线桌宠状态)
                                └── SocialEventGenerator (协作事件生成)

    线程模型:
        - 主线程: 调用 publish/subscribe/register_pet (通过队列异步分发)
        - 后台线程: EventDispatcher 从队列取出事件并分发给 handler
    """

    def __init__(
        self,
        pet_manager: Any = None,
        queue_size: int = 256,
        dispatcher_thread_name: str = "MultiPetDispatcher",
    ):
        self._manager = pet_manager
        self._queue: Queue[PetEvent] = Queue(maxsize=queue_size)
        self._event_bus = EventBus()
        self._social_generator = SocialEventGenerator()

        # 桌宠注册表: agent_id -> info dict
        self._pets: dict[str, dict[str, Any]] = {}
        self._pets_lock = threading.Lock()

        # 后台调度线程
        self._dispatcher_thread: threading.Thread | None = None
        self._dispatcher_running = False
        self._dispatcher_name = dispatcher_thread_name

        logger.info(
            "[MultiPetBridge] Created (queue_size=%d, manager=%s)",
            queue_size,
            type(pet_manager).__name__ if pet_manager else "None",
        )

    # ── 生命周期 ──

    def start(self) -> None:
        """启动后台事件调度线程"""
        if self._dispatcher_running:
            logger.warning("[MultiPetBridge] Already running")
            return

        self._dispatcher_running = True
        self._dispatcher_thread = threading.Thread(
            target=self._dispatch_loop,
            name=self._dispatcher_name,
            daemon=True,
        )
        self._dispatcher_thread.start()
        logger.info("[MultiPetBridge] Started dispatcher thread: %s",
                     self._dispatcher_thread.name)

    def stop(self) -> None:
        """停止后台线程，清空队列"""
        if not self._dispatcher_running:
            return

        self._dispatcher_running = False
        self._queue.put_nowait(None)  # sentinel to wake up the loop

        if self._dispatcher_thread and self._dispatcher_thread.is_alive():
            self._dispatcher_thread.join(timeout=5.0)

        logger.info("[MultiPetBridge] Stopped")

    # ── 事件队列 (publish / consume) ──

    def publish(self, event: PetEvent) -> None:
        """发布事件到队列

        事件先入队，由后台线程消费并分发给订阅者。
        队列满时丢弃最旧的事件（非阻塞）。
        """
        try:
            self._queue.put_nowait(event)
        except Exception:
            # 队列满了，丢弃最旧的
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(event)
                logger.warning("[MultiPetBridge] Queue full, dropped oldest event")
            except Exception:
                logger.error("[MultiPetBridge] Failed to enqueue event: %s", event)

    def _dispatch_loop(self) -> None:
        """后台线程：从队列取事件并分发"""
        logger.info("[MultiPetBridge] Dispatcher loop started")

        while self._dispatcher_running:
            try:
                event = self._queue.get(timeout=1.0)
            except Empty:
                continue

            # sentinel value: stop signal
            if event is None:
                logger.info("[MultiPetBridge] Dispatcher loop received stop signal")
                break

            self._dispatch_event(event)

        logger.info("[MultiPetBridge] Dispatcher loop ended")

    def _dispatch_event(self, event: PetEvent) -> None:
        """分发单个事件到所有匹配的订阅者

        在后台线程中串行执行，保证事件顺序。
        """
        handlers = self._event_bus.get_subscribers(event.event_type)

        if not handlers:
            logger.debug(
                "[MultiPetBridge] No handlers for %s (from %s)",
                event.event_type, event.source_agent,
            )
            return

        matched = 0
        for agent_id, handler in handlers:
            # 路由过滤
            if event.target_agent is not None and event.target_agent != agent_id:
                continue

            # 自己不发给自己（除非是系统广播）
            if event.source_agent != "system" and event.source_agent == agent_id:
                continue

            try:
                handler(event)
                matched += 1
                logger.debug(
                    "[MultiPetBridge] Dispatched %s → %s (%s)",
                    event.event_type, agent_id, handler.__qualname__,
                )
            except Exception as e:
                logger.error(
                    "[MultiPetBridge] Handler error for %s on %s: %s",
                    event.event_type, agent_id, e,
                )

        logger.info(
            "[MultiPetBridge] Event '%s' dispatched to %d/%d handlers",
            event.event_type, matched, len(handlers),
        )

    # ── 订阅 / 取消 ──

    def subscribe(self, event_type: str, handler: Callable, agent_id: str) -> None:
        """订阅某个事件类型

        Args:
            event_type: 事件类型
            handler:    回调函数，签名 handler(event: PetEvent) -> None
            agent_id:   订阅者标识
        """
        self._event_bus.subscribe(event_type, handler, agent_id)

    def unsubscribe(self, event_type: str, agent_id: str) -> None:
        """取消订阅"""
        self._event_bus.unsubscribe(event_type, agent_id)

    # ── 桌宠状态管理 ──

    def register_pet(self, agent_id: str, pet_window: Any = None) -> None:
        """注册一个桌宠

        Args:
            agent_id:     桌宠 agent ID
            pet_window:   对应的 PetWindow 实例（可选）
        """
        with self._pets_lock:
            if agent_id in self._pets:
                logger.warning("[MultiPetBridge] Pet %s already registered, updating",
                               agent_id)
            self._pets[agent_id] = {
                "agent_id": agent_id,
                "pet_window": pet_window,
                "registered_at": time.time(),
                "status": "online",
            }

        # 自动广播 pet_enter 事件
        enter_event = PetEvent(
            source_agent="system",
            event_type=EVENT_TYPE_PET_ENTER,
            payload={"agent_id": agent_id},
        )
        self.publish(enter_event)
        logger.info("[MultiPetBridge] Registered pet: %s (total=%d)",
                     agent_id, len(self._pets))

    def unregister_pet(self, agent_id: str) -> None:
        """注销一个桌宠"""
        with self._pets_lock:
            info = self._pets.pop(agent_id, None)

        if info:
            # 取消该 agent 的所有订阅
            self._unsubscribe_all(agent_id)

            # 广播 pet_leave 事件
            leave_event = PetEvent(
                source_agent="system",
                event_type=EVENT_TYPE_PET_LEAVE,
                payload={"agent_id": agent_id},
            )
            self.publish(leave_event)

            logger.info("[MultiPetBridge] Unregistered pet: %s (remaining=%d)",
                        agent_id, len(self._pets))
        else:
            logger.warning("[MultiPetBridge] Unregister unknown pet: %s", agent_id)

    def get_online_pets(self) -> list[str]:
        """获取在线桌宠 ID 列表"""
        with self._pets_lock:
            return [
                info["agent_id"]
                for info in self._pets.values()
                if info.get("status") == "online"
            ]

    def get_pet_info(self, agent_id: str) -> dict | None:
        """获取指定桌宠的信息"""
        with self._pets_lock:
            return self._pets.get(agent_id)

    def _unsubscribe_all(self, agent_id: str) -> None:
        """取消某 agent 的所有事件订阅"""
        for event_type in list(self._event_bus._subscribers.keys()):
            self._event_bus.unsubscribe(event_type, agent_id)

    # ── 协作事件生成 ──

    def generate_social_event(self, event_type: str = "random") -> PetEvent | None:
        """生成一个协作社交事件

        从可用桌宠中随机选取至少 2 个，生成对话事件并发布。

        Args:
            event_type: 强制指定场景类型，"random" 则随机

        Returns:
            生成的 PetEvent 或 None
        """
        online = self.get_online_pets()
        if len(online) < 2:
            logger.debug(
                "[MultiPetBridge] generate_social_event: need ≥2 pets, got %d",
                len(online),
            )
            return None

        result = self._social_generator.generate(online, self, event_type=event_type)
        return result

    # ── 快捷方法 ──

    def send_chat(
        self,
        from_agent: str,
        text: str,
        to_agent: str | None = None,
        emotion: str = "neutral",
    ) -> PetEvent:
        """发送跨宠对话的快捷方法"""
        event = PetEvent(
            source_agent=from_agent,
            event_type=EVENT_TYPE_CHAT,
            payload={"text": text, "emotion": emotion},
            target_agent=to_agent,
        )
        self.publish(event)
        return event

    def send_reaction(
        self,
        from_agent: str,
        reaction: str,
        to_agent: str | None = None,
    ) -> PetEvent:
        """发送回应的快捷方法"""
        event = PetEvent(
            source_agent=from_agent,
            event_type=EVENT_TYPE_REACTION,
            payload={"reaction": reaction},
            target_agent=to_agent,
        )
        self.publish(event)
        return event

    def send_care(
        self,
        from_agent: str,
        care_type: str,
        to_agent: str | None = None,
    ) -> PetEvent:
        """发送协作关心的快捷方法"""
        event = PetEvent(
            source_agent=from_agent,
            event_type=EVENT_TYPE_COLLAB_CARE,
            payload={"care_type": care_type},
            target_agent=to_agent,
        )
        self.publish(event)
        return event

    def send_gift(
        self,
        from_agent: str,
        gift_name: str,
        to_agent: str | None = None,
    ) -> PetEvent:
        """发送协作礼物的快捷方法"""
        event = PetEvent(
            source_agent=from_agent,
            event_type=EVENT_TYPE_COLLAB_GIFT,
            payload={"gift_name": gift_name},
            target_agent=to_agent,
        )
        self.publish(event)
        return event

    # ── 状态查询 ──

    @property
    def pet_count(self) -> int:
        """在线桌宠数量"""
        return len(self.get_online_pets())

    @property
    def queue_size(self) -> int:
        """当前事件队列大小"""
        return self._queue.qsize()

    @property
    def is_running(self) -> bool:
        """后台线程是否运行中"""
        return self._dispatcher_running

    def status_summary(self) -> dict:
        """获取桥接器状态摘要"""
        return {
            "running": self._dispatcher_running,
            "pet_count": self.pet_count,
            "online_pets": self.get_online_pets(),
            "queue_size": self.queue_size,
            "subscriber_count": sum(
                len(v) for v in self._event_bus._subscribers.values()
            ),
            "event_history_count": len(self._event_bus._history),
        }
