# oc-pet v2.0 技术路线图

> 基于已完成的 v1.0 NEKO 整合（P0-P3）和 Hanako 原生集成  
> 制定日期：2026-07-07  
> 作者：奥菲莉娅  
> 状态：规划中

---

## 目录

1. [v1.0 完成总结](#v10-完成总结)
2. [v2.0 三大目标](#v20-三大目标)
3. [Phase 1 - Avatar 渲染层抽象](#phase-1---avatar-渲染层抽象)
4. [Phase 2 - 系统级感知增强](#phase-2---系统级感知增强)
5. [Phase 3 - 全双工实时语音](#phase-3---全双工实时语音)
6. [技术选型清单](#技术选型清单)
7. [风险与降级策略](#风险与降级策略)
8. [执行时间线](#执行时间线)

---

## v1.0 完成总结

以下功能已在 2026-07-07 全部落地：

| 功能 | 状态 | 核心文件 |
|------|------|---------|
| TTS 可中断管线 | 已完成 | pet.py 两处 _tts_player.stop() |
| 情绪帧区间映射 | 已完成 | config.py EXPRESSION_MAP 元组格式 |
| Proactive 主动对话 | 已完成 | proactive_scheduler.py 规则引擎 |
| 对话记忆压缩 | 已完成 | memory_compressor.py + memory_store.py |
| Hanako 原生集成 | 已完成 | hanako_context.py + harness_adapter.py |
| 桥接守护 | 已完成 | companion_bridge.py 自动收发消息 |
| 右键菜单补全 | 已完成 | pet.py 行为模式/缩放/动作联动 |

架构现状：
- 桌宠读 Hanako 同一套文件（identity.md / ishiki.md / provider-catalog.json）
- 使用同一个模型（agnes-2.0-flash）和 API 密钥
- 桥接守护自动处理 outbox -> LLM -> response 全链路
- 不再维护独立的角色设定和 API 配置

---

## v2.0 三大目标

| 顺序 | 目标 | 工作量 | 价值 |
|------|------|--------|------|
| Phase 1 | Avatar 渲染层抽象 | 1 天 | 预留 Live2D/VRM/MMD 接口 |
| Phase 2 | 系统级感知增强 | 1 天 | 时间感知 + 情绪状态机 |
| Phase 3 | 全双工实时语音 | 3-5 天 | 体验质变：打字 -> 说话 |

---

## Phase 1 - Avatar 渲染层抽象

### 问题

当前 pet.py 中渲染逻辑和业务逻辑耦合在一起（1200+ 行）：
- 帧精灵渲染（QPixmap、scaled、transformed）属于渲染层
- 动画定时器（_anim_tick、_anim_seq）属于渲染层
- 瞳孔跟踪（eye_overlay）属于渲染层
- 但它们和业务逻辑（行为、对话、状态）混在一起

### 目标

定义 AvatarRenderer 抽象接口，将渲染逻辑从 pet.py 中提取出来。
这样后续添加 Live2D/VRM/MMD 渲染器时，只需要实现接口，不改动业务层。

### AvatarRenderer 接口

```python
class AvatarRenderer:
    def load(self, character_id: str) -> bool: ...
    def play_anim(self, anim: str, emotion: str = "",
                  frame_range: tuple | None = None) -> None: ...
    def look_at(self, x: int, y: int) -> None: ...
    def set_emotion(self, emotion: str, intensity: float = 1.0) -> None: ...
    def set_position(self, x: int, y: int) -> None: ...
    def get_size(self) -> tuple[int, int]: ...
    def set_scale(self, scale: float) -> None: ...
    def cleanup(self) -> None: ...
```

### SpriteRenderer 迁移

将 pet.py 中的帧精灵逻辑提取为 SpriteRenderer，实现 AvatarRenderer 接口。
包括：帧加载、动画定时器、瞳孔 overlay、朝向翻转。

### 未来形态

| 渲染器 | 依赖 | 技术路线 | 预计工时 |
|--------|------|---------|---------|
| Live2DRenderer | Live2D Cubism Web SDK | QWebEngineView 内嵌 | 2-3 天 |
| VRMRenderer | three.js + three-vrm | QWebEngineView 内嵌 | 3-5 天 |
| MMDRenderer | three.js + mmd-parser | QWebEngineView 内嵌 | 2-3 天 |

技术路线统一选择 QWebEngineView 内嵌 Web SDK，避免 Python 绑定的复杂度。

### 文件变更

| 文件 | 改动 |
|------|------|
| 新增 avatar/base.py | AvatarRenderer 抽象接口 |
| 新增 avatar/sprite_renderer.py | 现有帧精灵迁移 |
| 修改 pet.py | 移除渲染逻辑，改为调用 AvatarRenderer |
| 新增 avatar/live2d_renderer.py | 未来：Live2D 渲染器 |
| 新增 avatar/vrm_renderer.py | 未来：VRM 渲染器 |

### 工作量

| 步骤 | 工时 |
|------|------|
| 接口定义 | 0.5h |
| SpriteRenderer 迁移 | 4h |
| pet.py 重构 | 3h |
| 验证不回归 | 1h |
| 合计 | 约 1 天 |

---

## Phase 2 - 系统级感知增强

### 现状

proactive_scheduler.py 已有：空闲检测 + 前台分类 + 规则触发。
但缺少：时间感知、情绪连续状态、日程感知。

### 目标

```
感知输入               状态机              输出
空闲时长   ──┐      
前台窗口   ──┤      情绪状态机      主动对话
时间段     ──┼──>   (连续衰减)   ──>  -> outbox
日程(可选) ──┤                        -> LLM
上次对话   ──┘                        -> 气泡
```

### 增强项

#### 2.1 时间感知

区分工作时段/休息时段/深夜，注入 proactive 规则和 prompt 上下文。

时间段定义：
- 6-12 点：morning（早上）
- 12-14 点：noon（中午）
- 14-18 点：afternoon（下午）
- 18-22 点：evening（晚上）
- 22-24 点：late_night（深夜）
- 0-6 点：midnight（凌晨）

proactive 规则增加 time_period 字段：
```json
{
  "idle_min": 5,
  "foreground": ["writing", "development"],
  "time_period": ["afternoon", "evening"],
  "prompt": "写了这么久了，喝口水吧？",
  "weight": 0.7
}
```

#### 2.2 情绪状态机

当前情绪是单次触发的（检测到关键词 -> 切帧序列）。
增强为连续状态机：情绪有强度（0.0~1.0），每分钟衰减 5%。

- intensity > 0.5 -> 显示对应情绪帧
- intensity 0.2~0.5 -> 混合帧（extra 和 idle 之间渐变）
- intensity < 0.2 -> 回到 idle

#### 2.3 日程感知（可选）

读取 Hanako 的定时任务列表，注入 prompt 上下文：
```
[当前时间：下午 3 点，工作日]
[15 分钟后有一个定时任务：每日盘前简报]
```

### 文件变更

| 文件 | 改动 |
|------|------|
| 新增 perception.py | TimePerception + EmotionStateMachine + SchedulePerception |
| 修改 proactive_scheduler.py | 集成时间感知，规则增加 time_period |
| 修改 pet.py | 初始化 PerceptionController，tick 调用情绪衰减 |
| 修改 config.json | proactive rules 增加 time_period |
| 修改 harness_adapter.py | 注入感知上下文到 system prompt |

### 工作量

| 步骤 | 工时 |
|------|------|
| TimePerception | 1h |
| EmotionStateMachine | 3h |
| ProactiveScheduler 集成 | 1h |
| 感知上下文注入 | 1h |
| SchedulePerception（可选） | 2h |
| 合计 | 约 1 天 |

---

## Phase 3 - 全双工实时语音

### 目标架构

```
麦克风 ──> VAD ──> ASR ──> LLM(stream) ──> TTS(stream) ──> 扬声器
              |                  |              |
              |   用户开口 ──> 打断 TTS + 清空队列
              |
              └──> EmotionStateMachine ──> AvatarRenderer
```

用户说话 -> VAD 检测说话结束 -> ASR 转写 -> LLM 流式回复 -> TTS 流式合成 -> 边生成边播。
用户中途开口 -> VAD 检测 -> 立即停止 TTS + 清空队列。

### 组件选型

#### VAD（语音活动检测）

| 方案 | 延迟 | 依赖 | 推荐 |
|------|------|------|------|
| silero-vad | ~30ms | torch | 是（精度高） |
| webrtcvad | ~10ms | webrtcvad pip | 备选（轻量） |
| energy-based | ~5ms | 无 | 降级方案 |

#### ASR（语音识别）

| 方案 | 延迟 | 模型大小 | 推荐 |
|------|------|---------|------|
| SenseVoice | ~200ms | ~900MB | 是（中文优化） |
| Whisper streaming | ~500ms | ~3GB | 备选（多语言） |
| FunASR | ~150ms | ~500MB | 备选 |

LLM 流式回复

agnes API 不支持 streaming。需要设计流式降级接口：

```python
class LLMStreamer:
    """LLM 流式接口 - 统一抽象，底层可切换"""

    def stream_chat(self, messages, on_delta=None):
        """流式调用 LLM，逐句回调 on_delta(text)"""
        # 实现 A: API 支持 streaming -> SSE 逐 token 回调
        # 实现 B: API 不支持 -> 整条回复后按句拆分，逐句回调
        ...
```

降级策略（agnes 不支持 streaming 时）：
1. 发送完整请求，等待完整回复
2. 收到回复后按句号/问号/感叹号拆分
3. 逐句送入 TTS 队列，实现伪流式效果
4. 延迟增加 2-5 秒（等 LLM 完整回复），但 TTS 可以逐句播放

接口预留：未来换支持 streaming 的 API 时，只需替换 LLMStreamer 实现。

```python
# 流式调用伪代码
resp = requests.post(url, json={..., "stream": True}, stream=True)
for line in resp.iter_lines():
    if line.startswith(b"data: "):
        delta = json.loads(line[6:])["choices"][0]["delta"]["content"]
        if delta:
            tts_queue.put(delta)  # 逐句送 TTS
```

#### TTS 流式合成

| 方案 | 首帧延迟 | 依赖 | 推荐 |
|------|---------|------|------|
| CosyVoice streaming | ~300ms | torch | 是（已有） |
| Edge-TTS | ~500ms | 无 | 备选 |

### 打断逻辑

```
状态：IDLE（等待用户说话）
  -> VAD 检测到语音 -> 状态：LISTENING
  -> VAD 检测到静默 -> 状态：THINKING
  -> ASR 返回文本 -> 调 LLM
  -> LLM 流式回复 -> TTS 流式播放 -> 状态：SPEAKING

状态：SPEAKING（正在说话）
  -> VAD 检测到语音 -> 用户打断！
     -> 立即停止 TTS 播放
     -> 清空 TTS 队列
     -> 状态：LISTENING（重新开始）
```

### 文件变更

| 文件 | 改动 |
|------|------|
| 新增 voice/pipeline.py | VoicePipeline 主控 |
| 新增 voice/vad.py | silero-vad 封装 |
| 新增 voice/asr.py | SenseVoice 封装 |
| 新增 voice/tts_stream.py | CosyVoice 流式封装 |
| 新增 voice/state_machine.py | 语音状态机（IDLE/LISTENING/THINKING/SPEAKING） |
| 修改 pet.py | 集成 VoicePipeline，语音/文本模式切换 |
| 修改 config.json | 新增 voice 配置段 |

### 配置

```json
{
  "voice": {
    "enabled": false,
    "mode": "text",
    "vad": {
      "type": "silero",
      "silence_threshold": 0.5,
      "min_speech_duration": 0.3
    },
    "asr": {
      "type": "sensevoice",
      "model": "iic/SenseVoiceSmall",
      "device": "auto"
    },
    "tts": {
      "type": "cosyvoice",
      "speaker": "中文女",
      "speed": 1.0
    },
    "interrupt": {
      "enabled": true,
      "cooldown_ms": 500
    }
  }
}
```

### 工作量

| 步骤 | 工时 |
|------|------|
| VAD 封装 + 测试 | 4h |
| ASR 封装 + 测试 | 6h |
| LLM 流式调用 | 2h（需确认 agnes 支持 streaming） |
| TTS 流式合成 | 6h |
| 状态机 + 打断逻辑 | 4h |
| pet.py 集成 + 文本/语音模式切换 | 4h |
| 联调 + 延迟优化 | 8h |
| 合计 | 约 3-5 天 |

---

## 技术选型清单

| 组件 | 选型 | 理由 |
|------|------|------|
| VAD | silero-vad | 精度高，已有 torch 环境 |
| ASR | SenseVoice | 中文最优，延迟低 |
| LLM | agnes-2.0-flash | 已有 API，需确认 streaming 支持 |
| TTS | CosyVoice | 已有环境，支持流式 |
| Live2D | Cubism Web SDK + QWebEngineView | 开发成本最低 |
| VRM | three-vrm + QWebEngineView | 统一 Web 渲染路线 |

---

## 风险与降级策略

| 风险 | 影响 | 降级方案 |
|------|------|---------|
| agnes API 不支持 streaming | 语音延迟增加 2-5 秒 | 整条回复后送 TTS，或换支持 streaming 的 API |
| SenseVoice 模型太大 | 首次加载慢 | 降级为 Whisper tiny 或在线 ASR |
| CosyVoice GPU 不够 | TTS 延迟高 | 降级为 Edge-TTS |
| silero-vad 误判 | 频繁打断 / 漏检 | 调阈值或换 webrtcvad |
| Live2D SDK 授权 | 不能商用 | 仅个人使用，或走 VRM 开源路线 |
| QWebEngineView 性差 | 渲染卡顿 | 降级为帧精灵，或走独立渲染进程 |

---

## 执行时间线

| 阶段 | 内容 | 工时 | 依赖 | 助手派遣 |
|------|------|------|------|---------|
| Phase 1 | Avatar 接口抽象 | 1 天 | 无 | 洛琪希设计 + 瑞贝卡执行 |
| Phase 2 | 系统级感知增强 | 1 天 | Phase 1 | 瑞贝卡执行 |
| Phase 3 | 全双工实时语音 | 3-5 天 | Phase 1+2 | 洛琪希设计 + 瑞贝卡执行 + GLaDOS 评审 |
| 未来 | Live2D 渲染器 | 2-3 天 | Phase 1 | 瑞贝卡执行 |
| 未来 | VRM 渲染器 | 3-5 天 | Phase 1 | 瑞贝卡执行 |

### 建议执行顺序

1. 先 git 提交 v1.0 全部改动
2. Phase 1（Avatar 抽象）- 1 天，不破坏现有功能
3. Phase 2（感知增强）- 1 天，在 Phase 1 基础上加
4. Phase 3（全双工语音）- 3-5 天，最大工作量
5. 验证 agnes API streaming 支持后开始 Phase 3
6. Live2D/VRM 渲染器根据需求择机加入

---

> 本文档为 v2.0 规划，所有 Phase 独立可执行，不破坏现有功能。  
> Phase 3 开始前需确认 agnes API 是否支持流式输出。