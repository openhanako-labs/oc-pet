# OC Desktop Pet

基于 PySide6 的透明桌面宠物，支持本地 LLM 对话与 Hanako Agent 双向通信。

## 功能

- 🎭 **帧精灵动画**：idle / walk / extra 三种动画序列，按情绪自动切换
- 💬 **对话气泡**：白底圆角 + 阴影 + 淡入动画，情绪高潮时变暖色
- 🔗 **Hanako 通信**：与 Hanako Agent 双向消息桥接，桌宠直接给 Agent 发消息
- 😊 **情绪检测**：根据 Agent 回复自动检测情绪（开心/悲伤/生气/惊讶），切换对应动画
- 👻 **右键菜单**：切换角色、显示/隐藏桌宠、设置 API、退出
- 🧠 **对话记忆**：本地 JSONL 持久化 + ChromaDB 向量检索（语义记忆）
- ⚡ **事件驱动**：WebSocket 实时通信替代文件轮询

## 快速开始

### 1. 安装依赖

```bash
pip install PySide6 requests Pillow websockets
```

### 2. 配置 API

启动后 **右键 → 设置**，填入 API 地址和 Key。不接 Hanako 也能用本地对话。

### 3. 启动

```bash
python main.py
```

或双击 `start.bat`。

## Hanako 集成

桌宠通过 WebSocket + 文件混合模式与 Hanako Agent 双向通信。

### 安装插件

将 `hanako-desktop-companion/` 目录安装为 Hanako 插件：

1. Hanako 设置 → 插件 → 安装本地插件 → 选择本目录下的 `hanako-desktop-companion/`
2. 重启 Hanako

### 通信架构（v2.0 事件驱动版）

```
桌宠 (pet.py)               WebSocket Server        Hanako Agent
    │                           │                        │
    │◄═══════════ WS 推送 ═══════►│                        │
    │  text_delta/               │                        │
    │  tool_start/               │                        │
    │  response                  │                        │
    │                           │                        │
    │─ WS 发送 outbox ──────────►│                        │
    │  {type: "outbox"}           │                        │
    │                           │                        │
    │  (WS 断开时回退文件)        │                        │
    │◄─── 写 outbox.json ────────┼───────────────────────►│  companion_outbox 读取
    │                           │                        │ Agent 回复
    │◄───── 写 response.json ────┼───────────────────────►│  companion_send 写入
    │                           │                        │
    │ hanako_monitor 轮询        │                        │
    │ (TODO + 通知)              │                        │
```

### 实时事件推送

WS Server 向桌宠推送的事件类型：

| 事件类型 | 用途 | 情绪映射 |
|---------|------|---------|
| `thinking_start` | Agent 开始思考 | 思考中 |
| `thinking_delta` | Agent 思考更新 | 思考中 |
| `text_delta` | Agent 文字输出 | 回复中 |
| `mood_text` | 情绪化文本 | 回复中 |
| `tool_start` | 执行工具 | 执行中/编辑中/浏览中 |
| `tool_end` | 工具完成 | 成功→开心 / 失败→生气 |
| `vision_progress` | 视觉处理中 | 观察中 |
| `file_write_prepare` | 文件写入中 | 编辑中 |
| `turn_end` | 回合结束 | 待机中 |

### 文件说明

| 文件 | 作用 |
|------|------|
| `main.py` | 启动入口 |
| `pet.py` | 主窗口：动画、气泡、拖拽、右键菜单、Hanako 状态回调、记忆写入 |
| `config.py` | 配置：角色信息、情绪映射、Hanako 状态映射 |
| `hanako_monitor.py` | Hanako 监控：轮询 TODO/通知、WS 事件驱动情绪映射 |
| `ws_server.py` | **WebSocket 服务器**（端口 19900） |
| `ws_client.py` | **WebSocket 客户端**（桌宠 → WS Server） |
| `memory_store.py` | **对话记忆存储**（JSONL + ChromaDB 向量） |
| `harness_adapter.py` | LLM 适配器：读取角色设定，调用 Chat API |
| `hanako-desktop-companion/` | Hanako 插件源码（消息桥接工具 + HTTP API） |

## 对话记忆系统

### 架构

```
对话结束 ──▶ MemoryStore.add()
               │
               ├──▶ memory.jsonl（源真理，追加写，不可变）
               │
               └──▶ ChromaDB 向量索引（语义检索，首次启用时下载 ~79MB ONNX 模型）
```

### 记忆条目

```json
{
  "user_msg": "你最喜欢的食物是什么？",
  "bot_reply": "炸牛排！夜之城最好的炸牛排",
  "summary": "你最喜欢的食物是什么？",
  "timestamp": "2026-05-29T...",
  "emotion": "happy",
  "confidence": 0.7,
  "source": "dialogue"
}
```

### 搜索方式

- **关键词搜索** `search(keyword)`：匹配 user_msg + summary（本地，即时）
- **语义搜索** `search_semantic(query)`：匹配向量嵌入（需要 ChromaDB 模型）
- **格式化输出** `format_recent(n)` / `format_semantic(query)`：生成可注入 prompt 的文本

### 自动写入

Agent 回复完成后，`_on_hanako_state` 回调自动将对话写入记忆（JSONL 层即时写入，ChromaDB 异步同步）。

## 目录结构

```
oc-pet/
├── main.py                 # 启动入口
├── pet.py                  # 主窗口（动画/气泡/拖拽/菜单/记忆写入）
├── config.py               # 配置（角色/情绪/状态映射）
├── hanako_monitor.py       # Hanako 监控（轮询 + WS 事件驱动）
├── ws_server.py            # WebSocket 服务器（端口 19900）
├── ws_client.py            # WebSocket 客户端
├── memory_store.py         # 对话记忆存储（JSONL + ChromaDB）
├── harness_adapter.py      # LLM 适配器
├── api.py                  # 本地对话 API
├── config.json             # 用户配置
├── start.bat               # 启动脚本（自动启动 WS 服务器）
├── hanako-desktop-companion/  # Hanako 插件
│   ├── manifest.json
│   ├── tools/
│   │   ├── companion_outbox.js
│   │   └── companion_send.js
│   └── routes/
│       └── api.js
├── characters/
│   ├── yuexiye/frames/     # 帧精灵
│   └── ophelia/frames/     # 帧精灵
└── skills/public/          # 角色设定
```

## 操作

| 操作 | 效果 |
|------|------|
| 左键点击角色 | 打开输入框，发送本地对话 |
| 输入文字按回车 | 发送消息 |
| 拖拽空白区域 | 移动窗口 |
| 右键 | 切换角色 / 隐藏桌宠 / 设置 / 退出 |

## 自定义

- 换角色图：替换 `characters/角色名/frames/` 下的 PNG
- 改角色设定：编辑 `skills/public/角色名/SKILL.md`
- 调动画速度：修改 `pet.py` 里的 `_frame_timer` 间隔

## 开发日志

### v2.0（2026-05-29）

- **WebSocket 实时通信**：新建 `ws_server.py`，`ws_client.py` 新增 WS 发送，`hanako_monitor.py` 改为事件驱动模式
- **对话记忆系统**：`memory_store.py` 重写，JSONL 持久化 + ChromaDB 向量检索，`pet.py` 集成记忆写入
- **ChromaDB 模型下载**：受网络限制（~20 KiB/s），首次启动时自动下载 all-MiniLM-L6-v2 ONNX 模型（~79MB），下载完成后自动启用语义搜索
- **Graceful degradation**：ChromaDB 不可用时静默降级到关键词搜索
