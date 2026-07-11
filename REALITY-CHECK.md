# 现状审计 (2026-07-11 v3)

## 正常工作的功能

### 核心对话
- ✅ LLM 对话（agnes-2.0-flash，读取 Hanako 身份+记忆，动态记忆预算 6000 字符）
- ✅ TTS 语音输出（CosyVoice2 本地 / MIMO TTS / API TTS，设置面板切换）
- ✅ ASR 语音输入（Whisper 本地 / MIMO ASR / API ASR）
- ✅ 情绪检测（LLM 回复带 [emotion:xxx] 标签）
- ✅ 跨线程 UI 更新（pyqtSignal 信号槽）

### 感知系统
- ✅ 屏幕感知（定时截屏 + agnes 视觉分析，变化检测 + 失败退避）
- ✅ 时间感知（时段/工作日/周末）
- ✅ 情绪状态机（触发 + 强度衰减）
- ✅ 日程感知（读取 Hanako 自动化任务）
- ✅ 统一 tick()（情绪衰减 + 主动对话 + 日程刷新，每 30 秒）
- ✅ 主动对话（规则引擎 + 空闲检测 + 前台窗口分类）

### 交互
- ✅ 鼠标交互（视线跟随/靠近反应/悬停/追逐/惊吓，5 秒冷却防刷屏）
- ✅ 帧精灵动画（idle/walk/extra，呼吸浮动 + 视线偏移叠加）
- ✅ 拖拽 + 弹跳物理
- ✅ 右键菜单（穿透/设置/插件/行为模式/退出）
- ✅ 插件面板（浏览 Hanako 插件 + 快捷发送）
- ✅ 对话气泡

### 多桌宠
- ✅ PetManager（扫描 ~/.hanako/agents/ 发现 agent）
- ✅ 多窗口并行（每个 agent 独立 PetWindow）
- ✅ 设置面板 Agent 管理（添加/移除/启用/禁用）
- ✅ 精灵来源优先级（agent/pet/ > characters/）

### 设置面板
- ✅ 三 tab 布局（基础 / 功能 / API）
- ✅ Agent 管理面板
- ✅ 行为模式 / 透明度 / 缩放 / 鼠标交互开关
- ✅ TTS/ASR/LLM 引擎选择 + Provider 下拉自动填充
- ✅ Model 下拉（从 provider-catalog.json 读取）
- ✅ TTS 音色下拉（MIMO + OpenAI 音色）
- ✅ 记忆注入配置（自动/手动）
- ✅ 久坐提醒 / 屏幕感知 / 主动对话配置

## 已删除的功能

- ❌ EyeOverlay（3px 瞳孔跟踪）→ 替换为精灵偏移视线跟随
- ❌ characters/yuexiye（遗留角色）
- ❌ 旧的单 character 模式 → 替换为多 agent 架构

## 已知限制

- 情绪帧只有 4 张 extra（surprised/thinking 映射越界，回退到 idle）
- agnes API 响应慢（10-30s），TTS 再加数秒，总延迟 15-40s
- Avatar 抽象层未完全委托（pet.py 仍直接操作 char_label 做拖拽/位置追踪）
- wetext 模型每次启动打印 "Downloading"（实际是缓存验证，非真正下载）
- MIMO TTS 非流式，长文本合成有明显等待
