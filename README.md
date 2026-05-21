# OC Desktop Pet

基于 PySide6 的透明桌面宠物，支持本地 LLM 对话与 Hanako Agent 双向通信。

## 功能

- 🎭 **帧精灵动画**：idle / walk / extra 三种动画序列，按情绪自动切换
- 💬 **对话气泡**：白底圆角 + 阴影 + 淡入动画，情绪高潮时变暖色
- 🔗 **Hanako 通信**：与 Hanako Agent 双向消息桥接，桌宠直接给 Agent 发消息
- 😊 **情绪检测**：根据 Agent 回复自动检测情绪（开心/悲伤/生气/惊讶），切换对应动画
- 👻 **右键菜单**：切换角色、显示/隐藏桌宠、设置 API、退出

## 快速开始

### 1. 安装依赖

```bash
pip install PySide6 requests Pillow
```

### 2. 配置 API

启动后 **右键 → 设置**，填入 API 地址和 Key。不接 Hanako 也能用本地对话。

### 3. 启动

```bash
python main.py
```

或双击 `启动桌宠.bat`。

## Hanako 集成（可选）

桌宠可以通过 `hanako-desktop-companion` 插件与 Hanako Agent 双向通信。

### 安装插件

将 `hanako-desktop-companion/` 目录安装为 Hanako 插件：

1. Hanako 设置 → 插件 → 安装本地插件 → 选择本目录下的 `hanako-desktop-companion/`
2. 重启 Hanako

### 通信流程

```
桌宠 (pet.py)                  Hanako Agent
    │                                │
    ├─ 写 outbox.json ──────────────→│ companion_outbox 读取
    │                                │ Agent 回复
    │←─ companion_send 写 response ──┤
    │                                │
hanako_monitor 轮询读取 ←────────────┘
```

### 文件说明

| 文件 | 作用 |
|------|------|
| `main.py` | 启动入口 |
| `pet.py` | 主窗口：动画、气泡、拖拽、右键菜单、Hanako 状态回调 |
| `config.py` | 配置：角色信息、情绪映射、Hanako 状态映射 |
| `hanako_monitor.py` | Hanako 监控：轮询 response.json、情绪检测 |
| `harness_adapter.py` | LLM 适配器：读取角色设定，调用 Chat API |
| `hanako-desktop-companion/` | Hanako 插件源码（消息桥接工具 + HTTP API） |

## 目录结构

```
oc-pet/
├── main.py
├── pet.py
├── config.py
├── hanako_monitor.py
├── harness_adapter.py
├── api.py
├── config.json
├── 启动桌宠.bat
├── hanako-desktop-companion/   # Hanako 插件（可独立安装）
│   ├── manifest.json
│   ├── tools/
│   │   ├── companion_outbox.js
│   │   └── companion_send.js
│   └── routes/
│       └── api.js
├── characters/
│   ├── yuexiye/frames/         # 帧精灵
│   └── ophelia/frames/         # 帧精灵
└── skills/public/              # 角色设定
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