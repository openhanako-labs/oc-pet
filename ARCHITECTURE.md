# oc-pet 架构

> 给新对话的助手看：读完这份文档就能理解代码结构，不用逐文件扫描。
> 最后更新：**2026-09-19**（§1 Qt 边界 / §13 技术债 / §15 漂移台账 已核实修正）
> 前版：2026-09-17 对照真实代码重写；再前版 2026-08-12 已严重过时
> 规模：约 **83,059 行** 自有 Python（350 文件）+ 测试 **18,669 行**（110 文件）
> ⚠ 本文档数字会漂——**先看 §15「文档漂移台账」**，那里列了所有已证伪的项

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

**依赖方向严格向上**：L1 不知道 L5 存在。

### Qt 依赖的真实边界（2026-09-19 核实修正）

旧措辞「L3 不 import 任何 Qt」**不成立**，已改为如实描述：

| 类型 | 数量 | 位置 | 性质 |
|---|---|---|---|
| **模块级硬依赖** | 1 | `core/window_interaction.py:24` | `from PySide6.QtCore import QTimer, QPoint, QRect`。窗口几何计算，无法回避 |
| **延迟导入（软依赖）** | 5 | `core/conversation_engine.py:309,519,522` · `core/memory_facts.py:359` · `core/memory_reflection.py:233` · `core/perception/proactive_generation.py:159` | 均包在 `try/except ImportError` 内，无 Qt 时**退化为同步路径** |

**为什么允许**：这 5 处都是「后台线程需要把回调送回主线程」，而 Qt 的
`Signal` 是唯一合规通道。它们是**有意的软依赖**，不是架构失控——
删掉会让这些模块在无 Qt 环境下直接崩，而不是优雅退化。

**不变式应改写为**：L3 原则上不依赖 Qt；跨线程回调需 Qt Signal 桥时
允许**延迟 import + 降级兜底**；`window_interaction` 是唯一的模块级例外。

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

## 3. PetWindow：接线分布（2026-09-17 重构后）

```python
class PetWindow(AudioMixin, AnimationMixin, InteractionMixin, ChatMixin,
                BehaviorMixin, VoiceProviderMixin, PlayMixin, BubbleMixin,
                InterfaceMixin, PerceptionMixin, QWidget):
```

**3,182 行**（重构前 3,839，**已降 657 行**）。`__init__` 只有 37 行，
直接调 **10 个** `_init_*`；其余是嵌套调用。

### 接线归属（技术债①已清偿大部分）

| 归属 | 方法 | 状态 |
|---|---|---|
| **pet.py**（骨架，不搬） | `_init_diag_switches` `_init_states` `_init_engine` `_init_voice_audio` `_init_visual_startup` `_init_neko_t05` | 与 Qt 窗口/渲染器强耦合 |
| **interface_mixin.py** | MCP / 状态口 / 外部触发 | 第一批（250 行） |
| **perception_mixin.py** | `_init_schedulers` + `_init_neko_p1` 及五条线 | 第二/五批 |
| **panels_mixin.py** | `_init_neko_panels` + `_init_multi_pet_greeting` | 第三批 |
| **interaction_mixin.py** | `_init_interaction` | 第四批 |
| **play_mixin.py** | `_init_play_layer` | 早已搬 |

### 判据：不是「它属于谁」，而是「它依赖什么」

搬家前逐个量了耦合面（self.属性数 / 调 pet.py 方法数）：

| 方法 | 行 | self.属性 | 调 pet.py 方法 | 结论 |
|---|---|---|---|---|
| `_init_multi_pet_greeting` | 22 | 2 | 0 | ✅ 已搬 |
| `_init_neko_panels` | 79 | 12 | 0 | ✅ 已搬 |
| `_init_interaction` | 45 | 29 | 0 | ✅ 已搬 |
| `_init_schedulers` | 126 | 25 | 0 | ✅ 已搬 |
| `_init_visual_startup` | 91 | 36 | **11** | ❌ 搬了更乱 |
| `_init_engine` | 101 | 44 | 4 | ❌ 与信号声明强绑 |

**关键发现**：`_init_visual_startup` 调 11 个 pet.py 内部方法
（`_setup_window` / `_setup_ui` / `_setup_menu` / `_setup_tray` …），
搬走等于把窗口构建拆成两半——**那是设计改动，不是搬家**。

### 安全绳（重构的前提）

`tests/test_signal_contract.py`（13 例）在重构**之前**写好，钉死：

| 类别 | 数量 | 搬漏的表现 |
|---|---|---|
| Signal 声明及参数签名 | 18 | 跨线程解包出错 |
| 关键跨线程连接（Signal→槽） | 18 | 回调静默失效 |
| **QTimer.timeout → 槽** | 15 | 「某个功能不刷新」 |
| **控件信号 → 槽** | 11 | 「那个交互没反应」 |
| QAction.triggered → 槽 | 4 | 菜单入口丢失 |
| `_init_*` 存在且可达 | 20 | 初始化链断裂 |
| `__init__` 调用顺序 | 16 | 初始化依赖错序 |
| pet.py 行数上限 | — | 接线又堆回 pet.py |
| **连接总数不减少** | ≥51 | 任何一条丢了 |

**为什么必须**：纯搬家重构的最大风险是搬漏一条连接——那会让某个回调
静默失效（不报错、不崩溃，只是功能再也不响应），现有测试抓不到。

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
├── pet.py             PetWindow（3182 行，见 §3）
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
├── pet_mixins/        PetWindow 行为拆分（11 文件）
│   ├── interface_mixin.py    对外接口（MCP / 状态口 / 外部触发）
│   ├── perception_mixin.py   调度器 + P1 感知/记忆集成
│   ├── panels_mixin.py       附属面板（聊天/记忆/角色卡）+ 多宠
│   └── ...（其余 8 个）
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

> 2026-09-19 核实更新：`_init_*` 数量、事件数、运行时拓扑均已修正，
> 详见 §15「文档漂移台账」。

1. **`pet.py` 仍有 11 个 `_init_*` 未搬**（旧写 6 个，漏统计 5 个）：
   - 文档原记的 6 个：`_init_diag_switches`(171) `_init_states`(186)
     `_init_engine`(262) `_init_voice_audio`(364) `_init_visual_startup`(500)
     `_init_neko_t05`(592)
   - **漏记的 5 个**：`_init_companion_memory`(927) `_init_lip_sync`(1105)
     `_init_a2a`(1125) `_init_llm_gate`(1204) `_init_game_watch`(1220)
     —— 这 5 个是嵌套调用（不在 `__init__` 直接调），此前未被计入
   - 真正卡住的仍是两个：`_init_visual_startup` 调 11 个内部方法
     （拆它=拆窗口构建）、`_init_engine` 与 18 个 Signal 声明强绑。
     搬这两个需要先抽接口——**那是设计改动，不是搬家**（见 §3）

   ⚠ **这不是「有空再清」的债，是「下次加功能必须先清」的债**：
   护栏测试 `tests/test_signal_contract.py::test_pet_py_does_not_regrow`
   设了**基线 3456 行 / 上限 3550 行**，而 pet.py 当前 **3,479 行**——
   **余量只剩 71 行**。`pet_mixins/perch_mixin.py` 的注释记载了一次实证：
   作者把 148 行行为代码直接写进 pet.py，被护栏拦下后才搬进 mixin。
   → 12 个 mixin 的多重继承组装模型已接近极限，继续「搬家」收益递减，
   根本解法见 §15 附注。

2. **`_rebuild` 的 use-after-cleanup 残留**：`old.cleanup()` 与 worker 合成竞态
3. **屏幕感知与对话共用 provider**：429 限流来源；已支持 per-agent
   `models.vision` 但用户尚未配置。
   ⚠ `core/perception/screen.py` 已从文档所记的 924 行涨到 **1,180 行（+28%）**，
   是本项目演进最快的模块，也是 73% LLM 时间的消耗方——文档写好就过时。
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
| `docs/架构图.md` | ⚠ **已作废**，见 §15.3。仅历史参考 |
## 15. 文档漂移台账（2026-09-19 核实）

> 本文档 2026-09-17 重写，**两日内已漂**。给新对话的助手：
> 下面这张表是本文件所有**已被证伪**的数字，以本表为准。

### 15.1 规模数字

| 项 | 旧写 | 实测（2026-09-19） | 说明 |
|---|---|---|---|
| 自有 Python | 71,500 行 | **83,059 行**（350 文件） | 不含 `third_party_reference/` 17,436 行 |
| 测试 | 17,700 行 / 75 文件 | **18,669 行 / 110 文件** | +47% 文件数 |
| `pet.py` | 3,182 行 | **3,479 行** | 距护栏上限 3550 仅剩 **71 行** |
| `core/perception/screen.py` | 924 行 | **1,180 行** | **+28%**，演进最快 |
| `core/harness_adapter.py` | 1,180 行 | **1,338 行** | +13% |
| `env_config.py` | 402 行 | **595 行** | +48% |
| `core/conversation_engine.py` | 2,508 行 | **2,595 行** | +3% |
| `core/perception/controller.py` | 646 行 | 646 行 | ✅ 仍准确 |
| `core/perception/proactive.py` | 967 行 | 967 行 | ✅ 仍准确 |

### 15.2 结构数字

| 项 | 旧写 | 实测 |
|---|---|---|
| PetWindow 继承 mixin | 10 个 | **12 个**（漏 `PanelsMixin` `PerchMixin`） |
| pet.py 内 `_init_*` | 6 个 | **11 个** |
| EventBus 事件 | 11 个 | **19 个**（漏的 8 个在 `play_mixin` 7 个 + `game/session` 的 `game_session`） |
| EventBus 订阅 | — | **仅 5 处**（pet.py 2 + interface_mixin 2 + pet_audio_bridge 1）。埋点远多于消费 |
| `ThreadPoolExecutor` | 5 处 | **2 处**（conversation_engine / memory_embedding） |
| `QThread` | 5 处 | **0 处实例化**。只有 `QThread.currentThread()` 静态查询 |
| Hana 工具数（plugins 26 / Apps 9 / MCP 3 / skills 92 / agents 7） | — | **不可静态验证**。全部来自 `~/.hanako/` 运行时快照；本机今日实测 skills 91 / plugins 23 / agents 8 / Apps 7 |

### 15.3 ⚠ `docs/架构图.md` 已作废

**该文件的「🌱 养成系统」整块子图（约 30+ 节点）在代码中不存在。**

全仓 grep 无任何匹配：`ItemRegistry` `WorkRegistry` `MissionSystem`
`MissionPool` `MissionTracker` `GachaSystem` `PetStateManager`
`PetSaveManager` `WorkTimer`。

代码中留着明确的弃用空壳注释：
- `pet.py:1358` — 「注入 P0 养成系统（已弃用）。保留空方法以兼容旧调用方；
  状态 / 喂食 / 工作 / 任务玩法已移除」
- `pet_mixins/interaction_mixin.py:526` — 「养成系统移除后，该方法保留空壳
  以避免旧调用方报错」

**该图其余部分也过时**（漏 PanelsMixin / PerchMixin、QThread 虚报）。
画图 / 理解架构**一律以本文件为准**，`docs/架构图.md` 仅作历史参考。

### 15.4 屏幕感知的配额隔离真相（2026-09-19 团队评审后修正）

> 这一节纠正一个曾被写进技术债的错误诊断，务必读完再动屏幕感知。

**旧诊断（§13.3，部分错误）**：「屏幕感知与对话共用 provider，是 429 主因；
已支持 per-agent `models.vision` 但用户尚未配置」。

**实测修正**：

| 事项 | 旧认知 | 实测 |
|---|---|---|
| `utility_model` 是否已配 | 「用户尚未配置」 | ✅ **已配**：`~/.hanako/user/preferences.json` → `utility_model = deepseek-v4-flash`（日日新）。`harness_adapter.py:223` 的 `_UTILITY_SOURCES` 已把 memory_extract / memory_reflect / screen_enrich / proactive / idle 五源切走 |
| 屏幕**视觉**分析走哪条路 | 以为走 adapter 的 utility 隔离 | ❌ **不走**。`screen.py:826-856` 是**独立 HTTP 直连**，自己读 `get_vision_config()`，完全绕过 `harness_adapter` |
| `phash_threshold` | 以为默认 0（关） | ✅ **已开**：`config.json` → `screen.phash_threshold = 4`，经 `pet.py:2569` / `perception_mixin.py:177` 两处接线 |
| 事件爆发合并 `_burst_enabled` | 以为默认关 | ✅ **默认开**：`screen.py:271` 初值 False，但 `screen.py:280` 导入成功即置 True（`EventBurstCoalescer` 可用时） |

**结论**：五个省钱的杠杆里，**三个早已拉到位**。
真正剩下的缺口是：**屏幕视觉（vision）走的是 `vision_model`，
而 `~/.hanako/user/preferences.json` 里 `vision_model = agnes-3.0-flash`
与 `utility_model` 是不同 provider——视觉这条流没有和对话隔离在同一层，
它靠的是另一套 `models.vision` 配置链。**

→ 所以「429 主因」不能笼统归为「共用 provider」。
排查时应**分别看 vision 与 utility 两条流的日志**，而不是一起调。

### 15.4.1 LLM 归因埋点（2026-09-19 已实装）

**之前无法回答的问题**：撞 429 时，到底是哪条流在撞？
`llm_gate` 只有一个全局 int `_hits_429`，于是只能笼统归因，进而调错杠杆。

**已补的能力**（`core/llm_gate.py`，**放行/冷却语义一字未改**）：

| 新增 | 说明 |
|---|---|
| `_hits_429_by_source` | 429 按 source 累计。**不因 `notify_ok()` 复位**——它是归因证据 |
| `_rejects_by_source` | 被闸门拦下的次数（区分「没调用」和「被拦了」） |
| `by_stream` | 按 **vision / utility / chat / other** 汇总用量 + 429 + 拒绝 |
| `stream_of(source)` | 归类表：`vision`/`enrich`→vision；`proactive`/`idle`/`memory_*`→utility；`user`/`direct`→chat |
| `attribution_report()` | 一行人可读归因，见下 |

**为什么全局计数复位、按 source 的不复位**：
全局 `_hits_429` 驱动**冷却翻倍**（连续撞墙 → 指数退避），必须复位；
按 source 的是**事后归因证据**，一次成功就抹掉会让答案消失。

**日志出口**：每 10 分钟一条（可配 `llm_gate.attribution_log_minutes`，
`0` = 关）。独立低频定时器 `_llm_attribution_timer`（**不用 50ms 的
`_unified_tick`**，那不适合拼字符串打日志），已在 `closeEvent` 清理列表。

**输出样例**：

```
LLM 归因 | vision: 用0/160 429=2 拒=3 | utility: 用0/30 429=1 拒=1
```

**怎么看**：
- `429` 集中在 vision → 调 `phash_threshold` / 拉长截屏间隔 / 换 `vision_model`
- `429` 集中在 utility → 换 `utility_model` / 收紧 per-source 预算
- `429` 出现在 chat → 用户对话被后台挤占，需要优先级保护（当前**没有**）
- `拒` 远大于 `429` → 是**自己的预算在拦**，不是 provider 限流，别去调 provider

⚠ **已知缺口**：chat（用户对话）**没有槽位保护**。
`llm_gate.py:30` 注释明说「抢不到槽就放弃，不排队」，
后台流可能把用户对话挤掉。若归因日志显示 chat 的 `拒` 不为 0，
应优先修这条（用户来源绕过闸门）。

**测试**：`tests/test_llm_gate_attribution.py`（20 例），
含一条 `test_stats_keeps_legacy_fields` 钉死 `stats()` 是**纯增量**
（既有消费者：pet.py:1214 / a2a.py:323 / llm_gate.py:385）。

### 15.5 表演层：不要建「统一表演队列」（2026-09-19 团队评审结论）

曾提议把气泡 / TTS / 表情 / 动作四个出口统一为「表演意图」进单一队列调度。
**评审结论：驳回。四个出口时间性质不同，硬归一为净收益负。**

| 出口 | 现有仲裁 | 时间性质 | 状态 |
|---|---|---|---|
| 表情 / 动作 / Live2D 参数 | `avatar/motion_mixer.py:132` MotionMixer（Layer 1-4 + 3s 冷却 + can_interrupt + easing） | 状态叠加 | ✅ 已解决 |
| TTS 音频 | `ui/tts_player.py:89` `play()` = `stop()` + `play()` | 互斥 | ✅ 已解决 |
| 气泡 | `bubble_mixin.py:112` 优先级 + `_pending_bubbles` + `:99` 2s 去重 | 可见栈 | ⚠ 半解决，见下 |

→ 真正的症状不在「缺统一调度器」，而在下面这个具体 bug：

#### ✅ 已修复：`_pending_bubbles` 每次只出队一条（2026-09-19）

**病灶**：`_clear_hanako_bubble` 的 docstring 写「依次弹出」，
但 `while` 循环体内有 `return` —— 每次超时**只弹第一条**。
且 `_pending_bubbles` 全仓只有这一个消费者。

**后果**：同时排入 3 条低优先级通知 → 只显示第 1 条，
其余要等下一次 `hide_bubble` 才轮到 → 通知丢失 + 顺序错乱。

**修法（注意：不是简单删 `return`）**：直接删会死循环——
`_show_bubble_impl` 在气泡可见时会把 `_bubble_priority` 抬到当前条优先级，
剩余低优先级条目随即被**重新入队**，`while` 永不结束。

现实现（`bubble_mixin.py::_clear_hanako_bubble_impl`）：
整体取出队列 → 清空 → 按优先级降序（同级保序）逐条尝试 →
只要有一条真正上屏就停。既能排空，也不会死循环。

**位置**：逻辑已迁入 `pet_mixins/bubble_mixin.py`（pet.py 有行数护栏，
且这段属气泡职责）；`pet.py` 保留薄入口 `_clear_hanako_bubble` →
调 `_clear_hanako_bubble_impl()`，信号接线不变。

**回归测试**：`tests/test_bubble_queue_drain.py`（9 例）。
已做反向验证：还原成旧实现后 2 例转红，确认测试真能抓到该 bug。

#### ✅ 已收敛：一条回复的交付契约（2026-09-19）

`_do_engine_reply_inner` 里「文字何时上屏 / 音频何时起播」的逻辑
与清洗文本、动画派发混在一起，散落 `_pending_bubble_text` 等暂存变量。

已抽出 `bubble_mixin.py::_deliver_reply(display_text, emotion, audio_path)`，
契约不变：**有音频 → 文字暂存等 `on_tts_start`；无音频 → 立即显示；
空回复 → 清掉「思考中」**。测试见 `test_bubble_queue_drain.py` 后 4 例。

#### ✅ 已收敛：celebrating 三处节流 → 一处（2026-09-19）

原先两处各做一遍 5s 时间节流，外加一道并发锁：

| 位置 | 判定 | 状态 |
|---|---|---|
| `_do_hanako_state`（旧 236-240） | 5s 时间节流 | ❌ 已删（重复） |
| `_do_celebrating`（旧 380-382） | 并发锁 `_celebration_in_progress` | ✅ 保留 |
| `_do_celebrating`（旧 385-388） | 5s 时间节流（注释「防御其他入口直调」） | ❌ 已删（重复） |

**关键发现**：`_do_celebrating` 全仓**只有 `_do_hanako_state` 一个调用者**，
所以「防御其他入口直调」是过时假设——第二处时间节流形同虚设。

**两道判定不等价，都保留**（合并成一道会漏）：
- **并发锁**：上一次还没演完（3s revert 未到 / 合成线程未收尾）→ 挡「多条庆祝序列叠加」
- **时间节流**：演完了但太近 → 挡「连播两次撒花」

现统一为 `bubble_mixin.py::_celebration_should_proceed()`，
日志文案也区分开了（不然出问题时说不清是被谁拦的）。
测试：`tests/test_celebrating_throttle.py`（10 例），
其中两条专门钉死「少任何一道都会漏」。

#### 其余证据（未修，但已在案）

- `pet.py:3100-3183` —— 状态气泡节流，注释记录实测：
  **564 个气泡里 339 个（60%）是「正在思考…」这类占位文案**
  （最密 7 秒弹 22 个）。这是「少说点」的问题，不是节流能解决的。

### 15.6 pet.py 的结构性困境：它不是太大，是形状错了

> 2026-09-19 团队评审诊断。**给未来接手的人：pet.py 的问题不是行数，是边界。**

#### 根本矛盾

pet.py 是 **God Object 伪装成 mixin 模式**。12 个 mixin 不是独立行为组件，
而是**全部依赖 PetWindow 提供共享 self 属性的「方法捆绑包」**——
搬到 mixin 只换了存放位置，**耦合面零减少**。

每个 mixin 的头部注释都自陈：「访问 self.xxx / self.yyy 等，
**均由 PetWindow 提供（鸭子类型，无需 import pet）**」。
这是设计意图，不是疏忽——但 GoF 的 mixin 模式要求 mixin 不依赖宿主属性，
这里不满足。

#### pet.py 实际承担的 7 个角色

| 角色 | 证据 |
|---|---|
| Qt 窗口壳 | `_setup_window` / `_setup_ui` / `_setup_menu` / `_setup_tray`（pet.py:1514 / 2061 / 2244 / 1609） |
| 渲染器宿主 | `self._renderer` 被引用 **30+ 次** |
| 引擎宿主 | `self._engine` 被引用 **20+ 次**，挂载 8 个回调 |
| 信号中枢 | **20 个 Signal 声明**（pet.py:90-131）+ 50+ 条连接 |
| 状态容器 | **~100 个 self 属性**（`_init_states` 定义 37 个，`_init_visual_startup` 27 个） |
| Tick 调度器 | 5 个 QTimer 共用事件循环（pet.py 内 QTimer 出现 **31 次**） |
| 初始化编排 | 11 个 `_init_*`（见 §13.1） |

#### 各 mixin 的依赖面（实测）

`BehaviorMixin` 依赖 11 个属性 · `BubbleMixin` 12 · `ChatMixin` 10 ·
`AnimationMixin` 8 · `InteractionMixin` 7 · `VoiceProviderMixin` 6 等。

⚠ **跨模块私有属性访问**（不是鸭子类型，是硬编码依赖）：
- `voice_provider_mixin.py:240-242` → `self._engine._lock` / `self._engine._tts` / `self._engine._tts_ready`
- `bubble_mixin.py:209-210` → `self._engine._adapter`
- `chat_mixin.py:369-372` → `self._engine._adapter._reply_timeout`
- `pet.py:3434` → `self._engine._thread`

#### ✅ 已清理：4 处跨模块私有访问（2026-09-19）

这是 PetSystem 拆分的**前置**——引擎内部实现泄漏到 UI 侧，
就没法把引擎搬进 PetSystem 而不动 UI 代码。

在 `ConversationEngine` 上新增四个**语义操作**（**不是 getter**，
把私有属性换名字暴露等于没改）：

| 新方法 | 替代 | 用途 |
|---|---|---|
| `set_tts_provider(p)` | `_lock` / `_tts` / `_tts_ready` | 原子替换 TTS（持引擎锁，返回 ready） |
| `transport_mode()` | `_adapter.transport_mode` | 「当前是不是 Hanako 模式」 |
| `reply_timeout_sec()` | `_adapter._reply_timeout` | 「该等多久」，缺失回退 180 |
| `join_thread(timeout)` | `_thread` | 关闭时等线程收尾，返回是否结束 |

`pet.py` 与 `pet_mixins/` 中 `_engine._*` 现已为 **0**
（`test_engine_public_api.py::test_no_engine_private_access_in_ui_side` 钉死）。

⚠ **原子性测试的一个重要教训**：第一版用「4 线程 × 3000 次切换观测撕裂态」，
并把实现**故意改成非原子**验证——结果**仍然通过**，说明那是假测试。
CPython 的 GIL 让两步赋值被读到中间态极难稳定复现。
改为**确定性验证**（把 `_lock` 换成探针锁，断言赋值发生在临界区内 +
只获取一次锁）后，反向验证正确转红 2 例。见 `tests/test_engine_public_api.py`。

⚠ **残留**：`self._engine` 本身（非私有属性）仍在 UI 侧被广泛引用（20+ 处）。
那是下一个层级的问题，属于 PetSystem 拆分本身，不在本次范围。

#### 推荐解法：PetSystem（领域）+ PetShell（Qt 壳）

| 层 | 职责 | 边界（可 grep 验证） |
|---|---|---|
| **PetSystem** | 领域 God Object：状态 / 引擎 / 感知 / 渲染 / 物理 / 运动 / 全部 mixin 方法 | **不含 `from PySide6`** |
| **PetShell** | Qt 适配壳：20 Signal + QTimer + 窗口 / 菜单 / 托盘 | **不含 `self._engine` / `self._renderer` / `self._perception`** | 

**为什么不是「组合优于继承」**：组件对象仍需宿主引用来拿 `_renderer`，
耦合从「隐式共享命名空间」变成「显式宿主引用」，**零减少**，只是更难追踪。

**为什么接受 God Object**：引擎 / 感知 / 渲染 / 物理 / 运动这些组件**天然耦合**，
强行拆开只会增加间接层。**问题不是「God Object 太大」，
是「God Object 和 Qt 窗口是同一个类」。**

#### 护栏该怎么改

**行数上限是差指标**——它测症状（类太大），不是病因（职责混杂）。
更好的约束（均可 grep/CI 验证）：

1. PetSystem 中 `from PySide6` 计数 **必须 = 0**
2. PetShell 中 `self._(engine|renderer|perception|physics|motion)` 计数 **必须 = 0**
3. PetShell 的 Signal 声明数 **≤ 20**（冻结）
4. 禁止 `self._engine._*` 跨模块私有访问（当前 4 处，见上）
5. 保留既有的 `test_signal_contract`（≥51 条连接不减少）—— 那是好约束

若保留行数上限，应改为 **PetShell ≤ 500 / PetSystem ≤ 3,500**，
而不是 pet.py ≤ 3,550。这样测的是边界，不是症状。

---

