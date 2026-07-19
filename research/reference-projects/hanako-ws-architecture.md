# Hanako WebSocket 客户端架构设计

> **作者**：洛琪希
> **审查**：奥菲莉娅
> **日期**：2026-07-19
> **版本**：v0.407.15 源码核对版

---

## 协议纠正（v0.407.15 源码实测）

| 项 | 题目背景描述 | 源码实际行为 |
|---|---|---|
| WS 地址 | `ws://localhost:14500?ticket=...` | `ws://localhost:14500/ws?ticket=...` |
| 客户端发消息 | `session_user_message` | `prompt` |
| 助手回复 | `assistant_message` | `text_delta` + `thinking_*` + `tool_*` + `turn_end` |
| 创建 Session | 未确认 | `POST /api/sessions/new` |

正确发送帧：

```json
{
  "type": "prompt",
  "text": "用户消息",
  "clientMessageId": "ocpet_xxx",
  "sessionId": "<stable-session-uuid>",
  "sessionPath": "<current-session-jsonl-path>"
}
```

`session_user_message` 是服务端对用户消息的回显（ack），不能作为客户端发送命令。

---

## 架构总览

```
PetManager / main.py
  │
  ├── shared HanakoWSClient
  │     ├── POST /api/ws-ticket
  │     ├── /ws?ticket=...
  │     ├── ping/pong + reconnect
  │     ├── resume_stream
  │     └── 原始事件分发（订阅机制）
  │
  ├── shared HanakoSessionManager
  │     ├── REST Session API（list/create/history）
  │     ├── TurnAccumulator（text_delta 聚合）
  │     ├── 工具进度 + content_block
  │     └── ReplyResult（Future 模式）
  │
  ├── HanakoMonitor（复用共享 WS）
  │     └── 只负责状态/动画，不聚合正文
  │
  └── PetWindow → ConversationEngine → HanakoPetAdapter
        ├── chat_via_hanako()  ← 主路径
        └── chat_direct()      ← fallback
```

**单进程单 WS**。多个桌宠、SessionManager 和 HanakoMonitor 共用连接。

---

## HanakoWSClient 接口

```python
class ConnectionState(str, Enum):
    STOPPED = "stopped"
    CONNECTING = "connecting"
    READY = "ready"
    BACKOFF = "backoff"
    CLOSING = "closing"

@dataclass(frozen=True)
class StreamCursor:
    session_id: str | None
    session_path: str
    stream_id: str | None
    last_seq: int = 0

class HanakoWSClient:
    def __init__(self, base_url, token, *, connect_timeout=10.0,
                 ping_interval=20.0, ping_timeout=10.0, reconnect=None): ...

    @property
    def state(self) -> ConnectionState: ...
    @property
    def is_ready(self) -> bool: ...

    def start(self) -> None: ...
    def stop(self, timeout=5.0) -> None: ...
    def wait_until_ready(self, timeout=None) -> bool: ...

    def subscribe(self, callback, *, event_types=None,
                  session_id=None, session_path=None) -> Subscription: ...
    def subscribe_state(self, callback) -> Subscription: ...

    def send_json(self, payload) -> None: ...
    def send_prompt(self, *, session_id, session_path, text,
                    client_message_id, display_message=None,
                    ui_context=None) -> None: ...
    def resume_stream(self, cursor) -> None: ...
    def abort_stream(self, cursor, reason="user_abort") -> None: ...
```

线程模型：

```
ConversationEngine 线程 → send_json()（send_lock 保护）
WS IO 线程 → ticket → 建连 → 接收 → event_queue
事件派发线程 → event_queue.get() → 去重 → 订阅回调
```

依赖：`websocket-client>=1.8.0`（同步，适配现有 threading + Qt Signal 架构）

---

## HanakoSessionManager 接口

```python
@dataclass(frozen=True)
class SessionRef:
    session_id: str
    session_path: str
    agent_id: str | None = None
    title: str | None = None

@dataclass(frozen=True)
class ReplyResult:
    session: SessionRef
    text: str
    thinking: str
    tool_calls: tuple[dict, ...]
    content_blocks: tuple[dict, ...]
    client_message_id: str | None
    stream_id: str | None
    origin: str  # "oc_pet" / "external"
    aborted: bool = False
    error: str | None = None

@dataclass(frozen=True)
class ToolProgress:
    session: SessionRef
    tool_call_id: str | None
    tool_name: str
    phase: str  # "start" / "progress" / "end"
    display_text: str
    success: bool | None = None

class HanakoSessionManager:
    def health(self) -> dict: ...
    def list_sessions(self, agent_id=None) -> list[SessionSummary]: ...
    def create_session(self, *, agent_id=None, cwd=None, ...) -> SessionRef: ...
    def get_history(self, session, *, limit=50, before=None) -> HistoryPage: ...
    def ensure_session(self, *, agent_id, preferred_session_id=None,
                       create_if_missing=True) -> SessionRef: ...
    def send_message(self, session, text, *, display_text=None,
                     ui_context=None) -> Future[ReplyResult]: ...
    def send_and_wait(self, session, text, *, timeout=180.0) -> ReplyResult: ...
    def abort(self, session, reason="user_abort") -> bool: ...
    def resolve_confirmation(self, confirm_id, *, confirmed, value=None) -> bool: ...
    def on_progress(self, callback) -> Callable: ...
    def on_tool(self, callback) -> Callable: ...
    def on_reply(self, callback) -> Callable: ...
```

同一 Session 只允许一轮主请求进行中。多 PetWindow 可用不同 Session 并发。

---

## 回复聚合规则

```
CREATED → SENT → ACKED → STREAMING → COMPLETED / FAILED / ABORTED
```

| 事件 | 动作 |
|---|---|
| `session_user_message`（回显） | 按 `clientMessageId` 确认自己的请求 |
| `status(isStreaming=true)` | 记录 streamId，进入 STREAMING |
| `thinking_*` | 聚合 thinking，发"正在思考"进度 |
| `text_delta` | 按 seq 去重后追加正文 |
| `tool_start` | 发工具状态 |
| `tool_end` | 更新工具成功/失败 |
| `content_block` | 交付文件、图片、确认卡片 |
| `deferred_result` | 处理异步生图、子 Agent |
| `turn_end` | 原子完成 Future，只交付一次 |
| `error` | 绑定对应 Session 的 pending turn |

传输级去重键：`(sessionId, streamId, seq)`

---

## 断线恢复

客户端记录每个 Session 的 `sessionId, sessionPath, streamId, lastSeq`。

重连后发送：

```json
{
  "type": "resume_stream",
  "sessionId": "<uuid>",
  "sessionPath": "<path>",
  "streamId": "<stream-id>",
  "sinceSeq": 37
}
```

服务器返回 `stream_resume` 事件，包含缺失事件列表。恢复事件合并外层 Session 字段后重新进入普通处理函数。

`turn_end` 后流缓存已清空时，改用 REST 历史补拉。已交给 `send()` 的 prompt 不自动重发（避免重复执行工具）。

---

## HarnessAdapter 改造

保留原签名，新增 `chat_via_hanako()`：

```python
class HanakoPetAdapter:
    def chat(self, message, inject_memory=True, extra_context="", tools=None) -> tuple:
        if self.transport_mode == "direct":
            return self.chat_direct(...)
        try:
            return self.chat_via_hanako(...)
        except HanakoUnavailableBeforeSend:
            if self.transport_mode == "prefer_hanako":
                return self.chat_direct(...)
            raise

    def chat_via_hanako(self, message, inject_memory=True,
                        extra_context="", tools=None, timeout=180.0) -> tuple: ...
    def chat_direct(self, ...) -> tuple:  # 当前 chat() 的完整实现
    @staticmethod
    def parse_emotion(text: str) -> tuple[str, str]:  # 沿用 [emotion:xxx]
```

Fallback 边界：

| 状态 | 允许直连 fallback |
|---|---|
| ticket 获取失败 | ✅ |
| WS 尚未 READY | ✅ |
| prompt 尚未交给 socket | ✅ |
| send() 已返回但未回显 | ❌ |
| 已收到回显/status/tool/text | ❌ |
| Hanako 已开始工作但超时 | ❌ |

---

## 工具状态展示

```python
TOOL_ACTIVITY = {
    "web_search": "正在搜索…",
    "web_fetch": "正在读取网页…",
    "browser": "正在浏览…",
    "media_generate-image": "正在生成图片…",
    "read": "正在读取文件…",
    "write": "正在编辑…",
    "edit": "正在编辑…",
    "exec_command": "正在执行命令…",
}
```

未知工具显示"正在使用 <tool_name>…"。不在气泡中显示完整工具参数。

---

## 与 HanakoMonitor 的关系

复用共享 WS 连接，不新建：

```python
ws_client.subscribe_state(lambda state, err: monitor.set_ws_connected(...))
ws_client.subscribe(monitor.push_event, event_types={"thinking_start", "tool_start", ...})
```

- SessionManager 聚合正文和最终结果
- Monitor 只显示状态/动画/连接情况
- `pet.py._do_engine_reply()` 仍是正文唯一入口
- 文件轮询仅在 WS 稳定断开后启用

---

## 与群友 session:send 方案的对齐

```
插件做分析
  → ctx.bus.request("session:send", ...)
  → Hanako promptSession
  → Session 事件总线
  ├── Hanako 主对话框
  └── oc-pet 共享 WS
```

完全兼容。oc-pet 收到不属于自己 `clientMessageId` 的用户回显时，建立 `origin="external"` 的 accumulator，后续仍走 `text_delta → turn_end` 聚合。

---

## 文件改造清单

| 文件 | 改动 | 行数估计 |
|---|---|---:|
| `core/hanako_ws_client.py` | **新增** — 连接、ticket、重连、订阅、resume | 300–420 |
| `core/hanako_session_manager.py` | **新增** — REST、Session、聚合、工具、确认 | 420–600 |
| `core/harness_adapter.py` | 改造 — Hanako 路径、直连拆分、统一 emotion | 100–170 |
| `core/conversation_engine.py` | 改造 — 注入 manager、工具路径切换、进度 | 70–130 |
| `core/hanako_monitor.py` | 改造 — 共享订阅、tool_end、fallback | 40–80 |
| `pet.py` | 改造 — Qt Signal、进度续期、新对话入口 | 50–100 |
| `pet_manager.py` / `main.py` | 改造 — 共享客户端生命周期 | 30–70 |
| `env_config.py` | 改造 — Hanako URL、token、mode、timeout | 30–50 |
| `.env.example` | 新增配置占位 | 8–12 |
| `requirements.txt` | 新增 `websocket-client` | 1 |
| 测试 | WS、Session、Adapter、断线场景 | 750–1,100 |

核心实现约 1,000–1,500 行。

---

## 推荐配置

```env
HANAKO_BASE_URL=http://127.0.0.1:14500
HANAKO_API_TOKEN=
HANAKO_TRANSPORT_MODE=prefer_hanako
HANAKO_REPLY_TIMEOUT=180
HANAKO_MIRROR_EXTERNAL_REPLIES=true
```

---

## 风险清单

- 运行版本未必与 v0.407.15 安装包完全一致，实施前做真实 WS 冒烟
- Session 的 sessionId 和 sessionPath 不匹配会被服务器拒绝
- 同一 Session 并发 prompt 会触发 session_busy
- Hanako 模式下继续本地执行 tool call 会造成双执行
- 生图/视频/子 Agent 可能通过 deferred_result 延迟返回
- 高风险工具需要确认，不能由桌宠自动批准
- 当前 pet.py 的 30 秒超时不适合长任务
- 共享 WS 生命周期归 PetManager，不能由单个 PetWindow 控制
- Bearer token 和 ticket 不得写入日志或 Git

---

## 最小验收标准

- [ ] 桌宠输入出现在 Hanako Session 历史中
- [ ] Hanako 主窗口能打开同一 Session
- [ ] 搜索/生图/RSS/浏览器由 Hanako 工具系统执行
- [ ] 桌宠显示工具过程，最终回复只出现一次
- [ ] 可以创建新 Session
- [ ] 断线能恢复或补拉历史，不重复发送 prompt
- [ ] 直连 LLM 只在消息明确尚未发送时 fallback
- [ ] 插件 session:send、Hanako 主窗口和桌宠能观察同一流
