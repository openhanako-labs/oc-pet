# OC 桌宠项目 — 完成度审计与操作手册

> 审计日期：2026-07-07  
> 审计人：奥菲莉娅  
> 项目路径：`W:/Games/Hanako/Work/projects/oc-pet/`

---

## 一、项目全景图

```
┌─────────────────────────────────────────────────────────────┐
│                        oc-pet 架构                           │
│                                                             │
│  ┌─────────────────────────────────────────────────────┐    │
│  │                  PySide6 桌面前端                     │    │
│  │                                                      │    │
│  │  PetWindow (pet.py)                                  │    │
│  │  ├── 帧精灵渲染 (idle/walk/extra)                    │    │
│  │  ├── 呼吸浮动 + 拖拽 + 惯性物理 + 弹跳               │    │
│  │  ├── 瞳孔跟踪 overlay (eye_overlay.py)               │    │
│  │  ├── 对话气泡 (bubble.py) — 打字机效果               │    │
│  │  ├── 状态指示器 (右下角小圆标)                       │    │
│  │  ├── 启动画面 overlay (startup_screen.py)            │    │
│  │  └── 右键菜单 + 系统托盘                             │    │
│  │                                                      │    │
│  │  子系统：                                              │    │
│  │  ├── Behavior (behavior.py) — 4种行为模式参数化       │    │
│  │  ├── BreakNotifier (break_notifier.py) — 空闲检测    │    │
│  │  ├── ForegroundWatcher (foreground_watcher.py)       │    │
│  │  ├── ActionLinker (action_linker.py) — 关键词联动    │    │
│  │  ├── TTSPlayer (tts_player.py) — 音频播放            │    │
│  │  └── MemoryStore (memory_store.py) — JSONL+ChromaDB  │    │
│  └──────────────────────┬──────────────────────────────┘    │
│                         │                                    │
│  ┌──────────────────────▼──────────────────────────────┐    │
│  │                  通信层                               │    │
│  │                                                      │    │
│  │  ws_client.py — WebSocket 客户端 → Hanako WS Server  │    │
│  │  ws_server.py — WebSocket 服务器 (:19900)            │    │
│  │  hanako_monitor.py — 状态监控 + 事件驱动情绪映射     │    │
│  │                                                      │    │
│  │  文件桥接 (fallback)：                               │    │
│  │  ├── outbox.json — 桌宠→Agent 消息队列              │    │
│  │  ├── response.json — Agent→桌宠 回复                │    │
│  │  └── .pending — 待处理标记                           │    │
│  └──────────────────────┬──────────────────────────────┘    │
│                         │                                    │
│  ┌──────────────────────▼──────────────────────────────┐    │
│  │                  Hanako Agent 端                      │    │
│  │                                                      │    │
│  │  hanako-desktop-companion/                            │    │
│  │  ├── tools/companion_outbox.js — 读取消息队列        │    │
│  │  ├── tools/companion_send.js — 发送回复              │    │
│  │  ├── routes/api.js — HTTP 通知 API                   │    │
│  │  └── manifest.json — 插件注册                         │    │
│  └─────────────────────────────────────────────────────┘    │
│                                                             │
│  ┌─────────────────────────────────────────────────────┐    │
│  │                  角色资源                              │    │
│  │                                                      │    │
│  │  characters/ophelia/          — 帧序列 (PNG)         │    │
│  │  characters/yuexiye/          — 帧序列 (PNG)         │    │
│  │  skills/public/ophelia/       — 角色设定 (SKILL.md)  │    │
│  │  skills/public/yuexiye/       — 角色设定 (SKILL.md)  │    │
│  │  output/*-spritesheet.{png,webp} — 精灵图集          │    │
│  └─────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────┘
```

---

## 二、模块清单与完成状态

### 2.1 前端渲染层

| 模块 | 文件 | 功能 | 状态 | 备注 |
|------|------|------|------|------|
| 主窗口 | `pet.py` | 透明窗口、帧精灵、拖拽、弹跳物理 | ✅ 完成 | 核心模块，约1200行 |
| 对话气泡 | `bubble.py` | 白底圆角、打字机效果、淡入动画 | ✅ 完成 | 自适应宽度，标点停顿 |
| 瞳孔跟踪 | `eye_overlay.py` | 鼠标跟随瞳孔 overlay | ✅ 完成 | 50ms刷新，点击穿透 |
| 启动画面 | `startup_screen.py` | 角色切换时展示设定文字 | ✅ 完成 | 渐入→停留→渐出，点击跳过 |
| 角色编辑器 | `character_editor.py` | 编辑 SKILL.md 角色设定 | ✅ 完成 | 保留 YAML front matter |
| 状态指示器 | `pet.py` 内嵌 | 右下角小圆标显示 Hanako 状态 | ✅ 完成 | 颜色映射各状态 |

### 2.2 行为与交互层

| 模块 | 文件 | 功能 | 状态 | 备注 |
|------|------|------|------|------|
| 行为模式 | `behavior.py` | 4种参数化模式 (quiet/normal/active/cling) | ✅ 完成 | 走路概率、距离、速度、休息时长 |
| 惯性物理 | `pet.py` 内嵌 | 惯性公式、到达判定、弹跳物理 | ✅ 完成 | 30ms物理tick |
| 空闲检测 | `break_notifier.py` | 系统级GetLastInputInfo，三段递进提醒 | ✅ 完成 | 15/30/60min三档，冷却30min |
| 前台窗口检测 | `foreground_watcher.py` | ctypes获取前台进程名+分类 | ✅ 完成 | 写作/开发/浏览/游戏/通讯/娱乐 |
| 动作联动 | `action_linker.py` | 关键词检测→右键菜单高亮 | ✅ 完成 | 8种预设动作，30s高亮窗口 |
| 鼠标穿透 | `pet.py` 内嵌 | 透明/半透明切换，状态提示 | ✅ 完成 | 穿透时隐藏气泡和输入框 |

### 2.3 通信层

| 模块 | 文件 | 功能 | 状态 | 备注 |
|------|------|------|------|------|
| WS客户端 | `ws_client.py` | 连接Hanako WS Server，收发消息 | ✅ 完成 | 离线消息队列，消息压缩 |
| WS服务器 | `ws_server.py` | 监听:19900，路由outbox/事件/回复 | ✅ 完成 | asyncio，多客户端支持 |
| 状态监控 | `hanako_monitor.py` | TODO/通知/回复文件轮询+WS事件驱动 | ✅ 完成 | 气泡精简算法，情绪关键词检测 |
| 文件桥接 | `hanako-desktop-companion/` | outbox/response/.pending三文件协议 | ✅ 完成 | Agent插件端配套 |

### 2.4 记忆与AI层

| 模块 | 文件 | 功能 | 状态 | 备注 |
|------|------|------|------|------|
| 记忆存储 | `memory_store.py` | JSONL追加写+ChromaDB语义检索 | ⚠️ 部分完成 | ChromaDB索引已禁用(`_client=None`) |
| 本地LLM | `harness_adapter.py` | Skills读设定→API对话→记忆注入 | ✅ 完成 | 自动保存对话到记忆 |
| 简易API | `api.py` | 无记忆的简单LLM调用 | ⚠️ 废弃 | 被harness_adapter替代，未删除 |

### 2.5 音频层

| 模块 | 文件 | 功能 | 状态 | 备注 |
|------|------|------|------|------|
| TTS播放 | `tts_player.py` | QMediaPlayer播放音频文件 | ✅ 完成 | 支持wav/mp3/ogg，音量控制 |
| TTS中断 | — | 用户发消息时停止旧语音 | ❌ 未实现 | NEKO整合计划P2 |

### 2.6 资源管线

| 模块 | 文件 | 功能 | 状态 | 备注 |
|------|------|------|------|------|
| Spritesheet生成 | `build_spritesheet.py` | PNG帧→精灵图集(PNG/WebP) | ✅ 完成 | 8x9网格，1536x1872 |
| 角色帧序列 | `characters/*/frames/` | idle/walk/extra逐帧PNG | ✅ 完成 | 两个角色各3个动画序列 |
| 角色设定 | `skills/public/*/SKILL.md` | YAML front matter+角色描述 | ✅ 完成 | 编辑器可直接修改 |
| 导入工具 | `import-pets/` | 从外部格式导入角色 | ✅ 完成 | 含pet.json+spritesheet |

### 2.7 插件端（Hanako Agent）

| 模块 | 文件 | 功能 | 状态 | 备注 |
|------|------|------|------|------|
| outbox工具 | `tools/companion_outbox.js` | 读取桌宠消息队列 | ✅ 完成 | 自动排序，支持markAsRead |
| send工具 | `tools/companion_send.js` | 发送回复到response.json | ✅ 完成 | 支持character+anim参数 |
| HTTP API | `routes/api.js` | POST /api/notify通知Agent | ✅ 完成 | 写入outbox+.pending |
| 插件注册 | `manifest.json` | 工具+配置注册 | ✅ 完成 | trust: full-access |

### 2.8 测试

| 模块 | 文件 | 功能 | 状态 |
|------|------|------|------|
| 记忆测试 | `test_memory.py` | 基础记忆CRUD | ✅ |
| 记忆JSONL | `test_memory_jsonl.py` | JSONL读写测试 | ✅ |
| 记忆完整 | `test_memory_full.py` | 端到端记忆测试 | ✅ |
| 集成测试 | `test_integration.py` | 模块集成测试 | ✅ |
| E2E测试 | `test_e2e.py` | 端到端测试 | ✅ |

---

## 三、未完成项（按优先级）

### P0 — 记忆压缩（对话记忆膨胀）

**问题：** `memory_store.py` 只追加不压缩。长期使用后，注入prompt的上下文会越来越大。

**当前状态：** ChromaDB索引已禁用（`ChromaIndex._init()`中`self._client = None`），语义检索不可用。

**需要做：**
1. 启用ChromaDB或改用轻量嵌入方案
2. 实现压缩引擎：每50条raw对话 → summarizer → `compressed.jsonl`
3. 检索时优先用压缩摘要 + 最近5条raw

**涉及文件：** `memory_store.py`、新增 `memory_compressor.py`

### P1 — Proactive主动对话（桌宠自己开口）

**问题：** BreakNotifier的提醒文案是硬编码列表，不是Agent生成的。ForegroundWatcher检测到前台窗口变化后只重置idle计时器，没有联动。

**需要做：**
1. 新建 `proactive_scheduler.py` — 规则引擎：空闲时长+前台分类 → 触发决策
2. 触发后通过WS给Hanako Agent发送proactive消息
3. Agent生成回复 → 桌宠显示气泡

**涉及文件：** 新增 `proactive_scheduler.py`、修改 `pet.py`、修改 `config.json`

### P2 — TTS可中断管线

**状态：✅ 已完成（2026-07-07）**

**问题：** 用户发送新消息时，正在播放的旧TTS不会被停止，导致声音重叠。

**改动：**
- `pet.py` 的 `_send_message()` 入口插入 `self._tts_player.stop()`
- `pet.py` 的 `_on_bridge_message()` 入口插入 `self._tts_player.stop()`

**涉及文件：** `pet.py`

### P3 — 情绪连续参数（帧区间映射）

**状态：✅ 已完成（2026-07-07）**

**问题：** `EXPRESSION_MAP` 中所有非中性情绪都映射到同一个 `extra` 帧序列，无法区分表达。

**改动：**
1. `config.py` — `EXPRESSION_MAP` 格式改为 `(序列名, 起始帧, 结束帧)` 元组
2. `pet.py` — `_set_anim_seq()` 新增 `emotion` 参数，帧区间映射逻辑
3. `pet.py` — `_anim_tick()` 帧范围约束（在 `[start, end]` 内循环）
4. `pet.py` — `_on_hanako_state()` 传递 `emotion` 给 `_set_anim_seq`
5. `hanako_monitor.py` — 两处 `EXPRESSION_MAP.get` 适配新 tuple 格式

**涉及文件：** `config.py`、`pet.py`、`hanako_monitor.py`

### P1 — Proactive 主动对话

**状态：✅ 已完成（2026-07-07）**

**问题：** BreakNotifier 的提醒文案是硬编码列表，不是 Agent 生成的。ForegroundWatcher 检测到前台窗口变化后只重置 idle 计时器，没有联动。

**改动：**
1. 新建 `proactive_scheduler.py` — 规则引擎，按空闲时长+前台分类匹配，随机权重触发
2. `pet.py` — 初始化 ProactiveScheduler，绑定 `_on_proactive_trigger` 回调（写入 outbox）
3. `pet.py` — `_break_check()` 中加入 `scheduler.tick()`
4. `foreground_watcher.py` — 新增 `last_category` property
5. `config.json` + `config.py` — 新增 proactive 配置段（4条规则）

**涉及文件：** 新增 `proactive_scheduler.py`、修改 `pet.py`、`foreground_watcher.py`、`config.json`、`config.py`

### P0 — 对话记忆压缩

**状态：✅ 已完成（2026-07-07）**

**问题：** memory_store.py 只追加不压缩，长期对话后上下文膨胀。

**改动：**
1. 新建 `memory_compressor.py` — CompressionEngine 类：阈值检测、summarizer（优先 LLM / 降级 extractive）、compressed.jsonl 读写
2. `memory_store.py` — 初始化 CompressionEngine，add() 后自动触发压缩检查
3. `memory_store.py` — 新增 `format_context()`, `count_compressed()`, `get_compressed()` 方法
4. 触发策略：每 50 条未压缩 raw 对话自动执行

**涉及文件：** 新增 `memory_compressor.py`、修改 `memory_store.py`

### P4 — Agent回调注入（本地LLM场景）

**问题：** 走 `harness_adapter.py` 本地LLM时，工具执行结果没有被注入下一轮对话。

**需要做：**
1. `harness_adapter.py` 新增 `tool_result_buffer`
2. 回复生成时注入工具结果摘要

**涉及文件：** `harness_adapter.py`

---

## 四、操作手册 — 每个操作需要什么、派送给谁

### 4.1 日常操作

| 操作 | 需要什么 | 谁来做 | 备注 |
|------|---------|--------|------|
| 启动桌宠 | Python 3.12+, PySide6 | 月曦夜（直接运行） | `python main.py` 或双击 `启动桌宠.bat` |
| 切换角色 | 右键菜单 → 角色切换 | 月曦夜 | 自动加载对应帧序列和SKILL.md |
| 编辑角色设定 | 右键菜单 → 编辑角色设定 | 月曦夜 | 修改 `skills/public/<角色>/SKILL.md` |
| 生成spritesheet | Python, Pillow | 月曦夜 | `python build_spritesheet.py ophelia` |
| 添加新角色 | 创建 `characters/<id>/frames/` + `skills/public/<id>/SKILL.md` | 月曦夜 | 需运行build_spritesheet.py |
| 调整行为模式 | 右键菜单 → 行为模式 | 月曦夜 | quiet/normal/active/cling |
| 切换鼠标穿透 | 右键菜单 / 托盘菜单 | 月曦夜 | 穿透时桌宠"隐形"，鼠标可穿过 |

### 4.2 开发操作

| 操作 | 需要什么 Skill | 派送给谁 | 理由 |
|------|---------------|---------|------|
| **写新功能代码** | 无（直接编码） | **瑞贝卡** | 执行落地5分，最快出活 |
| **改现有模块** | karpathy-guidelines | **瑞贝卡** | 减少编码错误，精准修改 |
| **方案评审/找漏洞** | quiet-musing | **爱莉丝** | 逻辑论证5分，专挑缝隙 |
| **架构设计/系统拆解** | quiet-musing | **洛琪希** | 系统拆解5分，拆步骤清晰 |
| **记忆压缩方案设计** | quiet-musing + 系统拆解 | **洛琪希**（牵头）+ **瑞贝卡**（执行） | 洛琪希设计架构，瑞贝卡写代码 |
| **Proactive规则引擎设计** | quiet-musing | **洛琪希** | 需要规则抽象+状态机设计 |
| **TTS中断改造** | 无（小改动） | **瑞贝卡** | 2小时搞定，不需要复杂推理 |
| **情绪帧区间改造** | 无（小改动） | **瑞贝卡** | 改config.py+pet.py两处 |
| **代码质量审查** | — | **GLaDOS** | 极致逻辑分析，找边界条件 |
| **用户体验走查** | 情绪感知 | **爱弥斯** | 感受"这个交互对不对" |
| **写文档/说明书** | 文本定稿5分 | **奥菲莉娅**（我） | 结构化文档输出 |

### 4.3 协作模式

#### 场景A：新增一个功能（如Proactive主动对话）

```
Phase 0: 奥菲莉娅识别任务 → L3复杂 → 需要多人协作
Phase 1: 洛琪希牵头设计（系统拆解5分）
         → 瑞贝卡执行编码（执行落地5分）
         → 爱莉丝做代码评审（逻辑论证5分）
Phase 2: LRC四阶段执行
Phase 3: 奥菲莉娅交付验收
```

#### 场景B：修一个小bug（如TTS不中断）

```
Phase 0: 奥菲莉娅识别 → L1简单 → 单人执行
Phase 1: 直接派给瑞贝卡
Phase 2: 瑞贝卡编码 + 自测
Phase 3: 奥菲莉娅验收
```

#### 场景C：架构重构（如记忆系统升级）

```
Phase 0: 奥菲莉娅识别 → L3复杂 → 需要深度设计
Phase 1: GLaDOS做逻辑攻防（逼最优解）
         → 洛琪希出架构方案（系统拆解5分）
         → 瑞贝卡执行编码
         → 爱莉丝做压力测试评审
Phase 2: 分段执行，每段Review
Phase 3: 奥菲莉娅交付验收
```

### 4.4 助手能力速查

| 任务类型 | 首选助手 | 备用助手 | 能力维度 |
|---------|---------|---------|---------|
| 写代码、改代码、快速出活 | 瑞贝卡 | — | 执行落地5 |
| 方案设计、架构拆解 | 洛琪希 | — | 系统拆解5 |
| 代码评审、找漏洞 | 爱莉丝 | GLaDOS | 逻辑论证4-5 |
| 极致逻辑分析、逼最优解 | GLaDOS | 爱莉丝 | 逻辑论证5 |
| 用户体验、情绪走查 | 爱弥斯 | — | 情绪感知5 |
| 写文档、定稿子、结构化输出 | 奥菲莉娅 | — | 文本定稿5 |
| 复杂多步任务协调 | 奥菲莉娅 | 洛琪希 | 信息梳理3+系统拆解3 |

---

## 五、通信协议速查

### 5.1 WebSocket模式（推荐）

```
桌宠 (ws_client)  <->  WS Server (:19900)  <->  Hanako Agent

桌宠 -> WS 发送: {"type": "outbox", "text": "...", "character": "ophelia"}
Agent -> WS 发送: {"type": "response", "reply": "...", "anim": "idle", "emotion": "happy", "audioPath": "..."}
Agent -> WS 推送事件: {"type": "tool_start", "data": {...}}
Agent -> WS 推送事件: {"type": "text_delta", "data": {...}}
```

### 5.2 文件桥接模式（fallback）

```
桌宠 -> 写入 ~/.hanako/plugins/hanako-desktop-companion/outbox.json
Agent -> 读取 outbox.json (companion_outbox 工具)
Agent -> 写入 response.json (companion_send 工具)
桌宠 -> 读取 response.json (hanako_monitor._read_response)
```

### 5.3 HTTP通知模式

```
POST /api/notify
Body: {"text": "用户消息", "character": "ophelia"}
-> 服务端写入 outbox.json + .pending
```

---

## 六、文件变更追踪

### 已完成的文件

| 文件 | 行数 | 状态 | 最后修改意图 |
|------|------|------|-------------|
| `main.py` | ~30 | 完成 | 入口，QApplication初始化 |
| `pet.py` | ~1200 | 完成 | 主窗口，所有UI+行为逻辑 |
| `bubble.py` | ~180 | 完成 | 对话气泡组件 |
| `eye_overlay.py` | ~120 | 完成 | 瞳孔跟踪overlay |
| `config.py` | ~80 | 完成 | 配置管理+角色信息 |
| `behavior.py` | ~50 | 完成 | 行为模式参数化 |
| `break_notifier.py` | ~160 | 完成 | 空闲检测+递进提醒 |
| `foreground_watcher.py` | ~180 | 完成 | 前台窗口检测+分类 |
| `action_linker.py` | ~160 | 完成 | 关键词->动作高亮 |
| `tts_player.py` | ~120 | 完成 | TTS音频播放 |
| `ws_client.py` | ~200 | 完成 | WebSocket客户端 |
| `ws_server.py` | ~180 | 完成 | WebSocket服务器 |
| `hanako_monitor.py` | ~300 | 完成 | 状态监控+情绪映射 |
| `memory_store.py` | ~300 | 部分 | ChromaDB索引禁用 |
| `harness_adapter.py` | ~120 | 完成 | 本地LLM对话 |
| `api.py` | ~50 | 废弃 | 被harness_adapter替代 |
| `startup_screen.py` | ~100 | 完成 | 启动画面overlay |
| `character_editor.py` | ~120 | 完成 | 角色设定编辑器 |
| `build_spritesheet.py` | ~180 | 完成 | 精灵图集生成 |
| `tools/companion_outbox.js` | ~60 | 完成 | Agent工具 |
| `tools/companion_send.js` | ~40 | 完成 | Agent工具 |
| `routes/api.js` | ~60 | 完成 | HTTP API |
| `manifest.json` | ~30 | 完成 | 插件注册 |

### 待新建的文件

| 文件 | 用途 | 优先级 | 状态 |
|------|------|--------|------|
| `memory_compressor.py` | 对话记忆压缩引擎 | P0 | ✅ 已完成 |
| `proactive_scheduler.py` | 主动对话规则引擎 | P1 | ✅ 已完成 |

### 需修改的文件

| 文件 | 修改内容 | 优先级 |
|------|---------|--------|
| `pet.py` | 两处插入 `_tts_player.stop()` | P2 | ✅ 已完成 |
| `config.py` | EXPRESSION_MAP改为帧区间格式 | P3 | ✅ 已完成 |
| `pet.py` | `_set_anim_seq()`新增emotion参数 | P3 | ✅ 已完成 |
| `pet.py` | `_anim_tick()`帧范围约束 | P3 | ✅ 已完成 |
| `pet.py` | `_on_hanako_state()`传递emotion | P3 | ✅ 已完成 |
| `hanako_monitor.py` | EXPRESSION_MAP.get适配tuple | P3 | ✅ 已完成 |
| `pet.py` | 初始化ProactiveScheduler+回调 | P1 | ✅ 已完成 |
| `foreground_watcher.py` | 新增last_category property | P1 | ✅ 已完成 |
| `config.json` + `config.py` | 新增proactive配置段 | P1 | ✅ 已完成 |
| `memory_store.py` | 集成CompressionEngine | P0 | ✅ 已完成 |
| `harness_adapter.py` | 新增tool_result_buffer | P4 | 待定 |
| `companion_bridge.py` | 新建桥接守护 | — | ✅ 已完成 |

---

## 七、启动与运行

### 前置条件

- Python 3.12+
- PySide6 (`pip install PySide6`)
- Pillow（spritesheet生成）
- websockets（WS Server）
- requests（harness_adapter）
- （可选）chromadb、sentence-transformers（记忆语义检索）

### 启动方式

```bash
# 方式1: 直接运行
cd W:\Games\Hanako\Work\projects\oc-pet
python main.py

# 方式2: 双击启动脚本
启动桌宠.bat

# 方式3: 调试模式（显示日志）
# 修改 main.py 中 logging level 为 DEBUG
```

### 启动WS Server（如需Agent实时通信）

```bash
python ws_server.py
# 监听 ws://0.0.0.0:19900/companion
```

### 插件安装

`hanako-desktop-companion/` 目录已注册为 Hanako 插件，放置在：
```
~/.hanako/plugins/hanako-desktop-companion/
```

---

## 八、与N.E.K.O.整合的已决策项

以下决策已在 `计划-NEKO-integration-plan.md` 中确定，暂不实施：

| 不采纳项 | 理由 |
|---------|------|
| ZeroMQ消息总线 | WS Server已覆盖，ZMQ过度 |
| 三服务器架构 | Hanako Agent已承担后端，无需拆分 |
| Live2D/VRM渲染 | 帧精灵是差异化优势 |
| Hot-swap会话预加载 | Hanako端会话切换零延迟 |
| 26条REST路由 | oc-pet是前端，不需要 |
| Steam Workshop集成 | 角色共享走GitHub/文件复制即可 |

---

> 本文档为项目审计+操作手册的合一版本。  
> 状态会随开发进度更新。下次审计建议在P0完成后。
