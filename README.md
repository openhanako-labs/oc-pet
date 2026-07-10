# OC Desktop Pet

基于 PySide6 的 AI 桌面伴侣，深度集成 Hanako 生态。

## 功能

- 💬 **对话** - 复用 Hanako 同一套身份文件和记忆系统，不维护重复配置
- 🔊 **语音输出** - CosyVoice2 零样本克隆，情绪影响语气
- 🎤 **语音输入** - Whisper ASR，push-to-talk
- 👁️ **屏幕感知** - 定时截屏 + 视觉模型分析，注入对话上下文
- 🧠 **统一感知** - 时间/情绪状态机/日程/屏幕/主动对话，一个模块管理
- 🗣️ **主动对话** - 规则引擎：空闲时长 + 前台窗口分类 → 自动搭话
- 🔌 **插件面板** - 浏览 Hanako 全部插件 + 快捷发送指令
- ⚙️ **配置面板** - GUI 设置 TTS / 行为 / 主动对话 / 屏幕感知
- 🎨 **pet.json 规范** - 外部创作者用一张精灵图 + 一个 JSON 即可制作角色

## 快速开始

### 1. 安装依赖

```bash
pip install PySide6 PySide6-Addons requests Pillow pyyaml
pip install sounddevice openai-whisper  # 语音输入（可选）
```

### 2. 确保 Hanako 已安装

桌宠从 Hanako 读取以下文件：
- `~/.hanako/agents/<角色>/identity.md` - 角色身份
- `~/.hanako/agents/<角色>/ishiki.md` - 意识/规则
- `~/.hanako/agents/<角色>/config.yaml` - 模型配置
- `~/.hanako/agents/<角色>/memory/` - 记忆文件（today.md / facts.md / memory.md）
- `~/.hanako/provider-catalog.json` - API 地址和密钥

不需要单独配置 API，自动复用 Hanako 的。

### 3. 启动

```bash
python main.py
```

或双击 `start_pet.bat`。

首次启动时后台加载 CosyVoice 模型（约 30 秒），期间桌宠显示"正在准备声音..."。

## 制作角色

### 方式一：pet.json + 精灵图（推荐）

```
characters/my-pet/
├── pet.json          # 角色配置
└── spritesheet.png   # 精灵图（所有帧拼在一张图里）
```

`pet.json` 格式：

```json
{
  "id": "my-pet",
  "name": "我的桌宠",
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
    "angry":     { "start": 10, "count": 2 },
    "surprised": { "start": 12, "count": 2 },
    "thinking":  { "start": 14, "count": 2 }
  }
}
```

### 方式二：分帧 PNG 文件夹

```
characters/my-pet/
└── frames/
    ├── idle/idle_0.png, idle_1.png, ...
    ├── walk/walk_0.png, walk_1.png, ...
    └── extra/extra_0.png, extra_1.png, ...
```

两种方式自动检测，有 `pet.json` 用精灵图模式，否则回退到分帧模式。

## 架构

```
用户
  ├─ 打字/语音 ──> ConversationEngine（后台线程）
  │                   ├─ HanakoPetAdapter（LLM + 身份 + 记忆注入）
  │                   ├─ PerceptionController（时间/情绪/屏幕/日程）
  │                   └─ CosyVoiceService（TTS 零样本克隆）
  │                   ────── pyqtSignal ──────> 主线程 UI 更新
  │
  ├─ 右键菜单 ──> 设置面板 / 插件面板 / 角色切换
  └─ 屏幕截屏 ──> agnes 视觉模型 ──> 感知上下文
```

单进程架构，所有组件在一个 `python main.py` 进程内运行。

## 文件说明

| 文件 | 作用 |
|------|------|
| `main.py` | 启动入口 |
| `pet.py` | 主窗口：渲染、交互、菜单、状态管理 |
| `conversation_engine.py` | 对话引擎：LLM + TTS 后台线程 |
| `perception.py` | 统一感知系统（时间/情绪/日程/屏幕/主动对话） |
| `harness_adapter.py` | LLM 适配器：读 Hanako 配置，调 API |
| `hanako_context.py` | 读取 Hanako 身份/模型/记忆 |
| `tts_bridge.py` | CosyVoice 常驻 TTS 服务 |
| `tts_player.py` | QMediaPlayer 音频播放 |
| `voice_input.py` | Whisper ASR 语音输入 |
| `foreground_watcher.py` | 前台窗口分类 |
| `action_linker.py` | 动作联动（窗口→桌宠行为） |
| `avatar/base.py` | AvatarRenderer 抽象接口 |
| `avatar/sprite_renderer.py` | 帧精灵渲染器（支持 pet.json） |
| `settings_dialog.py` | 配置面板 GUI |
| `plugin_panel.py` | 插件浏览面板 |
| `bubble.py` | 对话气泡 UI |
| `eye_overlay.py` | 瞳孔跟踪 |
| `startup_screen.py` | 启动画面 |
| `character_editor.py` | 角色编辑器 |
| `behavior.py` | 行为模式参数 |
| `config.py` / `config.json` | 配置管理 |
| `paths.py` | 路径常量 |
| `pet.schema.json` | pet.json 规范参考 |

## 技术栈

| 组件 | 技术 |
|------|------|
| GUI | PySide6 (Qt6) |
| LLM | agnes-2.0-flash (OpenAI 兼容 API) |
| TTS | CosyVoice2 (零样本克隆) |
| ASR | OpenAI Whisper (base) |
| 视觉 | agnes-2.0-flash vision |
| 跨线程通信 | Qt Signal/Slot |

## 设计原则

- **不维护重复状态** - 记忆、身份、API 配置全部从 Hanako 读取
- **单进程** - 对话引擎在后台线程，UI 在主线程，信号槽通信
- **可扩展角色** - pet.json 规范让外部创作者无需改代码即可添加角色
- **感知统一** - 时间/情绪/屏幕/日程/主动对话一个模块管理
