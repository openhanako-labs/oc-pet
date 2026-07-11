# OC Desktop Pet

基于 PySide6 的 AI 桌面伴侣，深度集成 Hanako 生态。支持多桌宠并行运行，每个 Hanako agent 可独立拥有一个桌宠窗口。

## 功能

### 核心
- 💬 **对话** - 复用 Hanako 身份/记忆/模型配置，不维护重复状态
- 🗣️ **多 TTS 引擎** - CosyVoice2 本地克隆 / MIMO TTS / OpenAI 兼容 API，设置面板一键切换
- 🎤 **多 ASR 引擎** - Whisper 本地 / MIMO ASR / OpenAI 兼容 API
- 👁️ **屏幕感知** - 定时截屏 + 视觉模型分析，注入对话上下文
- 🧠 **统一感知** - 时间/情绪状态机/日程/屏幕/主动对话，一个模块管理
- 🗣️ **主动对话** - 规则引擎：空闲时长 + 前台窗口分类 → 自动搭话

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

## 快速开始

### 1. 安装依赖

```bash
pip install PySide6 PySide6-Addons requests Pillow pyyaml
pip install sounddevice openai-whisper  # 语音输入（可选）
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

首次运行自动添加 ophelia 为默认桌宠。后续可在设置面板添加更多 agent。

## 架构

```
PetManager（多桌宠管理器）
  ├─ PetWindow[ophelia] ──── ConversationEngine ──── HanakoPetAdapter (LLM)
  │   ├─ SpriteRenderer          ├─ TTS Provider        ├─ hanako_context (身份+记忆)
  │   ├─ MouseTracker            │   ├─ CosyVoice       └─ perception (感知上下文)
  │   ├─ PhysicsEngine           │   ├─ MIMO TTS
  │   └─ ChatBubble              │   └─ API TTS
  │                              └─ ASR Provider
  ├─ PetWindow[glados]           │   ├─ Whisper
  │   └─ ...                     │   ├─ MIMO ASR
  └─ ...                         │   └─ API ASR
                                 └────── pyqtSignal ──────> 主线程 UI
```

单进程架构，每个 PetWindow 独立运行，共享 QApplication。

## 设置面板

| Tab | 设置项 |
|-----|--------|
| **基础** | Agent 管理（添加/移除/启用/禁用）、行为模式、透明度、缩放、鼠标交互开关 |
| **功能** | TTS（引擎/音量）、主动对话、屏幕感知、久坐提醒、语音输入、记忆注入 |
| **API** | LLM/TTS/ASR 各有 Provider 下拉（自动填充 URL/Key）+ Model 下拉（从 catalog 读取）+ 音色选择 |

## TTS 引擎

| 引擎 | 说明 | 依赖 |
|------|------|------|
| **CosyVoice2** | 本地零样本克隆，GPU 加速 | cosyvoice-tts 项目 |
| **MIMO TTS** | 小米 MiMo V2.5，/v1/chat/completions 格式 | API Key |
| **API TTS** | OpenAI 兼容 /audio/speech | API Key |

MIMO TTS 音色：`mimo_default` / `冰糖` / `茉莉` / `苏打` / `白桦` / `Mia` / `Chloe` / `Milo` / `Dean`

## 制作角色

### 精灵目录结构

```
~/.hanako/agents/<agent>/pet/    # 优先读取（自定义）
├── pet.json                      # 可选，spritesheet 模式
├── spritesheet.png
└── frames/                       # 或分帧模式
    ├── idle/idle_0.png, ...
    ├── walk/walk_0.png, ...
    └── extra/extra_0.png, ...
```

回退到项目内置 `characters/<agent>/` 目录。

### pet.json 格式

```json
{
  "spritesheet": {
    "src": "spritesheet.png",
    "frameWidth": 331,
    "frameHeight": 568,
    "scale": 0.5
  },
  "animations": {
    "idle":  { "start": 0, "count": 4, "fps": 3 },
    "walk":  { "start": 4, "count": 4, "fps": 4 },
    "extra": { "start": 8, "count": 8, "fps": 3 }
  },
  "emotions": {
    "happy":     { "start": 8,  "count": 2 },
    "surprised": { "start": 12, "count": 2 }
  }
}
```

## 文件说明

| 文件 | 作用 |
|------|------|
| `main.py` | 启动入口（PetManager） |
| `pet_manager.py` | 多桌宠管理器：agent 发现、窗口管理 |
| `pet.py` | 主窗口：渲染、交互、菜单、状态管理 |
| `mouse_tracker.py` | 鼠标状态追踪 + 事件发射 |
| `conversation_engine.py` | 对话引擎：LLM + TTS 后台线程 |
| `perception.py` | 统一感知（时间/情绪/日程/屏幕/主动对话） |
| `harness_adapter.py` | LLM 适配器：读 Hanako 配置，调 API |
| `hanako_context.py` | 读取 Hanako 身份/模型/记忆（含动态记忆预算） |
| `behavior.py` | 行为模式参数 + 鼠标反应参数 |
| `physics.py` | 物理引擎：行走惯性 / 弹跳 / 运动状态机 |
| `avatar/sprite_renderer.py` | 帧精灵渲染器 + 视线跟随（精灵偏移） |
| `tts_provider/cosyvoice.py` | CosyVoice2 本地 TTS |
| `tts_provider/mimo_tts.py` | MIMO TTS（/v1/chat/completions 格式） |
| `tts_provider/api_tts.py` | OpenAI 兼容 API TTS |
| `asr_provider/whisper_local.py` | Whisper 本地 ASR |
| `asr_provider/mimo_asr.py` | MIMO ASR（/v1/chat/completions 格式） |
| `asr_provider/api_asr.py` | OpenAI 兼容 API ASR |
| `settings_dialog.py` | 设置面板（三 tab） |
| `plugin_panel.py` | 插件浏览面板 |
| `bubble.py` | 对话气泡 UI |
| `tts_player.py` | QMediaPlayer 音频播放 |
| `voice_input.py` | Whisper ASR 语音输入 |
| `foreground_watcher.py` | 前台窗口分类 |
| `action_linker.py` | 动作联动 |
| `startup_screen.py` | 启动画面 |
| `config.py` / `config.json` | 配置管理 |

## 技术栈

| 组件 | 技术 |
|------|------|
| GUI | PySide6 (Qt6) |
| LLM | agnes-2.0-flash / 任意 OpenAI 兼容 API |
| TTS | CosyVoice2 / MIMO TTS / OpenAI TTS |
| ASR | Whisper / MIMO ASR / OpenAI ASR |
| 视觉 | agnes-2.0-flash vision |
| 跨线程通信 | Qt Signal/Slot |

## 设计原则

- **不维护重复状态** - 记忆、身份、API 配置全部从 Hanako 读取
- **单进程多窗口** - 每个 agent 独立 PetWindow，共享 QApplication
- **可扩展角色** - pet.json 规范让外部创作者无需改代码即可添加角色
- **感知统一** - 时间/情绪/屏幕/日程/主动对话一个模块管理
- **优雅降级** - TTS/ASR/视觉任一组件缺失不影响核心对话功能

## 路线图

### 近期
- [ ] 窗口吸附 + "坐下"动画（拖到窗口边缘自动坐下）
- [ ] 情绪帧扩展（补 surprised/thinking 精灵帧，修复映射越界）
- [ ] Avatar 抽象层完善（pet.py 不再直接操作 char_label）

### 中期
- [ ] 搜索+总结能力（接入文件搜索 / 网页搜索工具）
- [ ] 日程管理扩展（SchedulePerception → 完整日程面板）
- [ ] 信息监控（屏幕感知 + 关键词匹配 → 主动通知）
- [ ] 角色编辑器接入设置面板

### 远期
- [ ] 流式 TTS（降低首字延迟）
- [ ] Live2D / VRM 骨骼动画支持
- [ ] 跨设备同步（多台电脑共享桌宠状态）
