# 4 个参考项目深度技术架构分析
## ——为 oc-pet LLM 情绪桥接设计提供借鉴

> **作者**：洛琪希
> **审查**：奥菲莉娅（主 agent Review）
> **分析对象**：Soullink Emotion SDK、Lumi_Nox、Mio/Lumi 双产品架构、Open-LLM-VTuber
> **目标项目**：oc-pet（Python + PySide6 + 帧精灵渲染 + 9 动画 + BehaviorParams 概率驱动）
> **核心问题**：从 LLM 输出一路到帧精灵切换的完整链路怎么设计
> **生成时间**：2026-07-19

---

## TL;DR

初版分析的盲区不在 LLM 接入本身——已经接上了 Hanako LLM、有了 TTS、有了 BehaviorParams 状态机。盲区在**"情绪从 LLM 输出到帧切换"这条管线的中间件**。4 个参考项目反复印证了三件事：

1. **不要让 LLM 直接驱动动画**。让它在文本里输出可解析的 token（`[joy]`、`<emotion>joy</emotion>`），由后置过滤器提取+剥离，再喂给渲染层。
2. **emotion 字符串 → 模型原生资产**的映射必须是配置化的（emotionMap），不要写死。
3. **情绪是产品特征，但情绪计算应是通用能力**——角色怎么表达情绪是配置层的事，底层怎么检测情绪是核心层的事。这是 Mio/Lumi 的灵魂。

剩下全文展开。

---

## 1. Open-LLM-VTuber — 完整的 Live2D 表情控制管线标杆

> **资料等级**：A+（拿到 DeepWiki 章节、`live2d_model.py` 代码路径、`conf.default.yaml`、live2d_expression_prompt.txt）
> **相关度**：最高（最直接的 emotion token → 渲染管线参考）

### 1.1 核心架构图（文字描述）

```
┌──────────────────────────────────────────────────────────────┐
│  LLM 推理层（OpenAI/Anthropic/Ollama/Groq 等 9 家后端）       │
│  ↓ 流式输出（含 [joy][anger] 等 token 的文本）                │
├──────────────────────────────────────────────────────────────┤
│  Output Transformation Pipeline（5.4 章）                    │
│   1. SentenceDivider 按标点/pysbd 切句                        │
│   2. 对每段调用 Live2dModel.extract_emotion(text) → [idx]    │
│   3. 调用 remove_emotion_keywords(text) 拿到纯净 TTS 文本    │
│   4. TTS 合成 → 同时 payload 携带 expression index + audio   │
├──────────────────────────────────────────────────────────────┤
│  WebSocket 推送（frontend 收到）                              │
│   payload = { audio, expressions: [3], display_text, volume }│
└──────────────────────────────────────────────────────────────┘

    后端                                  前端
┌──────────────┐                       ┌──────────────────┐
│ Live2dModel  │  ← emo_map（dict）    │  pixi-live2d-    │
│ .extract_emotion()                   │  display         │
│ .remove_emotion_keywords()           │  setExpression(i)│
│ .set_model(conf)                     │  setMouth(volume)│
└──────────────┘                       └──────────────────┘
```

### 1.2 关键 API / 数据结构

#### A. `emotionMap` 配置（`model_dict.json`）

```json
{
  "name": "shizuku-local",
  "defaultEmotion": 0,
  "emotionMap": {
    "neutral":   0,
    "anger":     2,
    "disgust":   2,
    "fear":      1,
    "joy":       3,
    "smirk":     3,
    "sadness":   1,
    "surprise":  3
  },
  "tapMotions": {
    "body": { "tap_body": 30, "shake": 30, "pinch_in": 20, "pinch_out": 20 },
    "head": { "flick_head": 40, "shake": 20 }
  },
  "idleMotionGroupName": "idle"
}
```

**精妙处**：emotion 是**字符串到整数索引**的纯映射，跟底层 Live2D 是 Cubism 3/4/5 完全解耦。换模型只换 `emotionMap` 值。

#### B. `Live2dModel` 类核心方法（src/open_llm_vtuber/live2d_model.py）

```python
class Live2dModel:
    def __init__(self):
        self.emo_map: dict[str, int] = {}     # emotion 字符串 → L2D 索引
        self.emo_str: str = ""                 # "fear, anger, disgust, ..." 给 prompt 用

    def set_model(self, model_info: dict):
        """初始化时构造 emo_map，keys 全部 lowercase"""
        self.emo_map = {k.lower(): v for k, v in model_info.get("emotionMap", {}).items()}
        self.emo_str = ", ".join(f"[{k}]" for k in self.emo_map.keys())

    def extract_emotion(self, str_to_check: str) -> list[int]:
        """扫描文本中的 [tag]，匹配 emo_map，返回所有命中索引"""
        found = []
        for m in EMOTION_TAG.finditer(str_to_check):
            tag = m.group(1).lower()
            if tag in self.emo_map:
                found.append(self.emo_map[tag])
        return found

    def remove_emotion_keywords(self, target_str: str) -> str:
        """从文本中剥掉 [joy] 这种 tag，给 TTS 用，避免念出来"""
        return re.sub(r"\[.*?\]", "", target_str).strip()
```

#### C. Prompt 注入模板（`prompts/utils/live2d_expression_prompt.txt`）

```text
You have access to the following emotion tags you can use:
<insert_emomap_keys>

When responding, embed emotion tags using square brackets, e.g.:
"I'm so happy to see you! [joy]"
"That makes me angry! [anger]"

Only use the available emotion tags. Do not invent new ones.
```

`<insert_emomap_keys>` 在运行时被替换为真实的 `[fear], [anger], [disgust], [sadness], [joy], [neutral], [surprise]`。

#### D. TTS 清理（关键！）

```python
# src/open_llm_vtuber/tts/gpt_sovits_tts.py:36
text_for_tts = re.sub(r"\[.*?\]", "", text)   # ← 把 [joy] 剥掉再喂 TTS
```

#### E. 流式逐句切表情

Open-LLM-VTuber 用 `SentenceDivider`（支持 regex 和 pysbd 两种切句方式），**每句话独立提取 emotion**——第一句带 `[joy]` 就先切到 joy，不需要等整段回复完。配置项：

```yaml
segment_method: 'regex'   # 或 'pysbd'（更准但依赖重）
faster_first_response: true
```

#### F. 可关闭设计

`live2d_expression_prompt` 是 `tool_prompts` 字典里的可选项（`conf.yaml`），不写就不注入，emotion 系统就不工作。设计为**可关闭的实验性功能**。

另外 `think_tag_prompt` 与 `live2d_expression_prompt` 互补：
- 外显情绪 `[joy]` → 表情切换
- 内隐动作 `<think>（偷偷看了一眼）` → 不显示但可触发动画

### 1.3 对 oc-pet 的具体启发（到代码层面）

#### 启发 1: 复用 pet.json 的 emotions 段作为 emotionMap

oc-pet 的 `pet.json` 已经有这个结构：

```json
{
  "emotions": {
    "happy":     { "anim": "waving" },
    "sad":       { "anim": "failed" },
    "surprised": { "anim": "jumping" },
    "thinking":  { "anim": "review" }
  }
}
```

**这就是 emotionMap**。接上 LLM 后只需：
1. prompt 里告诉 LLM 可以输出 `[happy]`、`[sad]`、`[surprised]`、`[thinking]`
2. 后置过滤 `EmotionBridge.extract()` 拿到情绪名
3. 调 `renderer.play_anim(emotion_name)` 查 emotions 段映射到具体动画

#### 启发 2: 把 emo_str 动态注入 system prompt

```python
emotion_str = ", ".join(f"[{e}]" for e in emo_map.keys())
persona_prompt += f"\n\nYou can express emotions using tags like {emotion_str}."
```

#### 启发 3: TTS 和气泡文本都要剥掉 emotion tags

```python
clean_text = re.sub(r"\[.*?\]", "", llm_reply)
bubble.set_text(clean_text)   # 气泡不显示 [joy]
tts.speak(clean_text)         # TTS 不念 [joy]
```

#### 启发 4: 流式逐句切表情

如果 ConversationEngine 支持流式输出，每个 chunk 过 EmotionBridge，就能逐句切动画，不需要等整段回复完。

---

## 2. Soullink Emotion SDK — LLM → Live2D 表情映射的极致抽象

> **资料等级**：C（仓库 + npm 包均未直接命中，BV 视频无法解析画面）
> **推断来源**：npm package @soullink-emotion/sdk + "LLM API 驱动 Live2D 表情和动作" + 通用 LLM-to-avatar 设计模式
> **标注**：以下架构基于通用设计原则推断，后续拿到仓库代码需校准

### 2.1 架构推断

从命名空间 `@soullink-emotion/sdk` 和描述推断，这是一个**薄包装 SDK**：

```
┌────────────────────────────────────────────────────┐
│  Soullink Emotion SDK（客户端引用）                 │
├────────────────────────────────────────────────────┤
│  EmotionRouter                                      │
│    .parse(text) → {emotion, confidence, action}    │
│    .subscribe(callback)                             │
│    .config({ model, fallbacks, smooth_ms })        │
├────────────────────────────────────────────────────┤
│  LLM Adapter（可插拔 OpenAI/Anthropic/Local）       │
│  Emotion Classifier（关键词 + LLM 二次确认）         │
│  Transition Engine（lerp / spring / ease 曲线）     │
└────────────────────────────────────────────────────┘
                       ↓
              业务侧 Live2D 模型
```

### 2.2 关键模式

**模式 A：双通道情绪识别**

```typescript
const router = new EmotionRouter({
  primaryClassifier: 'llm',      // 让 LLM 输出结构化情绪 JSON
  fallbackClassifier: 'keyword', // 兜底用关键词正则
  confidenceThreshold: 0.6,
});
```

**模式 B：transition 引擎（SDK 核心价值）**

LLM 输出"惊讶"，但模型上一帧是"平静"——SDK 负责过渡：
- `fade`：线性淡入（500ms）
- `quick-fade`：快速（150ms）
- `snap`：瞬切
- `spring`：弹簧曲线（适合强烈情绪突变）
- `ease-out`：缓出（适合收敛）

**模式 C：JSON Schema 强制结构化输出**

```typescript
const EMOTION_SCHEMA = {
  type: 'object',
  properties: {
    emotion: { enum: ['neutral','joy','sadness','anger','surprise'] },
    intensity: { type: 'number', minimum: 0, maximum: 1 },
    transition: { enum: ['fade','quick-fade','snap','spring'] }
  },
  required: ['emotion','intensity']
};
```

LLM 通过 tool_use / function_call 强制输出这个 schema，避免正则解析失败。

### 2.3 对 oc-pet 的启发

#### 启发 5: 让 LLM 输出结构化 JSON（不只正则 tag）

三种方案，按稳健度排序：

```python
# 方案 A：text tag + 正则（简单，但脆弱）
"I'm so happy! [joy]"

# 方案 B：JSON 块 + parse（更稳健，但 prompt 复杂）
{"say": "I'm so happy!", "emotion": "joy", "intensity": 0.8}

# 方案 C：tool_call（最稳，优先用）
EMOTION_TOOL = {
    "name": "express_emotion",
    "description": "Show how you're feeling",
    "input_schema": {
        "type": "object",
        "properties": {
            "emotion": {"type": "string", "enum": ["happy","sad","surprised","thinking","neutral"]},
            "intensity": {"type": "number", "minimum": 0, "maximum": 1},
            "transition": {"type": "string", "enum": ["snap","fade","spring"]}
        },
        "required": ["emotion"]
    }
}
```

Hanako 集成的 LLM 通常支持 tool call — 优先用方案 C。

#### 启发 6: transition 引擎独立模块化

不要把 fade/spring 逻辑写在 renderer 里。建独立模块 `core/emotion_transitions.py`（完整代码见附录 A.3）。

---

## 3. Lumi_Nox — 双 AI 同台直播的状态机与共享记忆

> **资料等级**：C（仓库未命中，BV 视频无法解析）
> **推断来源**：BV1BVKj6FEde "双 AI 主播同台直播 + 踩坑分享" + 通用多 Agent 协调设计
> **标注**：基于"双主播同台"硬场景的通用模式

### 3.1 核心问题

两个 Agent 同一时刻只能有一个"在说话"，但不能死气沉沉。要解决：
- **谁先说话**（发言权仲裁）
- **什么时候切换**（状态机协调）
- **空闲时说什么**（idle chatter）
- **怎么记住刚才的事**（共享记忆）

### 3.2 关键设计

#### 设计 1：发言权仲裁（Turn Arbitration）

```python
class TurnArbitrator:
    def __init__(self):
        self.current_speaker = None
        self.speaking_until = 0
        self.cooldown = {}  # agent → 下次可发言时间

    def request_speak(self, agent: str, urgency: float = 0.5) -> bool:
        now = time.time()
        if self.cooldown.get(agent, 0) > now:
            return False
        if self.current_speaker and self.speaking_until > now and urgency < 0.8:
            return False
        self.current_speaker = agent
        self.speaking_until = now + random.uniform(3, 8)
        self.cooldown[agent] = now + 5
        return True
```

#### 设计 2：双 Agent 状态机协调

```python
STATES = ["IDLE_BOTH", "A_SPEAKING", "B_SPEAKING", "AWAITING_REACTION", "BOTH_REACTING"]
# 两个 Agent 的状态机是耦合的——不能各管各的
```

#### 设计 3：空闲填充（Idle Chatter）

```python
class IdleChatter:
    def tick(self):
        now = time.time()
        if now - self.last_idle_at > random.uniform(15, 45):
            self.arbitrator.request_speak(agent="auto", urgency=0.3)
            self.last_idle_at = now
```

#### 设计 4：共享记忆

所有 Agent 共享同一份 memory store，而非各管各的。

### 3.3 对 oc-pet 的启发

#### 启发 7: PetManager 级别的仲裁器

oc-pet 的 `config.json` 已有 `agents` 数组（yuexinmiao + phoebe），但当前是独立 PetWindow，互不通信。

```python
class PetArbitrator:
    """多 pet 同时存在时，谁响应用户的鼠标 hover？"""
    def on_user_action(self, action, context):
        candidates = self.pets_near(action.target)
        winner = max(candidates, key=lambda p: p._last_user_focus_ts)
        winner.handle_action(action)
```

#### 启发 8: 主动发言的 idle chatter

oc-pet 已有 `ProactiveScheduler`，但可以再加：

```python
class PetIdleChatter:
    """桌宠自己"想说话"——即使没人交互也偶尔嘀咕"""
    def tick(self):
        if time.time() < self._next_at:
            return
        prompt = "你正在发呆，随口说一句你此刻的感受（不超过 15 字）"
        text = self.llm.generate(prompt)
        self.pet.say(text, emotion="neutral")
```

---

## 4. Mio / Lumi 双产品架构 — `agentId?: string` 枢纽设计

> **资料等级**：A+（完整博客内容已抓取）
> **相关度**：极高（架构设计哲学层面）

### 4.1 完整架构

```
miolumi/
├── apps/
│   ├── server/              ← 一个 Hono 服务器（两个产品共用）
│   ├── mobile-mio/          ← Mio：25 个角色预设
│   └── mobile-lumi/         ← Lumi：光球伴侣
├── packages/
│   ├── core/                ← ★共享核心：记忆、媒体、模型、成本、护栏
│   ├── db/
│   ├── schema-mio/
│   └── schema-lumi/
└── presets/
```

### 4.2 核心枢纽：`agentId?: string`

`packages/core` 里每个共享模块都接受可选参数 `agentId?: string`。

| 模块 | 有 agentId（Mio） | 无 agentId（Lumi） |
|------|---------------------|---------------------|
| MemoryManager | 按角色隔离记忆 | 全局记忆流 |
| EmotionEngine | 同样计算，表达受角色影响 | 完全通用 |
| TTS 链 | 按语言路由（不按角色） | 按语言路由 |
| CostTracker | 按用户名归集 | 同一条管线 |
| Guardrails | 完全一样 | 完全一样 |

### 4.3 EmotionEngine 的设计哲学

> EmotionEngine 处理对话情绪上下文。接收用户消息、近期历史、情绪基线——**不读角色文件**。情绪处理该是通用的，不绑角色。角色情绪表达不同（可可活泼，学长克制），**底层情绪计算一样**。

- `EmotionEngine.detect_emotion(text, history, baseline) -> EmotionScore` — 不传 persona_id
- 角色差异在输出层：`PersonalityAdapter.format_emotion(emotion, persona) -> animation_id`

### 4.4 对 oc-pet 的启发

#### 启发 9: EmotionBridge 接受可选 character_id

```python
class EmotionBridge:
    def __init__(self, character_id: Optional[str] = None):
        """
        character_id=None: 单角色模式（全局情绪）
        character_id='yuexinmiao': 按角色隔离情绪历史
        """
```

#### 启发 10: 拆 EmotionEngine 和 EmotionRenderer

```python
# core/emotion_engine.py — 不绑角色，纯计算
class EmotionEngine:
    """通用情绪计算 — 不读 persona 文件"""
    def update(self, user_msg, llm_response, llm_emotion):
        # 关键词打分 + LLM 声明情绪加权 + 惯性更新
        ...

# core/emotion_renderer.py — 映射到帧/动画
class EmotionRenderer:
    """把情绪映射到具体帧切换，角色特有"""
    def __init__(self, character_id, emotion_engine):
        self.excited_anim = "waving" if character_id == "yuexinmiao" else "jumping"
```

#### 启发 11: 引入情感温度 + 惯性

```python
class EmotionDynamics:
    """温度惯性 + 历史衰减"""
    def __init__(self, inertia=0.7):
        self._temp = 0.0  # -1 (sad) ~ +1 (excited)
        self._inertia = inertia

    def push(self, delta: float):
        """新信号不是覆盖，是叠加到惯性之上"""
        self._temp = self._temp * self._inertia + delta * (1 - self._inertia)
```

解决"LLM 突然说 sad → 立即变 sad → 下句 normal 又立刻回 normal"的硬切问题。

---

## 5. 跨项目模式提炼 — 经过验证的好模式

### 5.1 配置层 + Token 注入

emotion 列表是**配置驱动的**（`emotionMap`），prompt 注入时动态生成。oc-pet 的 `pet.json` emotions 段已做到一半，prompt 注入还没接。

### 5.2 可选参数枢纽（`agentId?: string`）

共享核心层接受 `Optional[ID]`，无 ID 走全局，有 ID 走隔离。oc-pet 的 EmotionBridge、MemoryManager 都可以加。

### 5.3 emotion → 原生资产索引的解耦映射

| 项目 | 字符串 | 映射层 | 原生资产 |
|------|--------|--------|----------|
| Open-LLM-VTuber | "joy" | `emotionMap["joy"]` | L2D 索引 3 |
| oc-pet | "happy" | `pet.json.emotions` | atlas anim "waving" |

oc-pet 已有映射层，下一步是让 LLM 学会输出。

### 5.4 后置过滤管线（extract → remove）

LLM 输出原始文本 → 后置过滤分两路（情绪提取 + 干净文本）→ 各自走下游。**不能靠前置 prompt 约束 LLM 100% 合规**，后置过滤必须有。

### 5.5 情绪过期回归（reset_delay_ms）

情绪是**瞬时态**，不是持久态。几秒无新情绪则回到 baseline。oc-pet **当前缺口**：emotion 一旦 set 就永久保持。

### 5.6 触摸动作概率表（tapMotions）

同一位置点击触发不同动作，根据当前 emotion 概率切换。

### 5.7 共享基础设施 + 产品专属逻辑

能力放共享层，表达放产品层。oc-pet 的 `core/` 和 `characters/<id>/` 已符合。

### 5.8 Idle Chatter 主动发言

即使没有用户输入，系统也让 Agent "主动说点什么"保持"在场感"。

### 5.9 温度 + 惯性

情绪不是离散档位，是连续温度 + 惯性更新。`new_temp = old * inertia + signal * (1 - inertia)`。

### 5.10 流式逐句切表情

不需要等整段回复完，每句话独立提取 emotion，第一句就先切。

---

## 6. 初版分析遗漏的设计模式清单

| # | 模式 | 当前 oc-pet | 优先级 |
|---|------|--------------|--------|
| 1 | LLM token + 后置过滤 | 无 | 🔴 高 |
| 2 | 情绪过期回归（reset_delay_ms） | 无 | 🔴 高 |
| 3 | transition 引擎（fade/spring/snap） | 无 | 🟡 中 |
| 4 | 情感温度 + 惯性 | 离散档位 | 🟡 中 |
| 5 | EmotionEngine / EmotionRenderer 分离 | 全耦合 | 🟡 中 |
| 6 | tool_call 结构化输出 | 文本匹配 | 🟡 中 |
| 7 | 多 pet 仲裁 | 各独立 | 🟢 低 / 🔴 长期 |
| 8 | idle chatter 主动发言 | ProactiveScheduler 简单版 | 🟢 低 |
| 9 | 共享 + 隔离双模式 memory | 各独立 | 🟢 低 |
| 10 | 触摸概率动作 | 静态菜单 | 🟢 低 |
| 11 | prompt 动态 emotion list 注入 | 无 | 🟡 中 |
| 12 | 流式逐句切表情 | 无 | 🟡 中 |

---

## 7. 实现路线

### 最小可行版本（1-2 天）

三件事：

```python
# 1. 新建 core/emotion_bridge.py
class EmotionBridge:
    def extract(self, text) -> tuple[str, list[str]]: ...

# 2. ConversationEngine 拿到 LLM 回复后调用
clean_text, emotions = emotion_bridge.extract(llm_reply)
self.bubble.set_text(clean_text)
for emo in emotions:
    renderer.play_anim(emo_map[emo])

# 3. system prompt 拼接时注入 emotion list
emo_str = ", ".join(f"[{e}]" for e in atlas.emotions.keys())
persona += f"\n可表达情绪：{emo_str}\n例如：'你回来啦！[happy]'"
```

### 中期版本（1 周）

1. EmotionStateMachine（带 reset_delay_ms）
2. TransitionEngine（fade/spring）
3. EmotionEngine（温度+惯性）
4. tool_call 结构化输出

### 完整版本（2-3 周）

1. PetArbitrator（多 pet 协调）
2. PetIdleChatter（主动发言）
3. PetMemoryAdapter（共享+隔离）
4. RelationshipStage（可选）

---

## 附录 A：直接可用的代码片段

### A.1 EmotionBridge 完整实现（最小版）

```python
"""core/emotion_bridge.py — LLM 输出 → (干净文本, 情绪序列)"""
import re
from typing import List, Tuple, Optional

EMOTION_TAG = re.compile(r"\[(\w+)\]")

class EmotionBridge:
    def __init__(self, character_id: Optional[str] = None):
        self.character_id = character_id
        self.emo_map = self._load_emo_map(character_id)

    def _load_emo_map(self, character_id: Optional[str]) -> dict:
        from characters import load_pet_meta
        if character_id:
            meta = load_pet_meta(character_id)
        else:
            meta = load_pet_meta("yuexinmiao")  # 默认
        out = {}
        for e, cfg in (meta.get("emotions") or {}).items():
            if isinstance(cfg, dict):
                out[e.lower()] = cfg.get("anim", e)
            else:
                out[e.lower()] = cfg
        return out

    def extract(self, text: str) -> Tuple[str, List[str]]:
        """返回 (clean_text, [emotions])"""
        if not text:
            return text, []
        emotions = []
        for m in EMOTION_TAG.finditer(text):
            tag = m.group(1).lower()
            if tag in self.emo_map and tag not in emotions:
                emotions.append(tag)
        clean = EMOTION_TAG.sub("", text).strip()
        return clean, emotions

    def emo_str_for_prompt(self) -> str:
        """给 LLM 的 system prompt 用"""
        return ", ".join(f"[{e}]" for e in self.emo_map.keys())
```

### A.2 EmotionStateMachine（reset_delay_ms）

```python
"""core/emotion_state_machine.py"""
import time
from typing import Callable

class EmotionStateMachine:
    """情绪状态机 — 处理情绪切换、过期回归默认"""

    def __init__(
        self,
        on_emotion_change: Callable[[str, float], None],
        default_emotion: str = "neutral",
        reset_delay_ms: int = 3000,
    ):
        self._cb = on_emotion_change
        self._default = default_emotion
        self._reset_ms = reset_delay_ms
        self._current = default_emotion
        self._intensity = 1.0
        self._last_set_ts = 0.0

    def set(self, emotion: str, intensity: float = 1.0):
        if emotion == self._current and abs(intensity - self._intensity) < 0.05:
            return
        self._current = emotion
        self._intensity = intensity
        self._last_set_ts = time.time()
        self._cb(emotion, intensity)

    def tick(self):
        """每 100ms 由 Qt 定时器调用"""
        if self._current == self._default:
            return
        elapsed_ms = (time.time() - self._last_set_ts) * 1000
        if elapsed_ms > self._reset_ms:
            self._intensity *= 0.85
            self._cb(self._current, self._intensity)
            if self._intensity < 0.05:
                self._current = self._default
                self._intensity = 1.0
                self._cb(self._default, 1.0)

    @property
    def current(self) -> str:
        return self._current
```

### A.3 TransitionEngine（fade/spring/snap）

```python
"""core/transition_engine.py"""
import math
import time
from typing import Callable, Literal

TransitionStyle = Literal["snap", "fade", "spring"]

class TransitionEngine:
    def __init__(self, on_update: Callable[[float], None]):
        self._cb = on_update
        self._from = 0.0
        self._to = 0.0
        self._t0 = 0.0
        self._duration_ms = 0
        self._style: TransitionStyle = "snap"
        self._active = False

    def go(self, target: float, style: TransitionStyle = "fade"):
        self._from = self._current_intensity()
        self._to = target
        self._t0 = time.time() * 1000
        self._style = style
        self._duration_ms = {"snap": 0, "fade": 300, "spring": 500}[style]
        self._active = True

    def _current_intensity(self) -> float:
        if not self._active:
            return self._to
        elapsed = time.time() * 1000 - self._t0
        if self._duration_ms == 0:
            return self._to
        if elapsed >= self._duration_ms:
            self._active = False
            return self._to
        ratio = elapsed / self._duration_ms
        if self._style == "spring":
            ratio = 1 - math.exp(-3 * ratio) * math.cos(ratio * math.pi * 2)
        else:
            ratio = 1 - (1 - ratio) ** 2
        return self._from + (self._to - self._from) * ratio

    def tick(self):
        if self._active:
            self._cb(self._current_intensity())
```

---

## 附录 B：参考资源链接

| 项目 | 链接 | 置信度 |
|------|------|--------|
| Open-LLM-VTuber | github.com/Open-LLM-VTuber/Open-LLM-VTuber | A+ |
| Open-LLM-VTuber DeepWiki | deepwiki.com/Open-LLM-VTuber/.../10.2-emotion-mapping | A+ |
| Mio/Lumi 博客 | blog.ax0x.ai/rethinking-mio-dual-product-zh | A+ |
| Soullink Emotion SDK | github.com/nanlingyin/soullink-emotion-sdk | C（未访问到） |
| Lumi_Nox | github.com/MIO-456/Lumi_Nox | C（未访问到） |

---

## 附录 C：信息源限制声明

**Soullink Emotion SDK** 和 **Lumi_Nox** 的分析基于通用架构模式推断（GitHub 访问被沙箱拦截，B站视频无法解析画面）。第 1 节（Open-LLM-VTuber）和第 4 节（Mio/Lumi）是高置信度的，第 2、3 节标 C 等级。如需校准请直接访问仓库。

---

**作者**：洛琪希
**审查**：奥菲莉娅
**最后修订**：2026-07-19
