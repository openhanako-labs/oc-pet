# 参考项目研究

> 2026-07-19 整理，供桌宠项目（oc-pet / 月薪喵）参考

---

## 1. Soullink Emotion SDK

**仓库**: github.com/nanlingyin/soullink-emotion-sdk
**作者**: 骥南凌音（SHZU 软件工程大三，B站 UID 349707254）
**视频**: [技术演示](https://www.bilibili.com/video/BV1MXKi6NEbR/) (04:38)
**安装**: `npm install @soullink-emotion/sdk`
**合集**: SoulLink_Live2D开发（3P）

### 核心思路

用 LLM API 驱动 Live2D 模型的表情和动作，让 AI 桌宠的 L2D 皮套不再靠硬编码状态机切换表情，而是通过语义理解自动选择合适的表情/动作。

### 技术要点

- **LLM → 情绪/动作映射**: LLM 输出结构化的情绪标签，SDK 翻译为 L2D 参数控制
- **自然连贯**: 不是突然切换表情，而是有过渡和衔接
- **通用性**: 适配多种 L2D 模型（演示中出现了 LilyaBEE、shizuku、hiyori、blondegirl 等）
- **模型热切换**: 支持运行时切换 L2D 模型

### 对 oc-pet 的启发

| 维度 | Soullink | oc-pet 现状 | 可借鉴 |
|------|----------|-------------|--------|
| 表情驱动 | LLM 语义 → L2D 参数 | 状态机（idle/walk/extra） | 可接入 LLM 情绪输出，映射到 atlas 行号 |
| 动作选择 | LLM 输出动作标签 | BehaviorParams 概率驱动 | 两种模式可并存：概率巡逻 + LLM 触发动作 |
| 渲染方案 | Live2D (Web) | PySide6 + 帧精灵 | 未来可考虑 L2D，但当前 atlas 够用 |
| 情绪持续 | 有过渡动画 | 无（瞬间切换） | 需要加 fade/blend 机制 |

### 可落地的改进

1. **LLM 情绪桥接**: 对话回复时提取情绪标签（happy/thinking/surprised 等），映射到 `pet.json` 的 emotions 配置
2. **表情过渡**: 在 `_set_anim_seq` 中加 blend 时间，不要瞬间跳切
3. **atlas 动作扩展**: 现有 9 种动画够用，但可以加 emotion-driven 的随机 idle 变体

---

## 2. Lumi_Nox（双 AI 搭档直播系统）

**仓库**: github.com/MIO-456/Lumi_Nox
**视频**: [两个 AI 一起直播两个月](https://www.bilibili.com/video/BV1BVKj6FEde/) (7.8万播放)
**作者**: Lumi和Nox-AI搭档（B站 UID 544387533）

### 核心问题

把一个 AI 主播变成两个，不是简单再接一个模型。要解决：
- 谁先说话（发言权仲裁）
- 什么时候切换游戏（状态机协调）
- 游戏加载时说什么（空闲填充）
- 打完之后怎么记住刚才发生的事（共享记忆）

### 技术要点

- **双 Agent 协调**: Lumi 和 Nox 各自独立的 LLM 实例，通过中间层协调发言
- **弹幕实时互动**: 接入 B 站直播弹幕，实时响应
- **游戏接入**: 能玩游戏并与观众互动
- **长期记忆**: 记住直播中发生的事件
- **人设保留**: Lumi/Nox 的人设部分未开源

### 对 oc-pet 的启发

| 维度 | Lumi_Nox | oc-pet | 可借鉴 |
|------|----------|--------|--------|
| 多 Agent 协调 | 双主播发言权仲裁 | 单 Agent | 未来多桌宠共存时参考 |
| 直播互动 | B站弹幕 → 响应 | Hanako 消息 → 响应 | 消息优先级队列设计 |
| 记忆 | 事件记忆 | 记忆系统已有 | 无需改动 |
| 游戏状态机 | 游戏加载/进行/结束 | 无 | 不直接相关 |

### 可落地的改进

1. **消息优先级队列**: 借鉴发言权仲裁，给不同类型的消息（用户主动、系统事件、定时巡检）排优先级
2. **空闲填充**: 游戏加载或等待时的自言自语机制（桌宠的 idle 时可以有"内心独白"）

---

## 3. Mio / Lumi（双产品 Monorepo 架构）

**博客**: [一个代码库藏着两个产品](https://blog.ax0x.ai/rethinking-mio-dual-product-zh)
**作者**: Xingfan Xia (ax0x.ai)

### 架构设计

同一个 monorepo 里跑两个情感 AI 产品：

```
miolumi/
├── apps/
│   ├── server/          # Hono 服务器（共用）
│   ├── mobile-mio/      # Mio：多角色社交（25个角色）
│   └── mobile-lumi/     # Lumi：单伴侣（光球）
├── packages/
│   ├── core/            # 共享核心：记忆、情绪、语音、成本、安全
│   ├── db/
│   ├── schema-mio/
│   └── schema-lumi/
└── presets/             # 角色配置
```

### 关键设计模式：agentId

```typescript
// 所有共享模块接受可选的 agentId
function processMessage(msg, agentId?: string) {
  // Mio: agentId = 当前角色 → 按角色隔离记忆
  // Lumi: 不传 → 全局记忆流
}
```

一个可选参数区分两种产品模式，零重复代码。

### 共享 vs 差异

**共享（packages/core）**：记忆管线、情绪引擎、语音链、成本追踪、安全护栏
**差异（apps 层）**：角色加载、UI 渲染、onboarding 流程

### 对 oc-pet 的启发

这个架构对 oc-pet 最大的启发不是"做两个产品"，而是**共享核心与产品层的分离**：

| Mio/Lumi 模式 | oc-pet 对应 |
|---------------|-------------|
| 记忆管线（通用） | 已有 Hanako 记忆 |
| 情绪引擎（通用） | 可抽象为 EmotionEngine |
| 语音链（按语言路由） | TTS provider 已模块化 |
| 角色加载（产品层） | characters/ + pet.json |
| UI 渲染（产品层） | SpriteRenderer |

**情绪引擎的哲学**：情绪处理不绑定角色。角色的情绪表达不同（月薪喵活泼 vs 其他角色），但底层情绪计算一样。这个思路可以指导 oc-pet 的情绪系统设计。

---

## 4. 生态全景（补充参考）

从搜索中发现的其他相关项目，按相关度排序：

### Open-LLM-VTuber
- **Stars**: 12.6k | **仓库**: github.com/Open-LLM-VTuber/Open-LLM-VTuber
- 完整的语音交互 AI 伴侣框架，支持 Live2D、语音对话、视觉感知
- 桌宠模式：透明背景全局置顶+鼠标穿透
- 情绪映射：`[emotion]` 标签触发表情切换
- 模型配置：`model_dict.json` + `emotionMap` 映射

### Live2DPet (Electron)
- Electron 桌面宠物 + Live2D + VOICEVOX TTS
- AI 视觉感知：定时截屏 + 活动窗口检测
- 情绪累积系统：AI 驱动表情/动作选择
- 模型热导入：任意 L2D 模型自动参数映射

### AI Anchor
- 完全本地部署：LMDeploy + GPT-SoVITS + Live2D
- 情绪驱动 TTS 和 L2D 动作联动
- RTX 4070Ti 上约 1 秒首段语音

### Eridanus
- Live2D 桌宠 + QQ 机器人后端共享
- 表情控制：用 gpt-4o-mini 快速判断情绪
- 桌宠聊天记录与 QQ 同步

---

## 5. 对 oc-pet 的优先改进路线

基于以上研究，按投入产出排序：

### P0：LLM 情绪桥接（低投入，高体验提升）
- 对话回复时提取情绪标签
- 映射到 pet.json 的 emotions 配置
- 参考 Soullink SDK 的映射思路

### P1：表情过渡动画（中投入，视觉提升）
- 动画切换时加 crossfade
- 避免瞬间跳切
- 参考 Open-LLM-VTuber 的 `reset_delay_ms` 机制

### P2：消息优先级队列（中投入，交互提升）
- 用户消息 > 系统事件 > 定时巡检
- 参考 Lumi_Nox 的发言权仲裁

### P3：情绪引擎抽象（高投入，架构提升）
- 抽取 EmotionEngine 为独立模块
- 不绑定角色，支持多角色复用
- 参考 Mio/Lumi 的 `agentId` 模式

### 远期：Live2D 渲染层
- 当前 atlas 帧精灵够用
- 如果未来需要更丰富的表情，考虑 L2D
- Soullink SDK 可直接集成

---

## 参考链接

| 项目 | 链接 | 类型 |
|------|------|------|
| Soullink Emotion SDK | github.com/nanlingyin/soullink-emotion-sdk | 表情控制 |
| Soullink 视频 | BV1MXKi6NEbR | 技术演示 |
| Lumi_Nox | github.com/MIO-456/Lumi_Nox | 双AI直播 |
| Lumi_Nox 视频 | BV1BVKj6FEde | 踩坑分享 |
| Mio/Lumi 博客 | blog.ax0x.ai/rethinking-mio-dual-product-zh | 架构设计 |
| Open-LLM-VTuber | github.com/Open-LLM-VTuber/Open-LLM-VTuber | AI伴侣框架 |
| Live2DPet | github.com/IAN225/Live2DPet | Electron桌宠 |
| AI Anchor | github.com/icjztdhop/AI-Anchor | 本地AI主播 |
| Eridanus | eridanus.netlify.app | Live2D+QQ |
