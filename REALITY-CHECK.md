# 现状审计 (2026-07-08 v2)

## 正常工作的功能

- ✅ 帧精灵动画（idle/walk/extra，瞳孔跟踪）
- ✅ pet.json spritesheet 模式 + 分帧文件夹模式（自动回退）
- ✅ LLM 对话（agnes-2.0-flash，读取 Hanako 身份+记忆文件）
- ✅ TTS 语音输出（CosyVoice2 零样本克隆）
- ✅ 语音输入（Whisper ASR，push-to-talk）
- ✅ 情绪检测（LLM 回复带 [emotion:xxx] 标签）
- ✅ 跨线程 UI 更新（pyqtSignal 信号槽）
- ✅ 主动对话（规则引擎 + 2 分钟启动保护）
- ✅ 屏幕感知（2 分钟截屏 + agnes 视觉分析）
- ✅ 统一感知系统（时间/情绪/日程/屏幕/主动对话）
- ✅ 配置面板（TTS/行为/主动对话/屏幕感知）
- ✅ 插件面板（浏览 23 个 Hanako 插件 + 快捷发送）
- ✅ 行为模式（静默/正常/活跃/黏人）
- ✅ 右键菜单完整

## 已删除的功能

- ❌ 久坐提醒（break_notifier）-- 被 proactive 主动对话取代
- ❌ 桌宠自有记忆系统（memory_store + memory_compressor）-- 用 Hanako agent 记忆
- ❌ companion_bridge（备选独立进程）-- 已合并为单进程
- ❌ WebSocket 通信（ws_server/ws_client）-- 已用信号槽替代

## 已知限制

- 情绪帧只有 4 张 extra（surprised/thinking 映射越界，回退到 idle）
- agnes API 偶尔返回空 content（已处理，显示兜底文本）
- agnes API 响应慢（10-30s），TTS 再加 7-8s，总延迟可能 20-40s
- wetext 模型每次启动打印 "Downloading"（实际是缓存验证，非真正下载）
- Avatar 抽象层未完全委托（pet.py 仍直接访问 char_label/eye_overlay）
