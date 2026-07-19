# VPet-Simulator 架构深度分析

> **作者**：洛琪希
> **目标项目**：oc-pet
> **对照对象**：LorisYounger/VPet（v1.10.x，6.5k stars）
> **生成时间**：2026-07-19

---

## 0. 关键澄清

1. **VPet 的"4 种状态"不是"正常/开心/生病/睡觉"**。
   - 实际枚举：`Happy / Nomal / PoorCondition / Ill`（健康/情绪综合状态）。
   - "睡觉"是动画类型（`GraphType.Sleep`），不是健康状态。
   - 这是两个正交维度。

2. **oc-pet 的现状比任务背景描述的更完整**。
   - 已有：EmotionStateMachine、TransitionEngine、IdleChatter、5 种鼠标反应、角色包。
   - 真正缺的是"养成域"：健康状态、物品、工作、经济、存档、插件协议。

---

## 1. VPet 核心系统

### 交互系统（按身体区域触发）

- `TouchArea(rect, on_press, on_long_press)` 数据驱动、可热挂
- 「点头/抬头次数」鲁棒算法区分摸头 vs 摸身体（比坐标更可靠）
- oc-pet 差距：当前是 eventFilter 一锅烩，缺 body 分区

### 状态系统（健康 + 动画类型正交）

- `ModeType { Happy, Nomal, PoorCondition, Ill }` — 由属性阈值自动计算
- `GraphType` — 正交维度（Touch_Head/Touch_Body/Sleep/Say/...）
- **挂起池**（StoreXxx）— 一次性给很多数值拆成多个 tick 慢慢回，避免"一口吃撑"
- oc-pet 差距：只有 EmotionStateMachine（情绪），没有健康/养成状态

### 物品系统（IFood 7 字段）

- `IFood`：Exp/Strength/StrengthFood/StrengthDrink/Feeling/Health/Likability
- `FoodType`：Food/Star/Meal/Snack/Drink/Functional/Drug/Gift
- `RealPrice` + `IsOverLoad` — 防超模平衡检测
- `FoodAnimation` — 3 层夹心特效（前/中/后）
- oc-pet 差距：完全缺位

### 工作/经济系统

- `Work(WorkType, MoneyBase, FinishBonus, LevelLimit, Time, Graph)`
- 三段公式：Money收益 = (MoneyBase * (1+FinishBonus/2) + 1)^1.25
- 效率由饱腹/口渴/心情决定（饿了收益打折）
- 倍率系统：每级解锁 1 档，共 5 档
- oc-pet 差距：完全缺位

### 插件系统

- `IMainWindow`（297 行接口）— 插件唯一能拿到的对象
- `MainPlugin` 抽象基类：LoadPlugin/Save/Setting/DIY 钩子
- **事件订阅**是核心协议：Event_WorkEnd/Event_TakeItem/Event_NewDay
- `CoreMOD` — 非代码 MOD 加载（food/pet/text/theme/lang/plugin 子目录约定）
- oc-pet 差距：只有 Node.js subprocess 工具调用，没有行为扩展协议

### 存档系统

- `IGameSave` 抽象：数据怎么存和怎么变分开
- 自动保存 + 数据迁移（BackUP → Saves）
- 多开支持（PrefixSave 前缀）
- oc-pet 差距：只有 config.json，没有养成数据存档

---

## 2. oc-pet vs VPet 差距总览

| 维度 | VPet | oc-pet | 差距 | 优先级 |
|------|------|--------|------|--------|
| 健康/养成状态 | IGameSave + 4 ModeType | 无 | 高 | **P0** |
| 物品/食物 | IFood 7 字段 + 防超模 | 无 | 高 | **P0** |
| 工作/经济 | Work + WorkTimer + 三段公式 | 无 | 高 | **P0** |
| 存档 | LPS + 自动保存 + 迁移 | 仅 config.json | 高 | **P0** |
| 身体分区触摸 | TouchArea + wave 计数 | eventFilter 一锅烩 | 高 | P1 |
| 动画二维索引 | mode × GraphType | emotion 一维 | 中 | P1 |
| 插件协议 | IMainWindow + 事件订阅 | Node.js subprocess | 中 | P1 |
| MOD 加载 | CoreMOD + 子目录约定 | zip 角色包 | 中 | P2 |
| 过渡动画 | 内置 | 已实现 | **持平** | — |
| 鼠标反应 | 5+ 种 | 5 种 | **持平** | — |
| 情绪状态机 | 离散 4 状态 | 5 情绪+衰减+过期 | **持平** | — |
| idle chatter | 内置 SayRnd | 已实现 | **持平** | — |
| 物理 | MoveTimer | physics.py | **持平** | — |

---

## 3. oc-pet 的差异化优势

VPet 没有的：
- **LLM 对话能力**（Hanako WS 完整客户端）
- **情绪 token 驱动动画**（[emotion:xxx] → 自动切表情）
- **表情过渡动画**（snap/fade/spring）
- **工具调用**（搜索/生图/RSS/浏览器）
- **真实工作流游戏化**（每完成 1 次工具调用 = 1 笔虚拟收入 + 经验）

---

## 4. P0 路线图（1-2 周，核心养成闭环）

1. `core/save/pet_save.py` — PetSave Pydantic 模型 + JSON 持久化
2. `core/items/item.py` — Item/Food(IFood) + validate_balance()
3. `core/pet_state.py` — Health/Stamina/Hunger/Thirst/Mood/Likability + 挂起池 + cal_mode()
4. `core/work/work.py` — Work/WorkTimer + 三段公式 + 异步结算
5. 接通 ConversationEngine — 工作完成喂给 LLM

**验收**：投喂→属性变化→触发工作→完成→金钱+经验+心情 完整回路

---

## 5. 信息源

- VPet GitHub 源码（main 分支 2026-05~11）
- 3 楼猫 v1.10 MOD 升级教程
- Steam 社区《Working and Learning parameters》指南
- VPet Fandom Wiki 交互表
- PR #541 多屏 active screen
