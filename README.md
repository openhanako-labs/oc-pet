# OC Desktop Pet


![License: AGPL v3](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)


基于 PySide6 的 AI 桌面伴侣，深度集成 Hanako 生态。支持多桌宠并行运行，每个 Hanako agent 可独立拥有一个桌宠窗口。

## 功能清单

### 对话系统
- 💬 **文字对话** -- 复用 Hanako 身份/记忆/模型配置，支持 tool calling
- 🗣️ **TTS 语音输出** -- 三种引擎可选：
  - CosyVoice2 本地克隆（零样本克隆，需 GPU）
  - MIMO TTS（小米 MiMo V2.5，音色可选）
  - OpenAI 兼容 API
- 🎤 **ASR 语音输入** -- 三种引擎可选：
  - Whisper 本地（离线识别）
  - MIMO ASR（小米 MiMo V2.5）
  - OpenAI 兼容 API
- 🔌 **插件工具调用** -- 自动扫描 Hanako 插件，LLM tool calling 执行插件工具

### 感知系统
- ⏰ **时间感知** -- 区分早晨/中午/下午/晚上/深夜/凌晨，影响对话风格
- 😊 **情绪状态机** -- happy/sad/thinking/surprised/neutral 五种情绪，自动衰减
- 📸 **屏幕感知** -- 定时截屏 + 视觉模型分析，注入对话上下文
- 🎭 **屏幕情绪检测** -- 从屏幕内容推断用户情绪（如"看视频" → happy）
- 🪟 **前台窗口监听** -- 检测用户正在使用的应用，用于窗口互动和主动对话触发
- 📱 **手机活动感知** -- MacroDroid 上报前台 App 切换，自动分类（娱乐/通讯/音乐/购物/阅读/工作/游戏）并注入上下文
- 🔌 **掌心窗集成** -- 通过 linjian-peek 服务获取手机截图、生活状态（电量/网络）、远程控制（打开App/通知/闹钟）

### 叙事引擎
- 📝 **微事件生成** -- 空闲时自动生成小事件（观察/关心/笑话/提问/问候）
- 📦 **本地模板兜底** -- LLM 不可用时用预设模板（无需断网，只需 API 不可用）
- 🔄 **情境缓存 + 冷却控制** -- 避免重复内容，冷却 600 秒（可配置）

### 交互功能
- 🖱️ **鼠标交互** -- 视线跟随 + 靠近反应 + 悬停 + 追逐 + 惊吓
- 🖐️ **拖拽** -- 左键拖动桌宠，释放后弹跳
- 📌 **边缘吸附** -- 拖到屏幕边缘坐下
- 🪟 **窗口互动** -- 检测前台窗口，桌宠自动走过去（冷却时间可配置）
- 💬 **右键菜单** -- 穿透/设置/插件/退出
- ⌨️ **聊天框** -- 左键点击切换聊天输入

### 主动对话
- 🤖 **规则引擎** -- 对话空闲时长 + 前台窗口分类 → 自动搭话
- 📊 **屏幕内容触发** -- 根据屏幕分析结果主动搭话（检测到视频/游戏/代码等关键词）
- ⏱️ **冷却控制** -- 屏幕内容触发 5 分钟冷却，避免频繁打扰

### Hanako 联动
- 🔗 **状态监控** -- 实时读取 Hanako 状态（TODO/通知/对话回复）
- 💬 **对话同步** -- Hanako 有新回复时，桌宠显示气泡 + 播放 TTS
- 📋 **工作状态** -- 检测到 Hanako 有 TODO 时，桌宠显示"工作中"状态
- 🔔 **通知转发** -- Hanako 通知显示为桌宠消息气泡
- 🌐 **多桌宠协作** -- 多个桌宠之间可以互相"聊天"/反应/关心/送礼物
- 📁 **记忆读取** -- 读取 Hanako 的置顶记忆和最近对话记录

### 手机感知（双通道）

桌宠通过两条独立通道感知手机状态，统一注入 LLM 上下文：

**通道 1：MacroDroid 直连（常态感知）**
- 📱 **前台 App 上报** -- MacroDroid 规则检测应用切换，HTTP POST 到桌宠本地接收器
- 🏷️ **自动分类** -- 7 类应用（娱乐/通讯/音乐/购物/阅读/工作/游戏）+ 情绪映射
- 📊 **活动摘要** -- "最近1小时使用了 小红书(3次)、微信(2次)"
- ⏱️ **空闲检测** -- 距上次手机活动的分钟数
- 🔒 **隐私优先** -- 数据不出本机，标准库 HTTP server，零外部依赖

**通道 2：掌心窗集成（按需增强）**
- 📸 **手机截图** -- 通过 linjian-peek 服务请求手机截图并返回
- 🔋 **生活状态** -- 电量、充电、网络、当前 App、屏幕时间、解锁次数
- 🎮 **远程控制** -- 打开应用、返回桌面、发送通知、设置闹钟
- 🔌 **MCP 工具** -- 通过 Hanako 插件系统注册，LLM tool calling 触发

**数据流：**
```
MacroDroid → HTTP POST → PhoneActivityReceiver → PhoneActivityPerception ─┐
                                                                           ├→ PerceptionController.build_context()
linjian-peek → MCP Plugin → Hanako tool calling ──────────────────────────┘
```

### 记忆系统
- 💾 **记忆快照** -- 导出/导入 Agent 记忆，支持 overwrite/smart/skip_existing 合并
- 📏 **动态记忆预算** -- 自动按模型 context 1% 计算，或手动指定字符数
- 📌 **置顶记忆** -- 读取 pinned-memory.json

### 多桌宠
- 🏠 **多窗口并行** -- 每个 Hanako agent 独立运行一个桌宠
- 🔍 **Agent 发现** -- 自动扫描 `~/.hanako/agents/`
- 🎨 **角色包管理** -- 自定义精灵 + 内置回退

### 通知
- 📱 **ntfy 通知** -- 推送通知到手机（需安装 ntfy app）

## 环境要求

- **Python**: 3.10+
- **操作系统**: Windows 10/11
- **Hanako**: 已安装并配置（桌宠读取 `~/.hanako/` 下的配置和角色数据）

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 确保 Hanako 已安装

桌宠从 Hanako 读取：
- `~/.hanako/agents/<agent>/` - 身份、意识、记忆、模型配置
- `~/.hanako/provider-catalog.json` - API 地址、密钥、模型列表

不需要单独配置 API，自动复用 Hanako 的。

### 3. 启动

```bash
python main.py
```

或双击 `start_pet.bat`。

首次运行自动添加 **月薪喵 (yuexinmiao)** 为默认桌宠。

## 配置说明

### config.json

```json
{
  "behavior": "normal",           // 行为模式: quiet/normal/active/cling
  "window_interaction": {
    "enabled": true,              // 是否启用窗口互动
    "cooldown_seconds": 30        // 窗口互动冷却时间（秒）
  },
  "memory": {
    "budget_chars": 0,            // 记忆预算字符数（0=自动）
    "budget_percent": 1.0         // 自动模式：模型 context 的百分比
  },
  "tts": {
    "enabled": true,
    "provider": "mimo",           // TTS 引擎: cosyvoice/mimo/api
    "volume": 0.8
  },
  "asr": {
    "provider": "whisper_local"   // ASR 引擎: whisper_local/mimo/api
  },
  "proactive": {
    "enabled": true,
    "cooldown_minutes": 10        // 主动对话冷却时间
  },
  "screen": {
    "enabled": true,
    "interval": 120,              // 截屏间隔（秒）
    "blur": true                  // 截图模糊（隐私保护）
  }
}
```

### .env 文件

```env
# LLM（可选，优先使用 Hanako 配置）
LLM_PROVIDER=deepseek
LLM_BASE_URL=https://api.deepseek.com
LLM_API_KEY=sk-xxx
LLM_MODEL=deepseek-chat

# TTS（可选）
TTS_PROVIDER=mimo
TTS_BASE_URL=https://token-plan-cn.xiaomimimo.com/v1
TTS_API_KEY=sk-xxx

# ASR（可选）
ASR_PROVIDER=whisper_local

# 视觉模型（可选，用于屏幕感知）
VISION_BASE_URL=https://api.siliconflow.cn
VISION_API_KEY=sk-xxx
VISION_MODEL=Qwen/Qwen2.5-VL-7B-Instruct

# ntfy 通知（可选）
NTFY_TOPIC=your-topic-name

# 手机活动感知 - MacroDroid 直连（可选）
PHONE_RECEIVER_PORT=8077
PHONE_AUTH_TOKEN=your-secret-token

# 掌心窗 - linjian-peek 集成（可选）
LINJIAN_URL=https://xxx.onrender.com
LINJIAN_TOKEN=your-linjian-token
```

### MacroDroid 配置（手机活动上报）

1. 安装 [MacroDroid](https://play.google.com/store/apps/details?id=com.arlosoft.macrodroid)（Android）
2. 创建新宏：触发器 = "应用启动/切换" → 动作 = "HTTP 请求"
3. HTTP 请求配置：
   - 方法：`POST`
   - URL：`http://<电脑IP>:8077/phone/activity`
   - Header：`X-Auth-Token: <你的token>`
   - Body：`{"app": "{app_name}", "event": "switch"}`
4. 保存并启用宏

> 💡 如果桌宠和手机在同一局域网，用电脑的内网 IP。如果需要外网访问，考虑用 ngrok 或 frp 做内网穿透。

## 测试指南

| 功能 | 测试方法 | 预期效果 |
|------|----------|----------|
| 拖拽 | 左键拖动桌宠 | 桌宠跟随鼠标移动 |
| 边缘吸附 | 拖到屏幕边缘 | 桌宠坐下 |
| 鼠标跟随 | 鼠标靠近桌宠 | 桌宠视线跟随 |
| 窗口互动 | 切换前台应用 | 桌宠走过去 |
| 屏幕感知 | 等 2 分钟 | 日志显示 `Screen analysis: ...` |
| 叙事引擎 | 等 10 分钟 | 桌宠自言自语 |
| 聊天 | 左键点击桌宠 | 弹出聊天框 |
| 设置 | 右键菜单 → 设置 | 打开设置面板 |
| 手机感知 | MacroDroid POST 到 localhost:8077 | 日志显示 `Phone activity: app=小红书 event=switch` |
| 掌心窗状态 | Hanako 对话中调用 `phone_status` | 返回服务在线状态 |

## 架构

```
PetManager（多桌宠管理器）
  ├─ PetWindow[yuexinmiao] ── ConversationEngine ── HanakoPetAdapter (LLM)
  │    ├─ SpriteRenderer (精灵渲染)
  │    ├─ MouseTracker (鼠标交互)
  │    ├─ PerceptionController (感知)
  │    │    ├─ ScreenWatcher (屏幕感知)
  │    │    ├─ PhoneActivityPerception (手机活动)
  │    │    ├─ PhoneActivityReceiver (MacroDroid HTTP)
  │    │    ├─ ProactiveScheduler (主动对话)
  │    │    └─ EmotionStateMachine (情绪)
  │    ├─ NarrativeEngine (叙事引擎)
  │    ├─ WindowInteraction (窗口互动)
  │    ├─ Bubble (对话气泡)
  │    └─ PluginPanel (插件面板)
  └─ SettingsDialog (设置)
       ├─ LLM/TTS/ASR Provider 选择
       ├─ Agent 管理
       └─ 记忆/行为/日程配置
```

## 目录结构

```
oc-pet/
├── main.py                 # 入口
├── pet_manager.py          # 多桌宠管理
├── pet.py                  # 单桌宠窗口（主逻辑）
├── config.py               # 配置管理
├── env_config.py           # .env 配置
├── core/                   # 核心模块
│   ├── conversation_engine.py  # 对话引擎
│   ├── harness_adapter.py      # LLM 适配器
│   ├── perception.py           # 感知系统（时间/情绪/屏幕/手机/主动对话）
│   ├── phone_activity.py       # 手机活动数据管理 + 感知层
│   ├── phone_receiver.py       # MacroDroid HTTP 接收器
│   ├── narrative_engine.py     # 叙事引擎
│   ├── window_interaction.py   # 窗口互动
│   ├── hanako_bridge.py        # Hanako 联动（状态读取）
│   ├── hanako_monitor.py       # Hanako 监控（TODO/通知/回复）
│   ├── multi_pet_bridge.py     # 多桌宠协作（事件通信）
│   ├── tool_registry.py        # 工具注册表
│   ├── tool_executor.py        # 工具执行器
│   ├── hanako_context.py       # 上下文构建
│   └── memory_snapshot.py      # 记忆快照
├── ui/                     # UI 模块
│   ├── settings_dialog.py      # 设置面板
│   ├── plugin_panel.py         # 插件面板
│   └── bubble.py               # 对话气泡
├── avatar/                 # 精灵渲染
│   └── sprite_renderer.py
├── motion/                 # 运动系统
│   ├── physics.py              # 物理引擎
│   ├── behavior.py             # 行为状态机
│   └── foreground_watcher.py   # 前台窗口监听
├── tts_provider/           # TTS 引擎
├── asr_provider/           # ASR 引擎
├── characters/             # 内置角色
│   └── yuexinmiao/             # 月薪喵（默认）
└── requirements.txt        # 依赖列表
```

## 常见问题

### Q: 桌宠不说话？
A: 检查 LLM API 配置。桌宠会自动使用 Hanako 的配置，如果 Hanako 没配置，需要在 `.env` 中指定。

### Q: TTS 不工作？
A: TTS 是可选功能，不影响文字对话。在设置面板切换 TTS 引擎。

### Q: 屏幕感知不触发主动对话？
A: 当前版本屏幕感知只触发情绪，不触发主动对话。主动对话由 ProactiveScheduler 根据空闲时间和前台窗口触发。

### Q: 如何添加更多桌宠？
A: 在设置面板的"角色包"中添加，或在 `~/.hanako/agents/` 下创建新的 agent 目录。

### Q: ntfy 通知怎么用？
A: 1) 手机安装 ntfy app（Android/iOS）；2) 订阅一个 topic；3) 在 `.env` 中配置 `NTFY_TOPIC=your-topic`。

## 许可

本项目采用**双重许可**：

- **开源许可**：[GNU AGPL v3](https://www.gnu.org/licenses/agpl-3.0.html) — 开源免费，但修改必须开源
- **商业许可**：闭源使用需购买商业授权，详见 [COMMERCIAL-LICENSE.md](./COMMERCIAL-LICENSE.md)