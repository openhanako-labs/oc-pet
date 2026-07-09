# OC Desktop Pet

基于 PySide6 的 AI 桌面伴侣，深度集成 Hanako 生态。

## 功能

### 核心能力

- 💬 **对话** - 读取 Hanako 同一套身份文件（identity.md / ishiki.md），使用同一模型（agnes-2.0-flash）
- 🔊 **语音输出** - CosyVoice2 零样本克隆，每个角色有独立音色，情绪影响语气
- 🎤 **语音输入** - Whisper ASR，右键"说话"按钮，push-to-talk
- 👁️ **实时视觉** - 每 2 分钟截屏，agnes 视觉模型分析你在做什么，注入对话上下文
- 🧠 **感知系统** - 时间感知 + 情绪状态机（连续衰减）+ 屏幕感知
- 🗣️ **主动对话** - 规则引擎：空闲时长 + 前台窗口分类 -> 自动搭话
- 📦 **记忆压缩** - 50 条对话自动压缩，prompt 不膨胀
- 🔌 **插件面板** - 浏览 Hanako 全部插件 + 快捷发送指令
- ⚙️ **配置面板** - GUI 设置 TTS / 行为 / 主动对话 / 屏幕感知

### 交互

- 帧精灵动画（idle / walk / extra），瞳孔跟踪鼠标
- 情绪帧区间映射（happy/angry/surprised/thinking）
- 4 种行为模式（静默/正常/活跃/黏人）
- 久坐提醒（三段递进）
- 前台窗口检测（写作/开发/浏览/游戏/通讯/娱乐）
- 右键菜单：对话/说话/行为/缩放/角色/穿透/设置/插件
- 系统托盘

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
- `~/.hanako/provider-catalog.json` - API 地址和密钥

不需要单独配置 API，自动复用 Hanako 的。

### 3. 启动

```bash
python main.py
```

或双击 `start_pet.bat`。

首次启动时后台加载 CosyVoice 模型（约 23 秒），期间桌宠显示"正在准备声音..."。

## 架构

```
用户
  ├─ 打字 ──────────────> ConversationEngine
  ├─ 右键"说话" ──> Whisper ──> ConversationEngine
  │                              │
  │                    ┌─────────┴─────────┐
  │                    │  LLM (agnes)      │
  │                    │  + Hanako 身份    │
  │                    │  + 时间/屏幕感知   │
  │                    │  + 记忆注入       │
  │                    └─────────┬─────────┘
  │                              │
  │                    ┌─────────┴─────────┐
  │                    │  CosyVoice TTS   │
  │                    │  (零样本克隆)      │
  │                    └─────────┬─────────┘
  │                              │
  └─ 桌宠窗口 <──────── 回调：气泡 + 语音
```

单进程架构，所有组件在一个 `python main.py` 进程内运行。

## 文件说明

| 文件 | 作用 |
|------|------|
| `main.py` | 启动入口 |
| `pet.py` | 主窗口：渲染、交互、菜单、状态管理 |
| `conversation_engine.py` | 对话引擎：LLM + TTS 后台线程 |
| `harness_adapter.py` | LLM 适配器：读 Hanako 配置，调 API |
| `hanako_context.py` | Hanako 上下文读取器：身份/模型/记忆 |
| `tts_bridge.py` | CosyVoice 常驻 TTS 服务 |
| `tts_player.py` | QMediaPlayer 音频播放 |
| `voice_input.py` | Whisper ASR 语音输入 |
| `screen_watcher.py` | 屏幕截屏 + 视觉模型分析 |
| `perception.py` | 感知系统：时间/情绪/日程 |
| `proactive_scheduler.py` | 主动对话规则引擎 |
| `memory_store.py` | 对话记忆（JSONL + 压缩） |
| `memory_compressor.py` | 记忆压缩引擎 |
| `avatar/base.py` | AvatarRenderer 抽象接口 |
| `avatar/sprite_renderer.py` | 帧精灵渲染器 |
| `settings_dialog.py` | 配置面板 GUI |
| `plugin_panel.py` | 插件浏览面板 |
| `paths.py` | 路径常量集中管理 |
| `config.py` | 配置管理 + 情绪映射 |
| `config.json` | 用户配置（不提交，在 .gitignore） |
| `data/` | 运行时数据（outbox/response，不提交） |
| `characters/` | 角色帧精灵资源（不提交） |

## 自定义

| 操作 | 方法 |
|------|------|
| 换角色图 | 替换 `characters/<角色>/frames/` 下的 PNG |
| 改行为模式 | 右键 -> 行为 -> 选择模式 |
| 调 TTS 音量 | 右键 -> 设置 -> 语音输出 |
| 调主动对话规则 | 编辑 `config.json` 的 `proactive.rules` |
| 加 TTS 参考音频 | 编辑 CosyVoice 的 `speaker_refs.json` |
| 换模型 | 在 Hanako 设置里改，桌宠自动跟随 |

## 技术栈

| 组件 | 技术 |
|------|------|
| GUI | PySide6 (Qt6) |
| LLM | agnes-2.0-flash (OpenAI 兼容 API) |
| TTS | CosyVoice2 (零样本克隆) |
| ASR | OpenAI Whisper (base) |
| 视觉 | agnes-2.0-flash vision (image input) |
| 记忆 | JSONL + 压缩引擎 |
| 截屏 | Pillow ImageGrab |

## 开发日志

### v3.0（2026-07-08）

- Hanako 原生集成：读取同一套身份/模型/API 配置
- 单进程架构：ConversationEngine 合并 bridge + pet
- 语音输出：CosyVoice2 常驻服务，零样本克隆
- 语音输入：Whisper ASR，push-to-talk
- 实时视觉：截屏 + agnes 视觉模型分析
- 感知系统：时间感知 + 情绪状态机
- LLM 情绪检测：回复带 [emotion:xxx] 标签
- 配置面板 + 插件面板
- Avatar 渲染层抽象（预留 Live2D/VRM）
- 清理：移除 ws_server/ws_client/hanako-desktop-companion

### v1.0（2026-07-07）

- TTS 可中断管线
- 情绪帧区间映射
- Proactive 主动对话
- 记忆压缩引擎
- NEKO 架构整合方案
