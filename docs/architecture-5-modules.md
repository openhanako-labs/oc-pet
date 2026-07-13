# OC Desktop Pet — 五大借鉴模块架构设计

> 从 Fritia_Online_NEXT 借鉴 5 个功能模块，集成到 OC Desktop Pet 项目。
> 目标：让桌宠从"问答式陪伴"进化为"有叙事、有记忆、有协作的桌面生态"。

---

## 0. 现状快照

```
PetManager（多桌宠管理器）
  ├─ PetWindow ── ConversationEngine ── HanakoPetAdapter (LLM)
  │    ├─ SpriteRenderer (精灵渲染 - 2D)
  │    ├─ MouseTracker (鼠标交互)
  │    ├─ PerceptionController (感知)
  │    │    ├─ TimePerception (时间感知)
  │    │    ├─ ScreenWatcher (屏幕感知 - 截图+视觉模型)
  │    │    ├─ ProactiveScheduler (主动对话)
  │    │    └─ EmotionStateMachine (情绪)
  │    ├─ Bubble (对话气泡)
  │    └─ PluginPanel (插件面板)
  └─ SettingsDialog (设置)
```

**已有能力**：多桌宠并行、屏幕感知（定时截屏+视觉模型）、主动对话（规则引擎）、情绪状态机、TTS/ASR、插件工具调用、Hanako 生态集成（identity/memory/facts）。

**核心约束**：保持 PySide6 + Python，不引入 Three.js；后台陪伴不占资源；渐进式可独立实现。

---

## 1. 模块架构图

```
┌─────────────────────────────────────────────────────────────────────┐
│                        PetWindow (每个 agent)                       │
│                                                                     │
│  ┌──────────┐   ┌──────────────┐   ┌──────────────────────────┐    │
│  │Narrative │   │Environment   │   │  Memory Snapshot          │    │
│  │Engine    │◄─►│Awareness     │   │  Export/Import            │    │
│  │(M1)      │   │(M2)          │   │  (M3)                     │    │
│  └────┬─────┘   └──────┬───────┘   └────────────┬─────────────┘    │
│       │                │                         │                  │
│  ┌────▼────────────────▼─────────────────────────▼──────────────┐  │
│  │              PerceptionController (已有，扩展)                 │  │
│  │  TimePerception │ ScreenPerception │ ProactiveScheduler       │  │
│  │  EmotionStateMachine │ VirtualObjectRegistry                  │  │
│  └──────────────────────────────────────────────────────────────┘  │
│                           ▲                                         │
│  ┌───────────────────────┼───────────────────────┐                 │
│  │  MultiPet Bridge (M4)  │  CharacterPackage (M5) │                 │
│  └───────────────────────┴────────────────────────┘                 │
│                           ▲                                         │
│               ┌───────────┼───────────┐                             │
│               │           │           │                              │
│        ConversationEngine  HanakoMonitor  PetManager                │
│        (已有)         (已有)        (已有)                            │
└─────────────────────────────────────────────────────────────────────┘
```

### 模块关系矩阵

| 模块 | 依赖 | 被依赖 | 与已有组件交互 |
|------|------|--------|----------------|
| M1 叙事引擎 | LLM Adapter | PetWindow, ConversationEngine | 注入感知上下文 |
| M2 环境感知 | ScreenPerception, ForegroundWatcher | Narrative Engine | 扩展虚拟物件注册表 |
| M3 记忆快照 | HanakoContext (memory/) | 所有 PetWindow | 读写 memory/facts/today |
| M4 多宠协作 | PetManager | 所有 PetWindow | 跨窗口事件分发 |
| M5 角色包 | CharacterPackage | PetManager, SettingsDialog | 读写 characters/ 目录 |

---

## 2. 模块详细设计

---

### M1: LLM 叙事生成引擎

**问题**：当前对话是问答式的——用户说→LLM 答。缺少"无事发生时的故事"和"小事件驱动的情感积累"。

**目标**：让 LLM 在空闲时生成"桌面小事件"，而非被动等待用户输入。

#### 数据流

```
ProactiveScheduler (空闲超时触发)
    │
    ▼
NarrativeEngine.request_event()
    │
    ├─ 读取感知上下文 (时间/情绪/屏幕/前台应用)
    ├─ 读取记忆上下文 (memory.md + facts.md)
    ├─ 读取角色 identity.md
    │
    ▼
LLM Prompt (叙事模板)
    │
    ▼
LLM 返回: { type: "micro_event", content: "...", emotion: "happy", action: "draw" }
    │
    ├─ 类型: micro_event / greeting / observation / joke / question
    ├─ 内容: 叙事文本
    ├─ 情绪: happy/sad/thinking/...
    └─ 可选动作: draw / sit / wave / look / sleep
    │
    ▼
NarrativeEngine.apply()
    ├─ 显示气泡文本
    ├─ 触发动画序列
    └─ 写入 memory/today.md (记忆沉淀)
```

#### 接口定义

```python
# core/narrative_engine.py

class NarrativeEvent(NamedTuple):
    """叙事事件数据结构"""
    event_type: str           # micro_event / greeting / observation / joke / question
    content: str              # 叙事文本
    emotion: str              # happy / sad / thinking / surprised / neutral
    animation: str | None     # 可选动画: draw / sit / wave / look / sleep
    priority: float = 0.5     # 优先级 0-1，高优先级打断低优先级
    source: str = "narrative" # 来源: narrative / proactive / user_initiated


class NarrativeEngine:
    """叙事生成引擎
    
    职责:
    1. 在空闲时段生成微事件 (micro_event)
    2. 将叙事结果注入对话历史
    3. 维护叙事上下文 (最近 N 个事件)
    """
    
    def __init__(self, character_id: str, perception: PerceptionController,
                 memory_budget: int = 800):
        self._character_id = character_id
        self._perception = perception
        self._memory_budget = memory_budget
        self._event_queue: list[NarrativeEvent] = []
        self._recent_events: list[dict] = []  # 最近 20 个事件 (用于上下文)
        self._lock = threading.Lock()
        
        # 叙事冷却: 两次叙事之间至少间隔 N 分钟
        self._cooldown_seconds = 15 * 60
        
    async def request_event(self) -> NarrativeEvent | None:
        """请求一个叙事事件 (异步，不阻塞主线程)"""
        ...
    
    def apply(self, event: NarrativeEvent, callback: callable):
        """应用叙事事件到 UI
        
        callback: Callable[[str, str, str], None]  # reply, emotion, anim
        """
        ...
    
    def build_narrative_prompt(self) -> str:
        """构建叙事 prompt (不含用户消息)"""
        ...
```

#### 叙事 Prompt 模板

```python
NARRATIVE_PROMPT_TEMPLATE = """
你是 {name}，一只桌面宠物。现在没有人在和你说话，但你想主动分享一些东西。

【当前情境】
{context}

【最近发生的事】
{recent_events}

【你的记忆】
{memory}

请生成一个简短的桌面小事件。可以是：
- 看到用户在做什么的有趣观察
- 一句随口的话
- 一个小动作的描述
- 一个轻松的笑话或吐槽

输出格式：
{type}: micro_event
{content}: 你的叙事文本 (不超过 2 句话)
{emotion}: happy/sad/thinking/surprised/neutral
{animation}: draw/sit/wave/look/sleep (可选)

规则：
1. 保持角色一致性
2. 不要重复最近发生过的事件
3. 语气自然，像朋友随口说的话
4. 如果实在没什么好说的，生成一个安静的观察
"""
```

#### 伪代码实现

```python
# core/narrative_engine.py

async def request_event(self) -> NarrativeEvent | None:
    now = time.time()
    if now - self._last_event_time < self._cooldown_seconds:
        return None
    
    context = self._perception.build_context()
    recent = "\n".join(f"- {e['content']}" for e in self._recent_events[-5:]) or "(刚安静了一会儿)"
    memory = self._read_memory_context()
    
    prompt = NARRATIVE_PROMPT_TEMPLATE.format(
        name=self._character_id,
        context=context,
        recent_events=recent,
        memory=memory
    )
    
    try:
        resp = await self._call_llm_narrative(prompt)
        event = self._parse_narrative_response(resp)
        
        with self._lock:
            self._recent_events.append({
                "content": event.content,
                "type": event.event_type,
                "time": datetime.now().isoformat(),
            })
            if len(self._recent_events) > 20:
                self._recent_events.pop(0)
            self._last_event_time = now
        
        return event
    except Exception as e:
        logger.warning("Narrative generation failed: %s", e)
        return None
```

---

### M2: 环境感知 + 虚拟物件

**问题**：当前屏幕感知只返回一句话描述。无法识别具体应用/文件，也无法让桌宠"放置"虚拟物件。

**目标**：
1. 细粒度环境识别（识别具体应用名、文件类型、窗口标题关键词）
2. 虚拟物件系统（桌宠可以"放置"小物件在桌面上）

#### 数据流

```
ForegroundWatcher (已有: 前台窗口检测)
    │
    ▼
EnhancedEnvironmentScanner
    │
    ├─ 窗口标题解析 → 应用名 + 文件名 + 状态
    │  例: "main.py - VS Code" → {app: "VS Code", file: "main.py", type: "code"}
    │  例: "Steam" → {app: "Steam", type: "gaming"}
    │
    ├─ 文件扫描 (可选) → 最近访问文件列表
    │  读取 Windows Recent 或 Hanako workspace
    │
    └─ 虚拟物件注册表
         │
         ▼
VirtualObjectRegistry
    ├─ 物件定义: {id, emoji, label, position, duration, action}
    ├─ 物件池: 预定义物件库 (咖啡杯、书本、星星、便签...)
    └─ 物件生命周期管理 (创建/显示/消失)
```

#### 接口定义

```python
# core/enhanced_environment.py

@dataclass
class EnvironmentSnapshot:
    """环境快照"""
    foreground_app: str           # 前台应用名
    window_title: str             # 完整窗口标题
    category: str                 # writing / development / gaming / communication / other
    detected_files: list[str]     # 检测到的文件名 (从标题解析)
    file_types: dict[str, str]    # 文件名 → 类型 (code/document/game/video)
    screen_description: str       # 屏幕感知一句话
    time_context: dict            # 时间上下文


class EnhancedEnvironmentScanner:
    """增强环境扫描器
    
    在 ForegroundWatcher 基础上增加：
    - 文件名解析
    - 文件类型推断
    - 关键词匹配
    """
    
    FILE_TYPE_PATTERNS = {
        "code": [".py", ".js", ".ts", ".html", ".css", ".json", ".md"],
        "document": [".docx", ".pdf", ".txt", ".xlsx"],
        "image": [".png", ".jpg", ".psd", ".fig"],
        "video": [".mp4", ".mov", ".avi"],
        "game": ["steam", "epic", "minecraft", "valorant", "lol"],
    }
    
    def parse_window_title(self, title: str) -> tuple[str, list[str]]:
        """从窗口标题解析应用名和文件名"""
        ...
    
    def infer_file_type(self, filename: str) -> str:
        """推断文件类型"""
        ...
    
    def scan(self) -> EnvironmentSnapshot:
        """生成完整环境快照"""
        ...


# ui/virtual_objects.py

class VirtualObject:
    """虚拟物件
    
    桌宠可以在桌面上"放置"虚拟物件，
    这些物件以 emoji + 标签的形式短暂显示在桌宠旁边。
    """
    
    def __init__(self, emoji: str, label: str, position: tuple[int, int] = (0, 0),
                 duration: int = 10):
        self.emoji = emoji        # 🎮 ☕ 📖 ⭐ 💡 🍕
        self.label = label        # "一杯咖啡" / "Steam 打开了！"
        self.position = position  # 相对于桌宠的偏移
        self.duration = duration  # 显示时长 (秒)
        self.created_at = time.time()
    
    def is_expired(self) -> bool:
        return time.time() - self.created_at > self.duration


class VirtualObjectRegistry:
    """虚拟物件注册表
    
    管理所有桌宠放置的虚拟物件，
    支持物件的创建、显示、过期清理。
    """
    
    # 预定义物件库
    DEFAULT_OBJECTS = {
        "coffee":    {"emoji": "☕", "label": "一杯热咖啡", "duration": 15},
        "snack":     {"emoji": "🍕", "label": "零食时间！", "duration": 10},
        "book":      {"emoji": "📖", "label": "在看书呢", "duration": 20},
        "star":      {"emoji": "⭐", "label": "你真棒！", "duration": 8},
        "idea":      {"emoji": "💡", "label": "想到好主意了", "duration": 12},
        "game":      {"emoji": "🎮", "label": "打游戏去！", "duration": 10},
        "sleepy":    {"emoji": "😴", "label": "困了...", "duration": 15},
        "writing":   {"emoji": "✏️", "label": "在写东西", "duration": 20},
    }
    
    def place(self, object_key: str, position: tuple[int, int] = None) -> VirtualObject:
        """放置一个虚拟物件"""
        ...
    
    def clear_expired(self):
        """清理过期的物件"""
        ...
    
    def get_active(self) -> list[VirtualObject]:
        """获取当前活跃的物件列表"""
        ...
```

#### 伪代码实现

```python
# core/enhanced_environment.py

def scan(self) -> EnvironmentSnapshot:
    # 1. 获取前台窗口信息 (复用 ForegroundWatcher)
    app_name = self._foreground_watcher.last_app_name or ""
    title = self._foreground_watcher.last_title or ""
    category = self._foreground_watcher.last_category or "other"
    
    # 2. 解析文件名
    detected_files = []
    file_types = {}
    # 尝试从标题提取文件名 (常见模式: "filename.ext - App Name")
    parts = re.split(r'\s*[-–—]\s*', title)
    for part in parts[:-1]:  # 最后一部分通常是应用名
        part = part.strip()
        if '.' in part:
            detected_files.append(part)
            file_types[part] = self.infer_file_type(part)
    
    # 3. 屏幕感知描述
    screen_desc = self._perception.screen.last_description or ""
    
    # 4. 时间上下文
    time_ctx = self._perception.time.get_context()
    
    return EnvironmentSnapshot(
        foreground_app=app_name,
        window_title=title,
        category=category,
        detected_files=detected_files,
        file_types=file_types,
        screen_description=screen_desc,
        time_context=time_ctx,
    )
```

---

### M3: 记忆快照导出/导入

**问题**：当前记忆分散在 Hanako 的各个文件中（memory.md, facts.md, today.md, longterm.md），
用户无法打包带走或分享给别人。

**目标**：
1. 一键导出为可读 JSON（结构化 + 可读的"回忆录"）
2. 导入时智能合并（礼物记录合并、好感度取高值、去重）
3. 支持跨设备同步

#### 数据流

```
MemorySnapshotExporter
    │
    ├─ 读取:
    │   ├─ memory.md (对话记忆)
    │   ├─ facts.md (事实知识)
    │   ├─ today.md (今日状态)
    │   ├─ longterm.md (长期记忆)
    │   ├─ pinned-memory.json (置顶记忆)
    │   └─ conversation_history.json (对话历史)
    │
    ├─ 转换:
    │   ├─ 结构化 (JSON Schema)
    │   ├─ 可读摘要 (Markdown 回忆录)
    │   └─ 加密选项 (可选)
    │
    ▼
MemorySnapshot (JSON)
    {
      "version": "1.0",
      "agent_id": "ophelia",
      "exported_at": "2026-07-13T12:00:00",
      "identity": { ... },
      "memories": {
        "recent": [...],
        "facts": [...],
        "longterm": [...],
        "pinned": [...]
      },
      "conversation_log": [...],
      "metadata": {
        "total_messages": 1234,
        "first_conversation": "2026-01-01",
        "last_conversation": "2026-07-13",
        "emotional_summary": { "happy": 45, "thinking": 30, ... }
      }
    }
```

#### 接口定义

```python
# core/memory_snapshot.py

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class MemorySnapshot:
    """记忆快照数据结构"""
    version: str = "1.0"
    agent_id: str = ""
    exported_at: str = ""
    
    # 身份 (从 identity.md + description.md 提取)
    identity: dict = field(default_factory=dict)
    
    # 记忆
    memories: dict = field(default_factory=lambda: {
        "recent": [],      # 最近对话摘要
        "facts": [],       # 事实知识
        "longterm": [],    # 长期记忆
        "pinned": [],      # 置顶记忆
    })
    
    # 对话日志 (可选，可能很大)
    conversation_log: list[dict] = field(default_factory=list)
    
    # 元数据
    metadata: dict = field(default_factory=dict)


class MemorySnapshotExporter:
    """记忆快照导出器"""
    
    def __init__(self, character_id: str, context: HanakoContext):
        self._character_id = character_id
        self._context = context
    
    def export(self, include_conversations: bool = False, 
               output_path: str = None) -> MemorySnapshot:
        """导出记忆快照"""
        ...
    
    def save_as_file(self, snapshot: MemorySnapshot, path: str):
        """保存为 JSON 文件"""
        ...
    
    def generate_recall_book(self, snapshot: MemorySnapshot) -> str:
        """生成可读的 Markdown 回忆录"""
        ...


class MemorySnapshotImporter:
    """记忆快照导入器"""
    
    def __init__(self, character_id: str, context: HanakoContext):
        self._character_id = character_id
        self._context = context
    
    def import_and_merge(self, snapshot: MemorySnapshot, 
                         strategy: str = "merge") -> dict:
        """导入并合并记忆
        
        Strategy:
        - "merge": 增量合并 (新事实添加, 旧事实保留, 对话追加)
        - "replace": 完全替换 (覆盖现有记忆)
        - "smart": 智能合并 (冲突检测 + 用户确认)
        """
        ...
    
    def _merge_facts(self, new_facts: list, existing_facts: list) -> list:
        """合并事实知识 (去重 + 冲突检测)"""
        ...
    
    def _merge_memories(self, new_memories: list, existing_memories: list) -> list:
        """合并对话记忆 (按时间排序 + 容量限制)"""
        ...
```

#### 合并策略详解

```python
# 合并逻辑 (对应 Fritia 的增量合并)

def merge_logic(new_item: dict, existing_items: list) -> list:
    """
    通用合并策略:
    
    1. 事实类 (facts):
       - 相同 key + 不同 value → 保留较新的 (或标记冲突)
       - 完全重复 → 跳过
       - 新增 → 添加
    
    2. 记忆类 (memories):
       - 按时间戳排序
       - 容量上限: 最近 N 条 (如 50)
       - 相似去重: embedding 相似度 > 0.9 视为重复
    
    3. 对话日志 (conversations):
       - 追加到新结尾
       - 不覆盖已有对话
    
    4. 好感度/礼物记录:
       - 同一天同一礼物 → 数量累加
       - 同一天不同礼物 → 全部保留
       - 好感度 → 取最大值
    """
    ...
```

---

### M4: 多桌宠协作事件

**问题**：当前多个桌宠各自独立运行，互不通信。缺少桌宠之间的互动。

**目标**：
1. 桌宠之间的对话（用户旁观，类似"暖调闲聚"）
2. 协作事件（A 桌宠发现用户在忙，让 B 桌宠去送杯水）
3. 随机社交事件（桌宠们闲聊、吵架、合作）

#### 数据流

```
PetManager (已有)
    │
    ▼
MultiPetBridge (新增)
    │
    ├─ 事件总线 (EventBus)
    │   ├─ 桌宠 A 触发: "user_is_busy" 事件
    │   ├─ 桥接器广播给所有在线桌宠
    │   └─ 桌宠 B 响应: "offer_help" 事件
    │
    ├─ 协作调度器
    │   ├─ 检测到互补场景 (A 在忙 + B 空闲 → B 主动)
    │   └─ 生成协作对话
    │
    └─ 社交事件生成器
        ├─ 随机触发 (每 N 小时)
        ├─ 条件触发 (特定时间/应用)
        └─ 用户触发 (右键菜单 "让她们聊聊")
```

#### 接口定义

```python
# core/multi_pet_bridge.py

@dataclass
class PetEvent:
    """桌宠间事件"""
    source_agent: str           # 触发事件的桌宠 ID
    event_type: str             # user_is_busy / user_free / greeting / joke / collaboration
    payload: dict               # 事件数据
    timestamp: float = field(default_factory=time.time)
    target_agent: str | None = None  # 指定目标桌宠 (None = 广播)


class EventBus:
    """桌宠事件总线
    
    轻量级发布-订阅，进程内通信。
    """
    
    def __init__(self):
        self._subscribers: dict[str, list[callable]] = {}
        self._history: list[PetEvent] = []  # 最近 50 个事件
        self._max_history = 50
    
    def subscribe(self, event_type: str, handler: callable, agent_id: str):
        """注册事件处理器"""
        self._subscribers.setdefault(event_type, []).append((agent_id, handler))
    
    def publish(self, event: PetEvent):
        """发布事件"""
        with self._lock:
            self._history.append(event)
            if len(self._history) > self._max_history:
                self._history.pop(0)
        
        # 分发给订阅者
        handlers = self._subscribers.get(event.event_type, [])
        for agent_id, handler in handlers:
            if event.target_agent is None or event.target_agent == agent_id:
                try:
                    handler(event)
                except Exception as e:
                    logger.warning("Event handler error for %s: %s", event.event_type, e)


class MultiPetBridge:
    """多桌宠协作桥接器
    
    管理桌宠间的通信和协作事件。
    由 PetManager 持有，所有 PetWindow 共享。
    """
    
    def __init__(self, pet_manager: 'PetManager'):
        self._manager = pet_manager
        self._event_bus = EventBus()
        self._collaboration_scheduler = CollaborationScheduler(self)
        self._social_generator = SocialEventGenerator(self)
        
        # 注册默认事件处理器
        self._register_default_handlers()
    
    def register_pet(self, agent_id: str, pet_window: 'PetWindow'):
        """注册一个桌宠窗口"""
        self._event_bus.subscribe("greeting", 
                                  lambda e: pet_window.receive_greeting(e),
                                  agent_id)
        self._event_bus.subscribe("collaboration",
                                  lambda e: pet_window.receive_collaboration(e),
                                  agent_id)
    
    def trigger_social_event(self, event_type: str = "random"):
        """触发一个社交事件"""
        self._social_generator.generate(event_type)
    
    def broadcast(self, event: PetEvent):
        """广播事件到所有桌宠"""
        self._event_bus.publish(event)
```

#### 协作事件生成

```python
# core/collaboration_events.py

COLLABORATION_SCENARIOS = [
    {
        "name": "送水事件",
        "trigger": "user_busy_long + pet_idle",
        "description": "A 桌宠发现用户在忙很久，让空闲的 B 桌宠送去一杯咖啡",
        "dialogue": [
            {"from": "pet_a", "text": "主人好像很忙呢...", "emotion": "thinking"},
            {"from": "pet_b", "text": "我去给主人倒杯水吧！☕", "emotion": "happy"},
        ]
    },
    {
        "name": "一起玩耍",
        "trigger": "user_gaming + multiple_pets",
        "description": "用户在玩游戏，多个桌宠一起评论",
        "dialogue": [
            {"from": "pet_a", "text": "哇，你在打游戏！带我一个～", "emotion": "happy"},
            {"from": "pet_b", "text": "我也想看！让我坐在你肩膀上", "emotion": "cute"},
        ]
    },
    {
        "name": "深夜闲聊",
        "trigger": "late_night + idle_pets",
        "description": "深夜两个桌宠偷偷聊天",
        "dialogue": [
            {"from": "pet_a", "text": "你也还没睡呀...", "emotion": "thinking"},
            {"from": "pet_b", "text": "嗯，在数星星呢 ✨", "emotion": "cute"},
        ]
    },
]


class SocialEventGenerator:
    """社交事件生成器
    
    基于场景库 + LLM 即兴生成桌宠间对话。
    """
    
    def __init__(self, bridge: MultiPetBridge):
        self._bridge = bridge
        self._scenario_pool = COLLABORATION_SCENARIOS
        self._last_social_event = 0.0
        self._min_interval = 30 * 60  # 至少 30 分钟一次
    
    def generate(self, event_type: str = "random") -> PetEvent | None:
        now = time.time()
        if now - self._last_social_event < self._min_interval:
            return None
        
        # 1. 选择场景
        scenario = self._pick_scenario(event_type)
        if not scenario:
            return None
        
        # 2. 用 LLM 即兴生成对话 (基于场景模板)
        dialogue = self._generate_dialogue(scenario)
        
        # 3. 发布事件
        event = PetEvent(
            source_agent="system",
            event_type="social",
            payload={"scenario": scenario["name"], "dialogue": dialogue},
        )
        self._bridge.broadcast(event)
        
        self._last_social_event = now
        return event
    
    def _pick_scenario(self, event_type: str) -> dict | None:
        """根据当前环境选择一个合适的场景"""
        # 简单版本: 随机选择
        # 高级版本: 基于时间/用户活动/桌宠状态加权选择
        return random.choice(self._scenario_pool)
    
    def _generate_dialogue(self, scenario: dict) -> list[dict]:
        """用 LLM 基于场景模板即兴生成对话"""
        # 调用 LLM，传入场景描述和角色 identity
        # 返回格式化的对话列表
        ...
```

---

### M5: 角色包系统

**问题**：当前角色数据散落在 `characters/<agent>/` 和 `~/.hanako/agents/<agent>/` 中，
用户无法方便地分享或迁移角色。

**目标**：
1. 统一的"角色包"格式 (`.pet` 文件 = `.zip` 压缩包)
2. 包含: identity + knowledge + memories + sprites + config
3. 一键导入/导出
4. 用户可分享"角色包"到社区

#### 角色包结构

```
my-character.pet  (本质是 .zip 文件)
├── manifest.json           # 角色包元信息
├── identity.md             # 角色身份定义
├── ishiki.md               # 意识/行为规则
├── description.md          # 角色描述
├── public-ishiki.md        # 对外可见规则
├── sprites/                # 精灵资源
│   ├── atlas.webp          # 精灵图集
│   ├── idle.png            # 备用单帧
│   └── frames/             # 帧动画目录
│       ├── idle/
│       ├── walk/
│       └── extra/
├── pet.json                # 动画配置 (复用现有格式)
├── knowledge/              # 知识库
│   ├── facts.md            # 事实知识
│   └── lore.md             # 世界观设定
├── memories/               # 记忆 (可选，导入时可选择是否携带)
│   ├── memory.md
│   ├── today.md
│   └── facts.md
└── config.json             # 角色专属配置 (TTS/Voice 等)
```

#### manifest.json 格式

```json
{
  "format_version": "1.0",
  "package_type": "character",
  "id": "my-character",
  "name": "我的角色",
  "author": "月曦夜",
  "version": "1.0.0",
  "description": "一个可爱的桌面宠物角色",
  "tags": ["cat", "office", "cute"],
  "created_at": "2026-07-13T12:00:00",
  "updated_at": "2026-07-13T12:00:00",
  "dependencies": [],
  "required_hanako_version": ">=0.110.0"
}
```

#### 接口定义

```python
# core/character_package.py

import zipfile
import json
from pathlib import Path
from dataclasses import dataclass, field


CHARACTER_PACKAGE_VERSION = "1.0"
PACKAGE_EXTENSIONS = [".pet", ".zip"]


@dataclass
class CharacterPackageInfo:
    """角色包信息 (解析后的 manifest)"""
    package_id: str = ""
    name: str = ""
    author: str = ""
    version: str = ""
    description: str = ""
    tags: list[str] = field(default_factory=list)
    has_sprites: bool = False
    has_memories: bool = False
    has_knowledge: bool = False


class CharacterPackage:
    """角色包管理器
    
    职责:
    1. 导出角色为 .pet 文件
    2. 导入 .pet 文件并安装角色
    3. 浏览已安装的角色包
    4. 验证角色包完整性
    """
    
    # 角色包必需文件
    REQUIRED_FILES = ["manifest.json", "identity.md"]
    # 角色包推荐文件
    RECOMMENDED_FILES = ["ishiki.md", "description.md", "pet.json", "sprites/"]
    
    def __init__(self, characters_dir: Path, hanako_agents_dir: Path):
        self._characters_dir = characters_dir
        self._hanako_agents_dir = hanako_agents_dir
    
    def export(self, agent_id: str, output_path: str,
               include_memories: bool = False) -> str:
        """导出角色为 .pet 文件"""
        ...
    
    def import_package(self, package_path: str, 
                       install_to: str = "hanako") -> CharacterPackageInfo:
        """导入角色包
        
        install_to:
        - "hanako": 安装到 ~/.hanako/agents/<id>/
        - "local": 安装到 characters/<id>/ (项目内置)
        - "both": 同时安装到两处
        """
        ...
    
    def list_packages(self, include_installed: bool = True) -> list[CharacterPackageInfo]:
        """列出所有可用的角色包"""
        ...
    
    def validate(self, package_path: str) -> tuple[bool, list[str]]:
        """验证角色包完整性
        
        Returns:
            (is_valid, missing_files)
        """
        ...
    
    def create_from_scratch(self, agent_id: str, name: str, 
                            identity_content: str, sprite_dir: str = None) -> str:
        """从零创建一个新的角色包"""
        ...
```

#### 伪代码实现

```python
def export(self, agent_id: str, output_path: str,
           include_memories: bool = False) -> str:
    """导出角色为 .pet 文件"""
    
    # 1. 收集角色文件
    source_dir = self._hanako_agents_dir / agent_id
    char_dir = self._characters_dir / agent_id
    
    files_to_pack = []
    
    # 必选: identity.md
    for src in [source_dir, char_dir]:
        f = src / "identity.md"
        if f.exists():
            files_to_pack.append(("identity.md", f))
            break
    
    # 可选: ishiki, description, public-ishiki
    for fname in ["ishiki.md", "description.md", "public-ishiki.md"]:
        for src in [source_dir, char_dir]:
            f = src / fname
            if f.exists():
                files_to_pack.append((fname, f))
                break
    
    # 精灵资源
    for src in [source_dir / "pet", char_dir]:
        if src.exists():
            for f in src.rglob("*"):
                if f.is_file() and f.suffix in ['.png', '.webp', '.json']:
                    rel = f.relative_to(src)
                    files_to_pack.append((f"sprites/{rel}", f))
            break
    
    # 动画配置
    for src in [source_dir, char_dir]:
        f = src / "pet.json"
        if f.exists():
            files_to_pack.append(("pet.json", f))
            break
    
    # 记忆 (可选)
    if include_memories:
        mem_dir = source_dir / "memory"
        if mem_dir.exists():
            for f in mem_dir.iterdir():
                if f.is_file():
                    files_to_pack.append((f"memories/{f.name}", f))
    
    # 2. 生成 manifest
    manifest = {
        "format_version": CHARACTER_PACKAGE_VERSION,
        "package_type": "character",
        "id": agent_id,
        "name": self._read_name(agent_id),
        "author": "OC Desktop Pet",
        "version": "1.0.0",
        "created_at": datetime.now().isoformat(),
    }
    
    # 3. 打包为 zip
    with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for arcname, filepath in files_to_pack:
            zf.write(filepath, arcname)
        zf.writestr("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))
    
    return output_path


def import_package(self, package_path: str,
                   install_to: str = "hanako") -> CharacterPackageInfo:
    """导入角色包"""
    
    # 1. 打开 zip，读取 manifest
    with zipfile.ZipFile(package_path, 'r') as zf:
        manifest_raw = zf.read("manifest.json")
        manifest = json.loads(manifest_raw)
        
        # 2. 验证
        info = CharacterPackageInfo(
            package_id=manifest.get("id", ""),
            name=manifest.get("name", ""),
            author=manifest.get("author", ""),
            version=manifest.get("version", ""),
        )
        
        # 3. 解压到目标目录
        target_dir = self._get_target_dir(install_to, manifest["id"])
        target_dir.mkdir(parents=True, exist_ok=True)
        
        for info in zf.infolist():
            if info.filename == "manifest.json":
                continue
            zf.extract(info, target_dir)
        
        # 4. 更新 config.json
        self._update_config(manifest["id"])
    
    return info
```

---

## 3. 模块间接口规范

### 3.1 PerceptionController 扩展

```python
# 在现有 PerceptionController 中新增:

class PerceptionController:
    # ... 现有代码 ...
    
    # M2: 环境感知扩展
    @property
    def environment(self) -> EnhancedEnvironmentScanner:
        """增强环境扫描器 (懒加载)"""
        if not hasattr(self, '_environment'):
            self._environment = EnhancedEnvironmentScanner(self)
        return self._environment
    
    # M1: 叙事事件注入
    def set_narrative_callback(self, callback: callable):
        """设置叙事事件回调"""
        self._narrative_callback = callback
    
    def inject_narrative_context(self) -> str:
        """在 build_context 中注入叙事上下文"""
        # 返回最近 N 个叙事事件的摘要
        ...
```

### 3.2 ConversationEngine 扩展

```python
# 在现有 ConversationEngine 中新增:

class ConversationEngine:
    # ... 现有代码 ...
    
    # M1: 叙事引擎集成
    def set_narrative_engine(self, engine: NarrativeEngine):
        """设置叙事引擎"""
        self._narrative_engine = engine
    
    def handle_narrative_event(self, event: NarrativeEvent):
        """处理叙事事件 (由 NarrativeEngine 触发)"""
        self.on_reply(event.content, event.emotion, 
                      event.animation or "idle", "")
    
    # M4: 多宠协作事件接收
    def receive_cross_pet_event(self, event: PetEvent):
        """接收来自其他桌宠的事件"""
        # 根据事件类型生成回复
        ...
```

### 3.3 PetManager 扩展

```python
# 在现有 PetManager 中新增:

class PetManager:
    # ... 现有代码 ...
    
    # M4: 多宠桥接
    def __init__(self):
        # ... 现有代码 ...
        self._bridge = MultiPetBridge(self)
    
    @property
    def bridge(self) -> MultiPetBridge:
        return self._bridge
    
    # M5: 角色包管理
    def __init__(self):
        # ... 现有代码 ...
        self._package_manager = CharacterPackage(
            characters_dir=CHARACTERS_DIR,
            hanako_agents_dir=AGENTS_DIR,
        )
    
    @property
    def package_manager(self) -> CharacterPackage:
        return self._package_manager
    
    def export_character(self, agent_id: str, path: str):
        return self._package_manager.export(agent_id, path)
    
    def import_character(self, path: str, install_to: str = "hanako"):
        return self._package_manager.import_package(path, install_to)
```

---

## 4. 实现优先级与路线图

### Phase 1: 基础叙事 + 环境增强 (2-3 周)

| 模块 | 优先级 | 依赖 | 预估工作量 |
|------|--------|------|-----------|
| M1 叙事引擎 | P0 | 无 | 3-5 天 |
| M2 环境感知 | P1 | M1 的感知上下文 | 2-3 天 |

**理由**：
- M1 是最核心的体验升级，让桌宠从"问答机器人"变成"有生命的陪伴"
- M2 是 M1 的数据源，环境越精准，叙事越真实
- 两者组合即可产生明显体验提升

**交付物**：
- `core/narrative_engine.py` — 叙事生成引擎
- `core/enhanced_environment.py` — 增强环境扫描
- `ui/virtual_objects.py` — 虚拟物件显示
- 配置项: `config.json` → `narrative.enabled`, `narrative.cooldown_minutes`

### Phase 2: 记忆系统 + 角色包 (2-3 周)

| 模块 | 优先级 | 依赖 | 预估工作量 |
|------|--------|------|-----------|
| M3 记忆快照 | P1 | 无 (独立) | 2-3 天 |
| M5 角色包 | P2 | M3 (共享导出逻辑) | 3-5 天 |

**理由**：
- M3 是用户留存的关键——"我的回忆"可以打包带走
- M5 是社区生态的基础——角色包可分享
- 两者可以并行开发

**交付物**：
- `core/memory_snapshot.py` — 记忆导出/导入
- `core/character_package.py` — 角色包管理
- `ui/settings_dialog.py` 新增 "导出角色包" / "导入角色包" 按钮
- 文件格式: `.pet` (zip)

### Phase 3: 多宠协作 (3-4 周)

| 模块 | 优先级 | 依赖 | 预估工作量 |
|------|--------|------|-----------|
| M4 多宠协作 | P2 | M1 (叙事复用) | 5-7 天 |

**理由**：
- 依赖 M1 的叙事引擎作为对话生成器
- 需要 PetManager 层面改造 (事件总线)
- 体验加分项，但不是核心功能

**交付物**：
- `core/multi_pet_bridge.py` — 多宠桥接
- `core/collaboration_events.py` — 协作事件
- `ui/pet_window.py` 新增跨宠事件处理
- 配置项: `config.json` → `multi_pet.enabled`, `social_event_interval`

---

## 5. 技术方案细节

### 5.1 叙事引擎的 LLM 调用优化

**问题**：叙事事件生成需要额外调用 LLM，增加 API 成本。

**方案**：
1. **缓存层**：相同情境下复用上次生成的叙事（hash 情境 → 缓存叙事）
2. **本地模型兜底**：当 API 不可用时，使用预设模板生成
3. **批量生成**：一次性生成 3-5 个候选叙事，按需选取

```python
# 情境 hash 缓存
def _compute_context_hash(context: str) -> str:
    """计算情境 hash，用于缓存命中"""
    import hashlib
    return hashlib.md5(context.encode()).hexdigest()[:12]

def _get_cached_narrative(context_hash: str) -> NarrativeEvent | None:
    """从缓存读取叙事"""
    cache_file = self._get_cache_path(context_hash)
    if cache_file.exists():
        data = json.loads(cache_file.read_text())
        if time.time() - data["timestamp"] < 3600:  # 缓存 1 小时
            return NarrativeEvent(**data["event"])
    return None
```

### 5.2 虚拟物件的 UI 渲染

**方案**：在 PetWindow 中新增一个 QLabel 覆盖层，显示虚拟物件。

```python
# ui/virtual_object_overlay.py

class VirtualObjectOverlay(QWidget):
    """虚拟物件显示层
    
    在桌宠窗口上叠加显示 emoji + 标签，
    支持淡入淡出动画。
    """
    
    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        
        self._label = QLabel(self)
        self._label.setAlignment(Qt.AlignCenter)
        self._label.setFont(QFont("Segoe UI Emoji", 24))
        
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._fade_out)
    
    def show_object(self, emoji: str, label: str, position: tuple[int, int]):
        """显示虚拟物件"""
        self._label.setText(f"{emoji}\n{label}")
        self.move(position[0], position[1])
        self.show()
        self._timer.start(5000)  # 5 秒后自动消失
    
    def _fade_out(self):
        """淡出隐藏"""
        self._timer.stop()
        self.hide()
```

### 5.3 记忆快照的文件存储

**方案**：使用 Hanako 现有的 memory/ 目录结构，导出时打包为 JSON。

```python
# 存储路径约定
MEMORY_DIR = Path.home() / ".hanako" / "agents" / "<agent_id>" / "memory"

# 导出时读取的文件
FILES_TO_EXPORT = [
    "memory.md",       # 对话记忆
    "today.md",        # 今日状态
    "facts.md",        # 事实知识
    "longterm.md",     # 长期记忆
    "pinned.md",       # 置顶记忆
    "pinned-memory.json",  # 置顶记忆 (结构化)
]

# 导入时写入的路径
FILES_TO_IMPORT = {
    "memories.recent": "memory.md",
    "memories.facts": "facts.md",
    "memories.longterm": "longterm.md",
    "memories.pinned": "pinned-memory.json",
}
```

### 5.4 多宠通信的线程安全

**方案**：使用 asyncio.Queue + 事件循环，避免多线程竞争。

```python
# 事件总线实现 (兼容现有 threading 架构)
from queue import Queue
from PySide6.QtCore import Signal, QObject

class EventBus(QObject):
    """跨桌宠事件总线
    
    使用 Qt 信号槽 + threading 实现，兼容现有架构。
    后台线程处理事件队列，通过 Qt 信号安全通知 UI。
    """
    
    # Qt 信号：事件到达时通知 UI 线程
    event_received = Signal(str, dict)  # (event_type, payload)
    
    def __init__(self):
        super().__init__()
        self._handlers: dict[str, list[callable]] = {}
        self._queue: Queue = Queue()
        self._running = False
        self._thread = None
    
    def start(self):
        """启动事件处理循环（后台线程）"""
        self._running = True
        self._thread = threading.Thread(target=self._process_loop, daemon=True)
        self._thread.start()
    
    def stop(self):
        """停止事件处理"""
        self._running = False
    
    def publish(self, event_type: str, payload: dict):
        """发布事件到队列"""
        self._queue.put((event_type, payload))
    
    def subscribe(self, event_type: str, handler: callable):
        """订阅事件"""
        if event_type not in self._handlers:
            self._handlers[event_type] = []
        self._handlers[event_type].append(handler)
    
    def _process_loop(self):
        """后台线程处理事件队列"""
        while self._running:
            try:
                event_type, payload = self._queue.get(timeout=1.0)
                # 通过 Qt 信号安全地通知 UI 线程
                self.event_received.emit(event_type, payload)
                # 同步调用已注册的 handler
                self._dispatch(event_type, payload)
            except Exception as e:
                if self._running:  # 忽略关闭时的超时
                    logger.error("EventBus error: %s", e)
    
    def _dispatch(self, event_type: str, payload: dict):
        """分发事件到所有订阅者"""
        handlers = self._handlers.get(event_type, [])
        for handler in handlers:
            try:
                handler(payload)
            except Exception as e:
                logger.error("Event handler error: %s", e)
```

---

## 6. 风险评估与备选方案

### R1: LLM 叙事生成 — API 成本高

| 风险等级 | 高 |
|----------|-----|
| 影响 | 频繁调用 LLM 导致 API 费用激增 |
| 缓解措施 | 1. 缓存命中 (情境 hash) 2. 提高冷却时间 3. 本地模板兜底 4. 用户可关闭叙事功能 |
| 备选方案 | 使用小型本地模型 (如 Ollama + Llama 3) 专门处理叙事生成 |

### R2: 屏幕感知 — 性能开销

| 风险等级 | 中 |
|----------|-----|
| 影响 | 截屏 + 视觉分析消耗 CPU/GPU |
| 缓解措施 | 1. 变化检测 (已有) 2. 降低截屏频率 3. 失败退避 (已有) 4. 可配置间隔 |
| 备选方案 | 仅使用 ForegroundWatcher (窗口标题) 不做截屏分析 |

### R3: 多宠协作 — 复杂度爆炸

| 风险等级 | 中 |
|----------|-----|
| 影响 | 桌宠间对话可能导致混乱或重复 |
| 缓解措施 | 1. 限制同时对话的桌宠数量 (最多 2 个) 2. 强制冷却时间 3. 用户可手动触发 |
| 备选方案 | 先实现单向通知 (A → B)，暂不实现双向对话 |

### R4: 角色包 — 兼容性问题

| 风险等级 | 低 |
|----------|-----|
| 影响 | 不同版本的 Hanako/OC Pet 格式不兼容 |
| 缓解措施 | 1. manifest.json 中声明 required_hanako_version 2. 导入时做版本检查 3. 提供降级提示 |
| 备选方案 | 角色包仅导出/导入 identity.md + sprites，不携带记忆 |

### R5: 记忆快照 — 隐私问题

| 风险等级 | 中 |
|----------|-----|
| 影响 | 导出的记忆可能包含敏感信息 |
| 缓解措施 | 1. 导出时可选过滤 2. 加密导出文件 3. 用户手动审查后再分享 |
| 备选方案 | 默认不导出对话日志，仅导出 facts + longterm |

---

## 7. 配置文件变更

```jsonc
// config.json 新增字段

{
  "agents": [ ... ],
  
  // M1: 叙事引擎配置
  "narrative": {
    "enabled": true,
    "cooldown_minutes": 15,          // 两次叙事间的最小间隔
    "max_recent_events": 20,          // 保留的最近叙事事件数
    "prefer_local_template": false,   // 优先使用本地模板 (节省 API)
    "scenarios": ["observation", "joke", "care", "comment"]  // 允许的场景类型
  },
  
  // M2: 环境感知配置
  "environment": {
    "scan_window_title": true,        // 扫描窗口标题解析文件名
    "virtual_objects_enabled": true,  // 启用虚拟物件
    "object_display_duration": 10     // 物件显示时长 (秒)
  },
  
  // M3: 记忆快照配置
  "snapshot": {
    "auto_backup": false,             // 是否自动备份记忆
    "backup_interval_hours": 24,      // 自动备份间隔
    "include_conversations": false    // 导出时是否包含对话日志
  },
  
  // M4: 多宠协作配置
  "multi_pet": {
    "enabled": false,                 // 默认关闭，用户手动开启
    "social_event_interval": 60,      // 社交事件最小间隔 (分钟)
    "max_concurrent_pets": 2,         // 最多同时参与对话的桌宠数
    "auto_collaborate": false         // 是否自动触发协作事件
  },
  
  // M5: 角色包配置
  "packages": {
    "install_dir": "~/.hanako/agents/",  // 角色包安装目录
    "community_feed": ""                // 社区角色包源 URL (预留)
  }
}
```

---

## 8. 文件变更清单

### 新增文件

| 文件路径 | 说明 | 所属模块 |
|----------|------|----------|
| `core/narrative_engine.py` | 叙事生成引擎 | M1 |
| `core/enhanced_environment.py` | 增强环境扫描器 | M2 |
| `ui/virtual_object_overlay.py` | 虚拟物件 UI 层 | M2 |
| `core/memory_snapshot.py` | 记忆快照导出/导入 | M3 |
| `core/multi_pet_bridge.py` | 多宠协作桥接 | M4 |
| `core/collaboration_events.py` | 协作事件生成 | M4 |
| `core/character_package.py` | 角色包管理 | M5 |
| `ui/settings_dialog.py` | 扩展: 新增角色包管理页 | M5 |

### 修改文件

| 文件路径 | 变更内容 | 所属模块 |
|----------|----------|----------|
| `core/perception.py` | 新增 `environment` 属性 | M2 |
| `core/conversation_engine.py` | 新增 `handle_narrative_event` / `receive_cross_pet_event` | M1, M4 |
| `pet_manager.py` | 新增 `_bridge` / `_package_manager` | M4, M5 |
| `pet.py` | 新增叙事事件处理 / 虚拟物件显示 | M1, M2 |
| `config.json` | 新增各模块配置段 | 全部 |
| `config.py` | 新增配置加载 | 全部 |

---

## 9. 与 Fritia_Online_NEXT 的功能映射

| Fritia 功能 | OC Pet 对应模块 | 差异说明 |
|-------------|----------------|----------|
| LLM 叙事生成 | M1 叙事引擎 | Fritia 是事件即兴，OC Pet 是微事件 + 主动触发 |
| 造梦系统 | M2 环境感知 | Fritia 自由创造家具，OC Pet 轻量版: 识别 + 虚拟物件 |
| 存档系统 | M3 记忆快照 | Fritia localStorage JSON，OC Pet 文件打包 .pet |
| 多角色互动 | M4 多宠协作 | Fritia 暖调闲聚地图，OC Pet 事件总线广播 |
| 角色卡导入 | M5 角色包 | Fritia 人格数据+知识库，OC Pet 统一 .pet 格式 |

---

## 附录 A: 叙事事件类型定义

```python
NARRATIVE_EVENT_TYPES = {
    "micro_event": {
        "description": "桌面小事件",
        "examples": [
            "看到你写了三个小时代码了，眼睛还好吗？",
            "窗外下雨了，适合窝在椅子上发呆。",
        ]
    },
    "observation": {
        "description": "对环境的观察",
        "examples": [
            "Steam 打开了...今天又要肝游戏？",
            "你在看视频笑什么，分享一下嘛。",
        ]
    },
    "care": {
        "description": "关怀提醒",
        "examples": [
            "喝水了吗？你已经两个小时没离开了。",
            "该站起来活动一下啦～",
        ]
    },
    "joke": {
        "description": "轻松幽默",
        "examples": [
            "你知道程序员最怕什么吗？...空指针。喵。",
        ]
    },
    "question": {
        "description": "主动提问",
        "examples": [
            "你今天过得怎么样？",
            "有没有什么想和我分享的？",
        ]
    },
    "greeting": {
        "description": "打招呼/回应",
        "examples": [
            "你回来了！等你好久了～",
            "早上好！今天也要加油哦。",
        ]
    },
}
```

## 附录 B: 虚拟物件库

| Key | Emoji | Label | Duration | Trigger |
|-----|-------|-------|----------|---------|
| coffee | ☕ | 一杯热咖啡 | 15s | 用户长时间工作 |
| snack | 🍕 | 零食时间！ | 10s | 接近饭点 |
| book | 📖 | 在看书呢 | 20s | 检测到阅读应用 |
| star | ⭐ | 你真棒！ | 8s | 用户完成 TODO |
| idea | 💡 | 想到好主意了 | 12s | 叙事事件 |
| game | 🎮 | 打游戏去！ | 10s | 检测到游戏应用 |
| sleepy | 😴 | 困了... | 15s | 深夜 + 用户活跃 |
| writing | ✏️ | 在写东西 | 20s | 检测到写作应用 |
| rain | 🌧️ | 下雨了 | 30s | 时间感知 + 天气 API |
| music | 🎵 | 在听音乐？ | 15s | 检测到音乐应用 |
