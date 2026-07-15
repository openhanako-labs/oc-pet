# OC Desktop Pet


![License: AGPL v3](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)


基于 PySide6 的 AI 桌面伴侣，深度集成 Hanako 生态。支持多桌宠并行运行，每个 Hanako agent 可独立拥有一个桌宠窗口。

## 功能

### 核心
- 💬 **对话** - 复用 Hanako 身份/记忆/模型配置，不维护重复状态
- 🗣️ **多 TTS 引擎** - CosyVoice2 本地克隆 / MIMO TTS / OpenAI 兼容 API，设置面板一键切换
- 🎤 **多 ASR 引擎** - Whisper 本地 / MIMO ASR / OpenAI 兼容 API
- 👁️ **屏幕感知** - 定时截屏 + 视觉模型分析，注入对话上下文
- 🧠 **统一感知** - 时间/情绪状态机/日程/屏幕/主动对话，一个模块管理
- 🗣️ **主动对话** - 规则引擎：对话空闲时长 + 前台窗口分类 → 自动搭话
- 🔌 **插件工具调用** - 自动扫描 Hanako 插件，LLM tool calling 执行插件工具

### 交互
- 🖱️ **鼠标交互** - 视线跟随（精灵偏移）+ 靠近反应 + 悬停 + 追逐 + 惊吓
- 🔌 **插件面板** - 浏览 Hanako 全部插件 + 快捷发送指令
- ⚙️ **设置面板** - 三 tab 布局：基础 / 功能 / API，全覆盖

### 多桌宠
- 🏠 **多窗口并行** - 每个 Hanako agent 独立运行一个桌宠
- 🔍 **Agent 发现** - 自动扫描 `~/.hanako/agents/`，设置面板添加/移除/启用/禁用
- 🎨 **精灵来源** - `~/.hanako/agents/<agent>/pet/`（自定义）> `characters/<agent>/`（内置回退）

### 记忆
- 📊 **动态记忆预算** - 自动读取模型 context 字段，按 1% 计算记忆上限（agnes 1M → 6000 字符）
- 🔧 **可配置** - 自动模式或手动指定字符上限

## 环境要求

- **Python**: 3.10+
- **操作系统**: Windows 10/11
- **Hanako**: 已安装并配置（桌宠读取 `~/.hanako/` 下的配置和角色数据）
- **Node.js**: 可选，插件工具调用需要

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

或手动安装：
```bash
pip install PySide6>=6.5.0 PySide6-Addons>=6.5.0 requests>=2.28.0 Pillow>=9.0.0 PyYAML>=6.0 sounddevice>=0.4.6 numpy>=1.24.0 scipy>=1.10.0
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

首次运行自动添加 **月薪喵 (yuexinmiao)** 为默认桌宠。后续可在设置面板添加更多 agent。

## 架构

```
PetManager（多桌宠管理器）
  ├─ PetWindow[yuexinmiao] ── ConversationEngine ── HanakoPetAdapter (LLM)
  │    ├─ SpriteRenderer (精灵渲染)
  │    ├─ MouseTracker (鼠标交互)
  │    ├─ PerceptionController (感知)
  │    │    ├─ ScreenWatcher (屏幕感知)
  │    │    ├─ ProactiveScheduler (主动对话)
  │    │    └─ EmotionStateMachine (情绪)
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
├── pet.py                  # 单桌宠窗口
├── config.py               # 配置管理
├── env_config.py           # .env 配置
├── core/                   # 核心模块
│   ├── conversation_engine.py  # 对话引擎
│   ├── harness_adapter.py      # LLM 适配器
│   ├── perception.py           # 感知控制器
│   ├── tool_registry.py        # 工具注册表
│   ├── tool_executor.py        # 工具执行器
│   └── hanako_context.py       # 上下文构建
├── ui/                     # UI 模块
│   ├── settings_dialog.py      # 设置面板
│   └── plugin_panel.py         # 插件面板
├── avatar/                 # 精灵渲染
│   └── sprite_renderer.py
├── motion/                 # 运动系统
│   ├── physics.py              # 物理引擎
│   ├── behavior.py             # 行为状态机
│   └── action_linker.py        # 动作链接
├── tts_provider/           # TTS 引擎
├── asr_provider/           # ASR 引擎
├── characters/             # 内置角色
│   └── yuexinmiao/             # 月薪喵（默认）
├── docs/                   # 文档
└── requirements.txt        # 依赖列表
```

## 配置

### .env 文件

```env
# LLM
LLM_PROVIDER=deepseek
LLM_BASE_URL=https://api.deepseek.com
LLM_API_KEY=sk-xxx
LLM_MODEL=deepseek-chat

# TTS (可选)
TTS_PROVIDER=api
TTS_BASE_URL=https://api.example.com
TTS_API_KEY=sk-xxx

# ASR (可选)
ASR_PROVIDER=whisper
```

### config.json

自动生成，可在设置面板修改：
- `agents` - 桌宠列表
- `behavior` - 行为模式 (quiet/normal/active/cling)
- `memory` - 记忆配置
- `tts/asr` - 语音配置

## 常见问题

### Q: 启动后没有反应？
A: 检查 Hanako 是否安装，`~/.hanako/` 目录是否存在。

### Q: LLM 返回 400 错误？
A: 检查 API URL 是否正确。桌宠会自动补 `/v1` 前缀，但部分 API 可能需要完整路径。

### Q: TTS 不工作？
A: TTS 是可选功能，不影响文字对话。检查 TTS 配置或切换到其他引擎。

### Q: 如何添加更多桌宠？
A: 在设置面板的"Agent 管理"中添加，或在 `~/.hanako/agents/` 下创建新的 agent 目录。

## 许可证

MIT License
