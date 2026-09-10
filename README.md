# OC Desktop Pet


![License: AGPL v3](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)

## ⚠️ Live2D 模型版权说明

本项目**不随仓库分发任何 Live2D 模型文件**（`characters/*/live2d/` 已在 `.gitignore` 中排除）。

- 仓库仅内置 **miku** 占位角色配置（`pet.json` + `profile.json`），**不含任何模型素材**；`Rory` / `sample_live2d` 等完整角色包由用户本地放置模型后使用，不随仓库分发。
- Live2D 渲染器代码完整保留，但**模型需用户自行提供**——请使用有分发许可的模型（如 [Live2D 官方示例](https://www.live2d.com/en/learn/sample/)），或运行 `python tools/fetch_free_live2d_sample.py` 下载官方 Haru 示例模型。
- 请勿将无再分发许可的模型（如游戏提取模型）放入仓库。

> **占位角色（需自备模型）**：`miku` / `Rory` / `sample_live2d` 是**占位角色**——仓库仅含配置与 `profile.json`，**不含任何模型素材**。使用前请按对应角色目录放置模型文件；**缺少模型时桌宠会提示「需下载模型」且不会加载**（不会白屏或崩溃）。
>
> **无默认角色**：首次启动由引导流程选择角色包；仓库不含内置精灵图角色。


基于 PySide6 的 AI 桌面伴侣，深度集成 Hanako 生态。支持多桌宠并行运行，每个 Hanako agent 可独立拥有一个桌宠窗口。

> **💡 一句话说明：Hanako 是什么？**
> Hanako（本项目的宿主）是运行在后台的 AI 助手框架。oc-pet 桌宠**不是独立 AI**——
> 它的对话、记忆、工具调用都复用你机器上 `~/.hanako/` 里已有的 Hanako 配置。
> 换句话说：先装 Hanako 并配好模型/API，桌宠才有灵魂；没装 Hanako 时桌宠会用内置降级逻辑跑基础对话。
> 安装见下方「环境要求」。

<details>
<summary>功能清单</summary>

### 对话系统
- 💬 **文字对话** -- 复用 Hanako 身份/记忆/模型配置，支持 tool calling
- 🗣️ **TTS 语音输出** -- 四种引擎可选：
  - CosyVoice2 本地克隆（零样本克隆，需 GPU）
  - **微软 Edge TTS（免费在线，秒级，免注册免 key）**
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
- 📸 **屏幕感知** -- 定时截屏 + 视觉模型分析，注入对话上下文（独立进程，不卡 UI）
- 🎭 **屏幕情绪检测** -- 从屏幕内容推断用户情绪（如"看视频" → happy）
- 🪟 **前台窗口监听** -- 检测用户正在使用的应用，用于窗口互动和主动对话触发
- 📱 **手机活动感知** -- MacroDroid 上报前台 App 切换，自动分类（娱乐/通讯/音乐/购物/阅读/工作/游戏）并注入上下文
- 🔌 **掌心窗集成** -- 通过 linjian-peek 服务获取手机截图、生活状态（电量/网络）、远程控制（打开App/通知/闹钟）
- 🎵 **SMTC 媒体感知** -- 读取正在播放的媒体信息（歌曲名、艺术家、专辑）

### 叙事引擎
- 📝 **微事件生成** -- 空闲时自动生成小事件（观察/关心/笑话/提问/问候）
- 🤖 **感知走 AI 发挥** -- 有 LLM 时走生成，无 LLM 才降级模板（不念预置台词）
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
- 📊 **Token/费用统计** -- 按会话/按天统计，可配预算上限
- 🔄 **记忆自动维护** -- 空闲时自动归纳/去重/修剪/重要性衰减
- ⚡ **异步写入** -- 批量 flush + 异步落盘，不阻塞对话

### 多桌宠
- 🏠 **多窗口并行** -- 每个 Hanako agent 独立运行一个桌宠
- 🔍 **Agent 发现** -- 自动扫描 `~/.hanako/agents/`
- 🎨 **角色包管理** -- 自定义精灵 + 内置回退
- 🎛️ **per-pet 独立配置** -- 每个桌宠可单独绑定自己的 TTS 引擎/音色与对话助手
  （设置面板 → 基础 →「桌宠独立配置」；不配置则沿用全局默认）

### 系统管理
- 🏥 **子服务健康四态** -- 服务状态可视化（enabled/running/ready/last_error）
- 🎛️ **主动能力面板** -- 统一展示开关 + 运行状态 + 费用边界
- 🔒 **隐私暂停** -- 一键停掉所有隐私敏感能力
- 💾 **备份恢复** -- 完整备份 + SHA-256 校验 + 一键恢复
- 🎮 **插件 KV 存储** -- 插件自带配置页 + 持久存储

### 通知
- 📱 **ntfy 通知** -- 推送通知到手机（需安装 ntfy app）

</details>

## 环境要求

- **Python**: 3.10+
- **操作系统**: Windows 10/11
- **Hanako**: 已安装并配置（桌宠读取 `~/.hanako/` 下的配置和角色数据）
  - Hanako 项目：<https://github.com/liliMozi/openhanako>
  - 安装后运行**至少一次**（生成 `~/.hanako/agents/` 与 `provider-catalog.json`）
  - 不装 Hanako 也能启动桌宠（走本地降级对话），但会缺失身份/记忆/多助手等核心能力

## 快速开始

> 首次拉取仓库如果因网络中断报 `early EOF`，重试一次即可（可加 `git config http.postBuffer 524288000` 增大缓冲）。

### 1. 创建虚拟环境并安装依赖

```bash
# 推荐：创建 venv，避免污染系统 Python
python -m venv .venv
.venv\Scripts\activate

pip install -r requirements.txt
```

> 不用 venv 直接 `pip install -r requirements.txt` 也可以跑，但会装进全局环境。
> 强烈建议用 venv——本项目依赖较多（PySide6 等 170MB+），可随时删除重建。

**跑起来你会看到什么**（首次启动）：
- 🖱️ 眼睛跟着鼠标转（视线跟随）
- 👁️ 自动眨眼（每隔几秒）
- 😴 5 分钟没操作 → 犯困打哈欠
- 😵 15 分钟没操作 → 打瞌睡倒头
- 💬 10 分钟空闲 → 桌宠主动找你说话
- 🖱️ 左键拖动桌宠，释放后弹跳
- 📌 拖到屏幕边缘 → 桌宠坐下

### 2. 确保 Hanako 已安装

桌宠从 Hanako 读取：
- `~/.hanako/agents/<agent>/` - 身份、意识、记忆、模型配置
- `~/.hanako/provider-catalog.json` - API 地址、密钥、模型列表

不需要单独配置 API，自动复用 Hanako 的。

### 3. 下载 Live2D 模型（首次）

桌宠不随仓库分发 Live2D 模型文件。需要自备或下载官方示例模型：

```bash
# 方式 A：下载官方 Haru 示例模型（推荐新手）
python tools/fetch_free_live2d_sample.py haru

# 方式 B：按角色目录 README 下载
# 见 characters/miku/README.md（或任意角色目录的 README.md）
```

**模型硬要求**（三条）：
- ✅ **裸文件**：解压后能看到 `.model3.json` + `.moc3` + 贴图
- ✅ **没加密**：模型文件可直接导入 VTube Studio
- ✅ **Cubism 3+**：支持 Cubism 3 或 4 的模型

> 💡 **判断标准**：能导入 VTube Studio 的模型就能用。下单前问卖家："是否提供 model3.json 素材文件？"

### 4. 启动

```bash
python main.py
```

或双击 `start_pet.bat`。

首次启动由引导流程选择角色包（默认无内置模型，需自行提供）。以 **miku** 占位角色为例：
把官方/有许可的 Live2D 模型放入 `characters/miku/live2d/`，再把 config.json 的 `character` 改为 `miku` 即切换为 Live2D 桌宠；其他角色同理。

## 本地 CosyVoice TTS 部署（从零）

桌宠默认用**本地 CosyVoice2** 配音（无需联网/付费）。它跑在独立子进程里，
不卡 UI；合成延迟约 **8–10 秒/句**（GPU + fp16）。下面是从零让另一台机器
也能用本地 TTS 的步骤。

> 前置：Windows + **NVIDIA 显卡**（本地 TTS 需要 CUDA）；Python 3.10~3.12。
> 没有独显的机器会自动给出告警，可在「设置 → TTS」改用 MIMO / 在线 TTS。

### 一键引导

```bash
# 1) 把 cosyvoice-tts 仓库放到 oc-pet 的同级目录（两仓库并排即零配置），或：
setup_tts.bat --cosyvoice-repo https://your.git/cosyvoice-tts.git

# 2) 引导脚本会：建 venv → 装 CUDA 版 torch + 依赖 → 获取 cosyvoice-tts
#    → 下载 CosyVoice2-0.5B 模型（约 4.6GB，需联网）→ 写 .env
```

引导完成后直接 `start_pet.bat` 即可。如需手动分步，见下方。

### 手动分步

1. **获取代码**：把 `cosyvoice-tts`（含 `src/` 与 `models/`）放到 oc-pet 同级目录，
   或在 `.env` 设置 `OC_PET_COSYVOICE_DIR` 指向它。
2. **装环境**（关键：torch 必须带 CUDA，否则本地 TTS 会跑 CPU 慢速）：
   ```bash
   python -m venv .venv_cosy
   .venv_cosy\Scripts\python -m pip install torch torchaudio `
       --index-url https://download.pytorch.org/whl/cu124
   .venv_cosy\Scripts\python -m pip install -r requirements_cosyvoice.txt
   ```
3. **下载模型**（约 4.6GB）：
   ```bash
   .venv_cosy\Scripts\python scripts/download_cosyvoice_model.py
   ```
4. 在 `.env` 写入 `OC_PET_COSYVOICE_DIR` 与 `OC_PET_COSYVOICE_PYTHON`
   （引导脚本会自动写；手动装则需自己加）。

### 配置项（.env）

| 变量 | 说明 | 默认 |
|---|---|---|
| `OC_PET_COSYVOICE_DIR` | cosyvoice-tts 目录 | 相邻 `../cosyvoice-tts` → 兜底硬编码 |
| `OC_PET_COSYVOICE_PYTHON` | 运行 worker 的解释器（需含 torch+onnxruntime-gpu+cudnn） | 自动探测 |
| `OC_PET_COSYVOICE_MODEL` | 模型名 | `CosyVoice2-0.5B` |

解析顺序：`OC_PET_COSYVOICE_DIR` → `config.json` 的 `tts.cosyvoice_dir`
→ 与 oc-pet 相邻的 `../cosyvoice-tts` → 内置兜底路径。

### 常见问题

- **合成一句要 1–2 分钟？** 说明跑在 CPU 上（无 CUDA / cudnn 没生效）。
  确认显卡驱动正常、torch 是 CUDA 版、且 `onnxruntime-gpu` + `nvidia-cudnn-cu12` 已装。
- **没独显？** 本地 TTS 会优雅降级为不可用并告警，改用 MIMO / 在线 TTS 即可。

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
    "provider": "cosyvoice",      // TTS 引擎: cosyvoice(本地) / edge(微软免费) / mimo(在线) / api
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
  ├─ PetWindow[<character>] ── ConversationEngine ── HanakoPetAdapter (LLM)
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
│   ├── miku/                 # Miku（占位，模型自备）
│   ├── Rory/                # Rory（本地角色包，模型自备，不入库）
│   └── sample_live2d/        # 免费示例模型（Haru 等）
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

## 致谢与参考来源

本项目在架构设计与技术方案上参考了以下开源项目：

- **[Code-Amadeus / Amadeus](https://github.com/Code-Amadeus/Amadeus)** — 实时多模态桌面 Agent。oc-pet 的 Live2D 渲染架构、HUD 设计语言、情感驱动参数体系均以此为参照。
- **[Soullink Emotion SDK](https://github.com/nanlingyin/soullink-emotion-sdk)** — TypeScript / MIT。Embody 层（情绪→参数映射抽象）的设计思想来源，未集成 SDK 本体，以 Python 独立重写。
- **[Live2D CubismWebSamples](https://github.com/Live2D/CubismWebSamples)** — Live2D 官方免费示例模型（Haru / Hiyori 等），可直接下载使用。
- **[Hanako](https://github.com/liliMozi/openhanako)** — AI 助手框架。桌宠的对话、记忆、工具调用、多助手协作均复用 Hanako 配置。
- **[N.E.K.O.](https://github.com/Project-N-E-K.O/N.E.K.O)** — Apache 2.0。主动对话决策管线、情绪状态机设计语言等参考来源。

感谢以上项目的开源贡献。

## 许可

本项目采用**双重许可**：

- **开源许可**：[GNU AGPL v3](https://www.gnu.org/licenses/agpl-3.0.html) — 开源免费，但修改必须开源
- **商业许可**：闭源使用需购买商业授权，详见 [COMMERCIAL-LICENSE.md](./COMMERCIAL-LICENSE.md)