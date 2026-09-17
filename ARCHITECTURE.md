# oc-pet 架构

> 给新对话的助手看：读完这份文档就能理解代码结构，不用逐文件扫描。
> 最后更新：**2026-09-17**（对照真实代码重写；上一版 2026-08-12 已严重过时）
> 规模：约 **71,500 行** Python（含测试 17,700 行），75 个测试文件

---

## 0. 一句话定位

**oc-pet 是一个翻译层**：把 Hana 的结构化事件（会话、工具、记忆、感知）
翻译成一只会眨眼、会说话、会走动的存在。

它不是聊天客户端——聊天只是它的一个出口。它的核心是**持续在场的状态机**：
即使没人跟它说话，它也在看屏幕、读日程、算情绪、决定要不要开口。

---

## 1. 分层架构（自下而上）

```
┌─────────────────────────────────────────────────────────────────┐
│  L5  交付层   气泡 / TTS 语音 / Live2D 动作 / 粒子 / HUD        │
├─────────────────────────────────────────────────────────────────┤
│  L4  表达层   情绪状态机 → 动画映射 → 口型 → 语气               │
├─────────────────────────────────────────────────────────────────┤
│  L3  能力层   对话引擎 / 感知 / 记忆 / 工具路由 / MCP            │
├─────────────────────────────────────────────────────────────────┤
│  L2  适配层   Hana 桥（WS / HTTP / catalog / 文件）             │
├─────────────────────────────────────────────────────────────────┤
│  L1  骨架层   PetManager / PetWindow / 事件总线 / 配置          │
└─────────────────────────────────────────────────────────────────┘
```

**依赖方向严格向上**：L1 不知道 L5 存在；L3 不 import 任何 Qt。

---

## 2. 启动链路（L1 骨架）

```
main.py
  ├─ faulthandler + crash_collector        # C++ 崩溃栈收集（最先装）
  ├─ _setup_file_logging()                 # logs/oc_pet.log 滚动日志
  ├─ QApplication + 主题系统
  ├─ PetManager()
  │    ├─ _load_config()
  │    ├─ _init_hanako_ws()                # 共享一条 WS 连接
  │    └─ MultiPetBridge                   # 多宠社交事件
  ├─ manager.launch_all()                  # 每个 enabled agent 一个窗口
  │    └─ launch_window(agent) → PetWindow
  ├─ 业务就绪哨兵 logs/ready_<pid>.flag     # launcher 据此区分启动期/运行期崩溃
  ├─ 主线程心跳 logs/heartbeat_<pid>.txt    # launcher 看门狗检测 GUI 卡死
  └─ app.exec()
```

**launcher.py 是守护进程**：拉起 main.py 子进程，读心跳文件；主线程卡死
→ 心跳停更 → 强杀 + 自动复活。启动期崩溃不算「健康运行」。

---

## 3. PetWindow：god-object 的现状（诚实记录）

```python
class PetWindow(AudioMixin, AnimationMixin, InteractionMixin, ChatMixin,
                BehaviorMixin, VoiceProviderMixin, PlayMixin, BubbleMixin,
                QWidget):
```

**3,839 行**，其中 `__init__` 通过 **20 个 `_init_*` 方法**接线：

| 分组 | 方法 |
|---|---|
| 基础 | `_init_diag_switches` `_init_states` `_init_schedulers` `_init_interaction` |
| 对话 | `_init_engine` `_init_voice_audio` |
| 视觉 | `_init_visual_startup` `_init_play_layer` |
| N.E.K.O. 移植 | `_init_neko_t05` `_init_neko_panels` `_init_neko_p1` |
| P1 感知 | `_init_p1_anti_repeat` `_init_p1_screen_enrich` `_init_p1_fact_store` `_init_p1_reflection` `_init_p1_embedding_check` |
| 外部接口 | `_init_status_http` `_init_external_trigger` `_init_mcp_server` |
| 多宠 | `_init_multi_pet_greeting` `_init_companion_memory` |

**已知技术债**：10 个 mixin 只搬了方法，`__init__` 仍是接线中心。
拆分方案见 `docs/optimization-plan.md` 第 10 项（未执行）。

---

## 4. 对话流水线（核心链路，L3）

```
用户输入（文本 / 语音）
  ↓ chat_mixin
ConversationEngine.send()          # 递增 generation（代际）
  ↓ 入队
后台线程 _run 出队 → _process_message
  ├─ 内置帮助 / 能力路由（命中则跳过 LLM）
  ├─ perception.build_context(source)   # 注入时间/日程/屏幕/情绪
  ├─ adapter.chat()                     # → Hanako WS（见 §5）
  ├─ 工具调用循环（最多 N 轮）
  ├─ parse_action_intent + parse_emotion  # 解析 [feel:]/[do:]/[emotion:]
  ├─ 情绪 → 动画映射
  └─ TTS 合成（线程池 max_workers=1）
       ↓
  on_reply 信号 → 主线程 _do_engine_reply → 气泡 + 播放
```

**代际打断**：每次 send/interrupt 递增 `_generation`；LLM 后 / TTS 前 /
TTS 后三处 `_is_stale` 检查，作废旧回复。

**TTS 竞态**：合成移入 `ThreadPoolExecutor`；`_tts`/`_tts_ready` 读写持锁。

---

## 5. Hana 桥（L2 适配层，最关键的一层）

桌宠不自己实现 LLM/工具/记忆——**全部借道 Hana**。

### 5.1 传输模式（三选一）

| 模式 | 行为 |
|---|---|
| `direct` | 跳过 Hana，直连 LLM API（无工具/无记忆） |
| `prefer_hanako` | **当前默认**。优先 Hana，失败 fallback 到 direct |
| `hanako_only` | 仅 Hana，不允许 fallback |

### 5.2 配置优先级（2026-09-17 核实）

```
.env 有值 → 用它
  ↓ 空
Hanako provider-catalog.json（走 agent config.yaml 的 models.<slot>）
  ↓ 缺
内置默认
```

**实测**：本机 `.env` 的 LLM/VISION/TTS/ASR 键**全空**，所以全部走 Hana。
`.env` 实际只剩 `QWEN_TTS_MODE` / `QWEN_TTS_REPO_PATH` 两个真值。

**关键**：`get_vision_config(agent_id)` 支持从 agent config 读
`models.vision`（与 `models.chat` 同级），2026-09-17 新增。

### 5.3 角色设定读取（两条路，同一份文件）

| 通道 | 触发 | 读什么 |
|---|---|---|
| 服务端 WS | `prefer_hanako`（实际在用） | `~/.hanako/agents/<agent_id>/` |
| 本地直连 | Hana 不可用时兜底 | 同上 |

`core/hanako_context.py::HanakoContext` 是唯一读取器：
`identity.md` → `description.md` → `AGENTS.md`(→ishiki→awareness) → `pinned`

**桌宠读的就是主 agent 的人设**，没有独立的「桌宠形态」层。

### 5.4 消息注入的现状（重要）

桌宠发给 Hana 的 text 会被拼装：

```
[pet-context]
[当前时间：...]
[延迟任务]
- 有 N 个任务待处理
[任务巡检]
- 新增 N 个延迟任务
[/pet-context]

[消息来源(user)] 用户原话
```

**为什么拼进 text 而不是 system 层**：实测 WS 的 `prompt` 消息字段白名单是
`text / images / videos / audios / displayMessage / uiContext / sessionFileRefs`
——**没有 `context` 通道**。`context`（可进 system 层）只有 HTTP API 支持，
而 HTTP 没有发送端点。`uiContext` 只给 `ui_inspect` 读。

→ **结论：WS 是唯一路径，拼进 text 是当前唯一可行方案。**
  因此该块的**内容必须精简**（2026-09-17 已把哈希 ID 换成计数）。

---

## 6. 感知系统（L3，`core/perception/`，6,595 行）

```
PerceptionController（中枢，646 行）
  ├─ TimePerception         时间上下文（时段/工作日）
  ├─ EmotionStateMachine    情绪状态机 + 衰减
  ├─ SchedulePerception     日程（cron / 延迟任务 / 插件任务）
  ├─ InspectionPerception   Hanako 任务巡检（5 分钟轮询，30 分钟节流）
  ├─ ScreenPerception       屏幕感知（924 行，最重）
  │    ├─ 截屏 → 视觉 LLM → 活动/场景分类
  │    ├─ LLM 语义增强（可选，有冷却）
  │    └─ ScreenObserverProcess  独立进程观察者
  ├─ MediaPerception        SMTC 媒体播放
  ├─ ProactiveScheduler     主动对话调度（967 行）
  ├─ FocusPerception        专注模式
  └─ 环境扫描 / 手机活动 / 健康状态
```

`build_context(source)` 按来源决定注入什么：
- `user`：不注入屏幕内容（避免"回复里冒出你在看什么"）
- `proactive` / `idle` / `screen_enrich`：注入屏幕

**性能实测**（2026-09-11~16 日志）：

| 来源 | 次数 | 合计耗时 | 均 | 最大 |
|---|---|---|---|---|
| `screen_enrich` | 31 | **484.2s** | 15.6s | 59.6s |
| `proactive` | 5 | 74.9s | 15.0s | 20.9s |
| `memory_reflect` | 3 | 70.4s | 23.5s | 29.0s |

屏幕感知占全部 LLM 时间约 **73%**，而 162 次分析只产出两种情绪。
**与对话共用同一 provider**，是 429 限流的主要来源。

---

## 7. 表达层（L4）

### 情绪 → 动画
```
[feel:v,a]  连续 VA 坐标 → 逐帧插值（_va_target → _va_cur）→ 面部参数
[emotion:x] 离散情绪 → 预设表查表（兜底路径）
[do:名称]   语义标签 → _AI_DO_ALIASES 归一 → motion/preset
```

**优先级**：`[feel:]` 存在时不再补 `[expression:]`（VA 更精细，静态参数会盖掉它）。

### 口型（三级来源，逐级回退）
1. **音素级**：文本 → 拼音 → 口型时间轴（`core/lip_sync.py`）
2. **词级**：Edge TTS `WordBoundary` → `<音频>.words.json`
3. **能量分段**：分帧 RMS → 自适应阈值（**任何 provider 都能用**）

侧车缺失/损坏**一律回落原包络**，绝不因口型影响出声。

### 语气（`tts_provider/emotion_prosody.py`）
**硬规矩：语速绝对不能变**，只允许 pitch / volume / 语气描述改变语气。
理由：语速是「这个人说话的样子」里最稳的特征。

---

## 8. 能力层（L3）

### 8.1 工具路由
```
unified_tool_router（526 行）
  ├─ 本地插件（plugins/，Node subprocess，无 shell）
  ├─ Hana 五套体系（hana_catalog.py，466 行）
  │    plugins(26) / Apps(9) / MCP(3) / skills(92) / agents(7)
  └─ 能力路由（capability_registry.py，关键词直接执行，跳过 LLM）
```

### 8.2 桌宠作为 MCP 提供方
`core/mcp_server.py` → `127.0.0.1:8979/mcp`，暴露 `pet_*` 工具给 Hana。
Hana 侧 connector 已配置并实测连通。

### 8.3 记忆
```
memory_facts / memory_hybrid / memory_embedding / memory_reflection
memory_filter（只用事实类，避免"AI 太懂我"的恐怖谷）
memory_snapshot（导出/导入）
```

---

## 9. 事件总线（解耦用）

`core/event_bus.py` 极简发布/订阅（on / off / emit / clear）。

**全仓实际使用的事件**（11 个）：

```
activity_event  external_trigger  mcp_action  multi_pet_event
pet_set_mode    phone_event       proactive_triggered
screen_analyzed screen_scene      screen_scene_enriched
window_interacted
```

用途：让感知层/外部接口能通知 UI，而不反向 import。

---

## 10. 运行时拓扑

**单进程 + 多线程 + 少量子进程**：

| 类型 | 数量 | 用途 |
|---|---|---|
| `Thread` | 41 处 | 感知轮询 / 对话后台 / 监控 |
| `ThreadPoolExecutor` | 5 处 | TTS 合成 / ASR 并发 |
| `QThread` | 5 处 | Qt 侧后台 |
| `subprocess` | 42 处 | 插件工具 / CosyVoice worker / ffmpeg |

**多宠**：每个 enabled agent 一个 `PetWindow`（同进程）。
Live2D `l2d.init()` 进程级只调一次，关单个宠不释放全局 GL。

---

## 11. 目录地图

```
oc-pet/
├── main.py            入口（286 行）
├── launcher.py        守护进程（347 行）：心跳看门狗 + 自动复活
├── pet.py             PetWindow（3839 行，god-object，见 §3）
├── pet_manager.py     多宠管理（833 行）
├── config.py          配置读写（545 行）
├── env_config.py      环境变量（402 行，多为空壳回退 Hana）
│
├── core/              核心逻辑，84 文件 31746 行（无 UI 依赖）
│   ├── conversation_engine.py   对话引擎（2508 行）
│   ├── hanako_session_manager.py 会话管理（1210 行）
│   ├── harness_adapter.py       LLM 适配（1180 行）
│   ├── hanako_monitor.py        状态监控（792 行）
│   ├── perception/              感知（20 文件 6595 行，见 §6）
│   └── ...（记忆/工具/MCP/桥接等 50+ 模块）
│
├── pet_mixins/        PetWindow 行为拆分（8 文件 3133 行）
├── ui/                UI 组件（38 文件 11609 行）
├── avatar/            渲染（15 文件 6628 行）
├── tts_provider/      TTS 引擎（12 文件 3030 行）
├── asr_provider/      ASR 引擎（6 文件 704 行）
├── motion/            运动物理（7 文件 1389 行）
├── characters/        角色资源（模型 + pet.json）
├── tests/             测试（75 文件 17691 行）
└── docs/              文档
```

---

## 12. 关键机制速查

| 机制 | 位置 | 要点 |
|---|---|---|
| 代际打断 | `conversation_engine` | `_generation` 递增 + 三处 `_is_stale` |
| TTS 异步 | `conversation_engine` | `ThreadPoolExecutor(max_workers=1)` |
| 状态气泡节流 | `pet.py::_do_engine_status` | 1.5s 窗口；**失败提示不过滤**；回复到达即取消待发 |
| 标签残渣清理 | `hanako_monitor::clean_bubble_text` | 先消费整对协议标签，再剥元信息 |
| 口型三级回退 | `lip_sync` / `word_timings` / `audio_timings` | 侧车缺失一律回落原包络 |
| 语气不变语速 | `emotion_prosody` | 只改 pitch/volume |
| 屏幕避让对话 | `screen.py::_should_enrich` | `busy_check()` 为真时不发 enrich |
| 模型配置链 | `env_config` | `.env` → agent `models.<slot>` → catalog |

---

## 13. 已知技术债

1. **`pet.py` 仍是 god-object**：20 个 `_init_*` 集中接线（3839 行）
2. **`_rebuild` 的 use-after-cleanup 残留**：`old.cleanup()` 与 worker 合成竞态
3. **屏幕感知与对话共用 provider**：429 限流来源；已支持 per-agent
   `models.vision` 但用户尚未配置
4. **`[pet-context]` 只能进 text**：WS 无 system 通道（见 §5.4）
5. **`env_config.py` 的 16 个空键**：历史遗留，实际只 2 个在用

---

## 14. 文档地图

| 文档 | 用途 |
|---|---|
| **本文件** | 架构总览（最先读） |
| `docs/STATUS.md` | 语音/互通事项总账（逐项状态） |
| `docs/voice-interop-plan.md` | 语音互通详细方案 + 调研存档 |
| `docs/optimization-plan.md` | 优化项清单（含未执行项） |
| `CHANGELOG.md` | 版本变更史 |
| `docs/维护指南.md` | 日常维护 |
