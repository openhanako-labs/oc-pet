# 现状审计 (2026-07-08)

## 正常工作的功能

- ✅ 帧精灵动画（idle/walk/extra，瞳孔跟踪）
- ✅ LLM 对话（agnes-2.0-flash，读取 Hanako 身份文件）
- ✅ TTS 语音输出（CosyVoice2 零样本克隆，7-8s/句）
- ✅ 语音输入（Whisper base ASR，push-to-talk）
- ✅ 情绪检测（LLM 回复带 [emotion:xxx] 标签）
- ✅ 跨线程 UI 更新（pyqtSignal 信号槽）
- ✅ 久坐提醒（三段递进）
- ✅ 主动对话（规则引擎 + 2 分钟启动保护）
- ✅ 屏幕感知（2 分钟截屏 + agnes 视觉分析）
- ✅ 记忆系统（JSONL 持久化 + 50 条压缩）
- ✅ 配置面板（TTS/行为/主动对话/屏幕感知 GUI）
- ✅ 插件面板（浏览 23 个 Hanako 插件 + 快捷发送）
- ✅ 行为模式（静默/正常/活跃/黏人）
- ✅ 右键菜单（对话/说话/行为/缩放/角色/穿透/设置/插件）
- ✅ 系统托盘

## 已知限制

- 情绪帧只有 4 张 extra（surprised/thinking 映射越界，回退到 idle）
- agnes API 偶尔返回空 content（已处理，显示兜底文本）
- agnes API 响应慢（10-30s），TTS 再加 7-8s，总延迟可能 20-40s
- wetext 模型每次启动打印 "Downloading"（实际是缓存验证，非真正下载）
- 调度感知读 automation*.json，文件不存在时为空
- Avatar 抽象层未完全委托（pet.py 仍直接访问 char_label/eye_overlay）

## 清理记录

- 移除 companion_bridge.py（备选独立进程，不再需要）
- 移除 build_spritesheet.py（一次性构建脚本）
- 移除 test_*.py（5 个测试文件，已过时）
- 移除 COMPLETION-AUDIT.md、ROADMAP-v2.md（历史文档）
- 移除 oc-pet.spec（打包配置，未使用）
- 移除 hanako-state.json、output/（运行时产物）
