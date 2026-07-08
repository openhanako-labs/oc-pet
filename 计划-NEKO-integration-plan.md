# oc-pet × N.E.K.O. 架构整合方案

> 基于 Project N.E.K.O. (Networked Emotional Knowing Organism) 架构分析，对 oc-pet 桌面宠物进行功能增强。  
> 分析日期：2026-07-01  
> 分析来源：https://github.com/Project-N-E-K-O/N.E.K.O. + 架构文档

---

## 目录

1. [现状素描](#1-现状素描)
2. [N.E.K.O. 架构摘要](#2-neko-架构摘要)
3. [整合方案总览](#3-整合方案总览)
4. [P0 — 对话记忆压缩](#p0--对话记忆压缩)
5. [P1 — Proactive 主动对话](#p1--proactive-主动对话)
6. [P2 — TTS 可中断管线](#p2--tts-可中断管线)
7. [P3 — 情绪系统连续参数](#p3--情绪系统连续参数)
8. [P4 — Agent 回调注入](#p4--agent-回调注入)
9. [不采纳项与理由](#9-不采纳项与理由)
10. [执行路线图](#10-执行路线图)

---

## 1. 现状素描

### 1.1 架构分层

```
┌─────────────────────────────────────────────────────┐
│                     oc-pet (桌面端)                    │
│                                                       │
│  ┌──────────┐  ┌──────────┐  ┌──────────────────┐   │
│  │ pet.py   │  │ bubble   │  │ EyeOverlay       │   │
│  │ (主窗口)  │  │ (对话气泡) │  │ (瞳孔跟踪)       │   │
│  │ 帧精灵    │  │ 打字机    │  │ 鼠标跟随        │   │
│  │ 呼吸浮动   │  │ 淡入动画  │  │                 │   │
│  └────┬─────┘  └──────────┘  └──────────────────┘   │
│       │                                               │
│  ┌────▼─────┐  ┌──────────┐  ┌──────────────────┐   │
│  │ ws_client │  │HanakoMon │  │ MemoryStore      │   │
│  │ (WS收发)  │  │ (状态轮询)│  │ JSONL+ChromaDB   │   │
│  └────┬─────┘  └──────────┘  └──────────────────┘   │
│       │                                               │
│  ┌────▼─────┐  ┌──────────┐                          │
│  │ ws_server │  │ BreakNoti│  ForegroundWatcher      │
│  │ (:19900)  │  │ 久坐提醒  │  前台窗口检测           │
│  └──────────┘  └──────────┘                          │
│                                                       │
│  Behavior(4模式)  ActionLinker(动作联动)  TTSPlayer   │
└──────────────────────┬───────────────────────────────┘
                       │ WS + 文件桥接
┌──────────────────────▼───────────────────────────────┐
│                Hanako Agent                           │
│  companion_outbox / companion_send 插件               │
└──────────────────────────────────────────────────────┘
```

### 1.2 现有模块清单

| 模块 | 功能 | 状态 |
|------|------|------|
| `pet.py` | 主窗口：帧精灵动画、呼吸浮动、拖拽、右键菜单 | ✅ |
| `bubble.py` | 对话气泡：白底圆角、打字机效果、淡入动画、高亮模式 | ✅ |
| `eye_overlay.py` | 瞳孔跟踪：鼠标跟随、定时刷新 | ✅ |
| `config.py` | 配置管理：角色信息、情绪映射、行为参数 | ✅ |
| `behavior.py` | 行为模式：quiet/normal/active/cling (4参数化) | ✅ |
| `ws_server.py` | WebSocket 服务器 (:19900) | ✅ |
| `ws_client.py` | WebSocket 客户端 | ✅ |
| `hanako_monitor.py` | Hanako 状态监控：WS 事件驱动 + 情绪映射 | ✅ |
| `memory_store.py` | 对话记忆：JSONL 持久化 + ChromaDB 语义检索 | ✅ |
| `harness_adapter.py` | 本地 LLM 适配器 | ✅ |
| `break_notifier.py` | 久坐提醒：系统空闲检测 + 多档位文案 | ✅ |
| `foreground_watcher.py` | 前台窗口检测：进程名 + 分类映射 | ✅ |
| `action_linker.py` | 动作联动：关键词检测 → 高亮菜单项 | ✅ |
| `tts_player.py` | TTS 播放器 | ✅ |
| `startup_screen.py` | 启动画面 | ✅ |
| `character_editor.py` | 角色设定编辑器 | ✅ |
| `hanako-desktop-companion/` | Hanako 插件：消息桥接工具 + HTTP API | ✅ |
| `api.py` | 本地对话 API | ✅ |

---

## 2. N.E.K.O. 架构摘要

### 2.1 三服务器设计

```
┌────────────────────────────────────────────────────────┐
│                    N.E.K.O. System                      │
│                                                         │
│  Main Server (:48911)     Memory Server (:48912)        │
│  ┌──────────────────┐    ┌──────────────────────┐      │
│  │ FastAPI + WebSocket│    │ SQLite + VectorStore │      │
│  │ LLMSessionManager  │    │ time-indexed原始     │      │
│  │ TTS管线(可中断)    │    │ 压缩摘要(long-term)  │      │
│  │ Hot-swap 会话      │    │ 语义检索             │      │
│  │ 26 REST 路由       │    │ 定期压缩             │      │
│  └────────┬─────────┘    └──────────────────────┘      │
│           │                                               │
│           │ ZMQ PUB/SUB + PUSH/PULL                     │
│           │                                               │
│  Agent Server (:48915)                                   │
│  ┌──────────────────────────────────────┐                │
│  │ Planner → Executor → Analyzer       │                │
│  │ MCP / Computer Use / Browser Use     │                │
│  │ 结果注入 → 下一轮 LLM 对话           │                │
│  └──────────────────────────────────────┘                │
└────────────────────────────────────────────────────────┘
```

### 2.2 关键机制

| 机制 | 描述 | oc-pet 是否已有 |
|------|------|----------------|
| Hot-swap 会话预加载 | 后台预创建下一轮会话，零切换延迟 | ❌ 不需要 |
| 记忆压缩 | 定期总结旧对话，节省上下文 | ❌ |
| Proactive 主动对话 | 空闲+环境检测 → 主动搭话 | ⚠️ 有检测无联动 |
| TTS 可中断 | 用户输入时立刻丢弃未播放 TTS | ❌ |
| 情绪连续参数 | 连续 blend shape 权重，非离散切换 | ❌ |
| Agent 回调注入 | 工具结果注入下一轮对话 | ❌ |
| 每角色隔离 | 独立的 session manager / queue / lock | ⚠️ 只有角色切换 |

---

## 3. 整合方案总览

### 3.1 决策原则

1. **不重复造轮子**：Hanako Agent 已承担 LLM 会话管理，不另起炉灶
2. **保持扁平**：不引入 ZMQ 或三进程架构，WS (:19900) 作为唯一桥接点
3. **帧精灵是差异化优势**：不向 Live2D/VRM 靠，在帧精灵表达空间内做优化
4. **渐进式**：每项改动可独立部署，不妨碍现有功能

### 3.2 优先级矩阵

| 优先级 | 功能 | 工作量 | 收益 | 风险 |
|--------|------|--------|------|------|
| **P0** | 对话记忆压缩 | ~1天 | 长期对话不掉质量 | 低 |
| **P1** | Proactive 主动对话 | ~半天 | 交互深度显著提升 | 低 |
| **P2** | TTS 可中断 | ~2小时 | 即时体验改善 | 极低 |
| **P3** | 情绪连续参数 | ~半天 | 视觉质变 | 低 |
| **P4** | Agent 回调注入 | ~半天 | 仅本地 LLM 场景受益 | 中 |

---

## P0 — 对话记忆压缩

### 现状

```
对话完成 → write to memory.jsonl (追加)
          → write to ChromaDB (异步)
          → 检索时 search_semantic() / search()
          → 未压缩 → prompt 持续膨胀
```

MemoryStore 有存储层和检索层，缺少压缩层。长期对话后，注入 prompt 的上下文会越来越大，尤其在 ChromaDB 不可用的降级模式下。

### 目标架构

```
对话完成 → write to memory.jsonl (不变)
          → write to ChromaDB (不变)
          → 压缩引擎：
               if 本角色未压缩记录 >= 50 条:
                   读取最近 50 条 → summarizer → 压缩摘要
                   写入 compressed.jsonl
                   标记已压缩
          → 检索时：
               ChromaDB 可用 → 语义检索 + 最近 5 条 raw
               ChromaDB 不可用 → compressed + 最近 5 条 raw
```

### 文件变更

| 文件 | 改动 |
|------|------|
| `memory_store.py` | 新增 `CompressionEngine` 类；新增 `compressed.jsonl` 读写；新增 `format_compressed()` 方法 |
| 新增 `memory_compressor.py` | 压缩引擎：阈值检测、summarizer 调用、压缩条目管理 |

### 压缩条目格式

```json
{
  "compressed_id": "cmp_001",
  "character": "ophelia",
  "source_range": ["2026-06-01T12:00:00", "2026-06-01T18:00:00"],
  "summary": "用户和奥菲莉娅讨论了写作项目，提到角色设定和世界观。用户对N.E.K.O.项目感兴趣。",
  "key_points": ["用户正在写小说", "对AI伴侣平台感兴趣"],
  "dialogue_count": 50,
  "compressed_at": "2026-06-01T19:00:00",
  "confidence": 0.85
}
```

### 压缩触发策略

- **阈值触发**：未压缩 raw 记录 >= 50 条时自动压缩
- **闲时压缩**：BreakNotifier 检测到空闲时触发
- **手动触发**：右键菜单新增"整理记忆"选项

### LLM 成本

使用 harness_adapter 中的本地 LLM 做 summarizer，不额外调用外部 API。每 50 条对话压缩一次，单次压缩约 1K tokens。

---

## P1 — Proactive 主动对话

### 现状

```
BreakNotifier (空闲检测)  ──→ 触发气泡提醒 (久坐提醒文案)
ForegroundWatcher (窗口检测) ──→ 触发情绪回调
```

两条线独立运作，没有产生"主动对话"行为。

### 目标架构

```
BreakNotifier ──┐
                ├──→ ProactiveScheduler
ForegroundWatch ──┘      │
                    ┌─────▼──────┐
                    │ 规则引擎     │
                    │             │
                    │ 空闲>5min   │
                    │ + 前台=编辑  │ → 询问是否休息
                    │             │
                    │ 空闲>30min  │
                    │ + 前台=游戏  │ → 撒娇求关注
                    │             │
                    │ 空闲>15min  │
                    │ + 刚结束对话  │ → 不打扰 (cooldown)
                    └─────┬──────┘
                          │
                    ┌─────▼──────┐
                    │ 触发对话     │
                    │ → WS 发给   │
                    │   Hanako    │
                    │ Agent 回复  │
                    └────────────┘
```

### 文件变更

| 文件 | 改动 |
|------|------|
| 新增 `proactive_scheduler.py` | 规则引擎：空闲检测+窗口分类→触发决策 |
| `pet.py` | 初始化 ProactiveScheduler；绑定回调；管理 cooldown 状态 |
| `config.json` → `proactive` 配置段 | 新增配置项 |

### 配置项

```json
{
  "proactive": {
    "enabled": true,
    "cooldown_minutes": 10,
    "rules": [
      {
        "idle_min": 5,
        "foreground": ["code", "editor", "browser_read"],
        "prompt": "写了这么久，休息一下吧？",
        "weight": 0.7
      },
      {
        "idle_min": 30,
        "foreground": ["game"],
        "prompt": "带我一起玩嘛～",
        "weight": 0.5
      },
      {
        "idle_min": 60,
        "foreground": ["*"],
        "prompt": "还在忙吗？想和你说说话～",
        "weight": 0.3
      }
    ]
  }
}
```

### 触发流程

1. ProactiveScheduler 每 30 秒检查一次（与 BreakNotifier 同频）
2. 匹配规则 → 检查 cooldown → 随机权重判定
3. 通过后 → 通过 WS 给 Hanako Agent 发送 proactive 消息
4. Agent 回复 → 桌宠显示气泡

### 与 BreakNotifier 的边界

| | BreakNotifier | ProactiveScheduler |
|---|---|---|
| 触发条件 | 系统空闲 | 空闲 + 前台窗口分类 |
| 响应方式 | 硬编码气泡文案 | 发给 Agent 生成回复 |
| 文案风格 | 提醒（喝水/休息） | 对话（撒娇/关心/卖萌） |
| 可定制性 | 文案列表 | 规则引擎 + Agent 生成 |

---

## P2 — TTS 可中断管线

### 现状

用户输入新消息时，正在播放的 TTS 不会被停止，导致前后声音重叠。

### 改动

在 `pet.py` 的 `_send_message()` 方法入口和 `_on_bridge_message` 收到新回复时，各插入一条 `self._tts_player.stop()`。

```python
# pet.py

def _send_message(self):
    # 用户发送新消息 → 立即截停当前 TTS
    self._tts_player.stop()  # ← 新增
    # ... 原有发送逻辑

def _on_bridge_message(self, msg):
    # 收到新回复 → 截停当前 TTS（防止旧回复未播放完）
    self._tts_player.stop()  # ← 新增
    # ... 原有处理逻辑
```

### `tts_player.py` 改动

```python
class TTSTtsPlayer:
    def stop(self):
        """立即停止当前播放并丢弃队列"""
        # 停止当前播放
        # 清空待播放队列
        # 重置播放状态
```

### 文件变更

| 文件 | 改动 |
|------|------|
| `tts_player.py` | 新增 `stop()` 方法 |
| `pet.py` | 两处调用 `self._tts_player.stop()` |

---

## P3 — 情绪系统连续参数

### 现状

```python
EXPRESSION_MAP = {
    "happy": "extra",       # 开心 → extra 序列
    "angry": "extra",       # 生气 → 也是 extra 序列
    "surprised": "extra",   # 惊讶 → 还是 extra 序列
    ...
}
```

所有非中性情绪都映射到同一个 `extra` 帧序列，无法区分表达。

### 目标

如果 extra 序列有多帧（假设 8 帧），用帧索引的子范围区分不同情绪：

```
extra 序列帧分配：
  [0-1]  → happy
  [2-3]  → angry
  [4-5]  → surprised
  [6-7]  → thinking
```

### 变更

#### `config.py` — 情绪映射改为帧区间

```python
EXPRESSION_MAP = {
    "happy":      ("extra", 0, 1),   # (序列名, 起始帧, 结束帧)
    "angry":      ("extra", 2, 3),
    "surprised":  ("extra", 4, 5),
    "thinking":   ("extra", 6, 7),
    "working":    ("extra", 6, 7),
    "neutral":    ("idle",  0, None),  # None = 全序列
    "sad":        ("idle",  0, None),
    "listening":  ("idle",  0, None),
    "speaking":   ("idle",  0, None),
}
```

#### `pet.py` — 帧选择逻辑

```python
def _set_anim_seq(self, seq_name, emotion=None):
    """根据情绪选择帧序列和子范围"""
    if emotion and emotion in EXPRESSION_MAP:
        seq, start, end = EXPRESSION_MAP[emotion]
        self._anim_seq = seq
        self._anim_range = (start, end)  # 帧区间
        self._anim_idx = start or 0
    else:
        # fallback 到旧逻辑
        ...
```

`_anim_tick()` 推进时，只在 `self._anim_range` 范围内循环，不越界。

### 文件变更

| 文件 | 改动 |
|------|------|
| `config.py` | `EXPRESSION_MAP` 格式改为帧区间 |
| `pet.py` | `_set_anim_seq()` 新增 emotion 参数；`_anim_tick()` 帧范围约束 |

### 额外收益

如果帧精灵图集足够丰富，可以进一步细化为：
- `extra[0]` = happy_low, `extra[1]` = happy_high
- 配合呼吸浮动幅度，形成"情绪强度"连续空间

---

## P4 — Agent 回调注入

### 场景

桌宠不接 Hanako，走本地 LLM（harness_adapter）时，Agent 工具执行结果需要被 LLM 引用。

### 架构

```
用户输入 → harness_adapter
            │
            ├── 调用 LLM → 回复
            │
            └── 如果 LLM 触发了工具调用：
                执行工具 → 得到结果
                结果摘要 → 注入下一轮对话的 system prompt
                重新生成回复（或等待用户主动问）
```

### 文件变更

| 文件 | 改动 |
|------|------|
| `harness_adapter.py` | 新增 `tool_result_buffer`；回复生成时注入工具结果摘要 |

### 注

这条路径仅在**不接 Hanako、使用本地 LLM** 时有效。如果桌宠永远走 Hanako Agent，Agent 已经自动做了回调注入，无需额外处理。

---

## 9. 不采纳项与理由

| N.E.K.O. 模块 | 不采纳理由 |
|---|---|
| **ZeroMQ 消息总线** | 现有 WS Server (:19900) 已覆盖所有通信场景。ZMQ PUB/SUB + PUSH/PULL 是为跨进程高吞吐场景设计的，对桌面应用过度 |
| **三服务器架构** | Hanako Agent 已承担 Agent Server + LLM 会话管理角色。再拆分三进程只会增加运维复杂度，不会带来实质收益 |
| **Live2D/VRM 渲染** | 帧精灵是差异化优势——零模型制作门槛、低资源占用、风格统一。Live2D 模型需专人制作，偏离 oc-pet 的轻量定位 |
| **Hot-swap 会话预加载** | N.E.K.O. 需要 hot-swap 是因为它自己做 LLM 会话管理（创建会话慢）。oc-pet 的 LLM 会话在 Hanako 端，切换零延迟 |
| **26 条 REST 路由** | oc-pet 是桌面前端，不是后端平台。只需要现有的 outbox/send 两条桥接通道 |
| **Steam Workshop 集成** | 角色共享可以走 GitHub / 直接文件复制，不依赖 Steam 平台 |

---

## 10. 执行路线图

### 阶段一：即时体验改善（~2天）

```
Step 1: [P2] TTS 可中断管线
  改动 tts_player.py + pet.py
  验证：输入新消息时旧 TTS 立即停止

Step 2: [P3] 情绪连续参数
  改动 config.py + pet.py
  验证：不同情绪显示不同帧区间
```

### 阶段二：交互深度提升（~1.5天）

```
Step 3: [P1] Proactive 主动对话
  新建 proactive_scheduler.py
  改动 pet.py + config.json
  验证：空闲时自动触发 Agent 对话

Step 4: [P1] 规则调优
  观察用户使用习惯，调 idle 阈值和权重
```

### 阶段三：长期记忆优化（~1天）

```
Step 5: [P0] 对话记忆压缩
  新建 memory_compressor.py
  改动 memory_store.py
  验证：50 条对话后自动压缩，prompt 注入质量不下降

Step 6: [P0] 压缩检索集成
  memory_store.py 检索方法增加 compressed 降级路径
```

### 阶段四：锦上添花（可选）

```
Step 7: [P4] Agent 回调注入（仅本地 LLM 场景）
  改动 harness_adapter.py
```

### 总工作量

| 阶段 | 内容 | 估计工时 |
|------|------|----------|
| 一 | TTS 可中断 + 情绪连续参数 | ~2 天 |
| 二 | Proactive 主动对话 | ~1.5 天 |
| 三 | 记忆压缩 | ~1 天 |
| 四 | Agent 回调注入 | ~0.5 天 |
| **合计** | | **~5 天** |

---

> 本文档为 v1.0 方案，所有改动独立可逆，不破坏现有功能。  
> 实际执行中可根据需要在阶段间插入验证或调整。