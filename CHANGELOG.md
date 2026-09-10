# Changelog

所有重要变更都会记录在此文件中。

## [未发布] - 2026-09-10

### 移除（零引用模块）

以下模块在全部 388 个 commit 中**从未有过调用方**（`git log -S` 核实），且绝大多数本身即为临时产物或架构错配。详见 `docs/review-2026-09-10/`。

```
core/plugin_kv.py                  # 插件级 KV 存储 —— 架构错配：插件走 Node subprocess，无法消费 Python 侧 KV
tts_provider/register_speakers.py  # 一次性运维脚本，与 quick_register 重复
tts_provider/quick_register.py     # 同上，且硬编码本机绝对路径
test_auto_supplement.py            # 假测试：抄生产逻辑副本测副本，生产改动不会使其失败
```

### 接线（已实现 → 已生效）

```
core/backup_service.py    # 366 行 → 补 __main__ 入口 + 设置面板按钮
core/usage_memory.py      # 215 行 → 接气泡 dismiss 事件 + 主动对话查询
core/memory_filter.py     # 125 行 → 接记忆注入点（事实类过滤 + 引用日期明示）
core/startup_check.py     # 242 行 → 接 launcher 就绪后自检
```

### 修复（静默异常，四条高危路径）

将 INFO 级别下不可见的 `log.debug` 提为 `warning` 或显式降级状态，覆盖 TTS / Live2D / 感知 / 音频。验收口径：**不是「有没有日志」，而是「INFO 下看不看得见 + 失败是否变成可查询状态」**。

### 新增

```
docs/review-2026-09-10/    # 评审产物（本目录按内部文档规则不入库，此处仅作索引）
```

### 依赖

- `requirements.txt`：移除 `portalocker`（全仓零引用，死声明）

---

## [0.9.0] - 2026-09-06

### 新增功能（17 项）

#### P1 短期任务
- **SMTC 媒体感知** — 读取正在播放的媒体信息（歌曲名、艺术家、专辑）
- **动作槽位自动映射** — motion 文件名自动映射到语义槽位
- **子服务健康四态** — 服务状态可视化（enabled/running/ready/last_error）
- **感知走 AI 发挥** — 移除模板池兜底，交给 AI 自由发挥

#### P2 中期任务
- **每帧管线化** — Live2D 渲染管线化
- **干掉 `_IGNORED_EXPRESSIONS` 硬编码** — 表情处理解耦
- **exp3 三层优先 + Blend** — 表情混合计算
- **端口收敛成 BodyAPI** — 统一外部接口
- **四级依赖阶梯 + 静默异常治理** — 异常处理规范化
- **屏幕观察进程化** — 独立进程避免卡 UI
- **Token/费用对账与预算边界** — 使用量统计 + 预算限制
- **记忆写入性能优化** — 批量 flush + 异步落盘
- **插件级 UI 与 KV 存储** — 插件自带配置页 + 持久存储

#### P3 战略任务
- **主动能力可解释面板** — 统一展示开关 + 运行状态 + 费用边界
- **备份恢复 + 数据迁移** — 完整备份 + SHA-256 校验 + 一键恢复
- **记忆自动维护代理** — 空闲时自动归纳/去重/修剪/重要性衰减
- **免装 Python 的运行时分发** — 嵌入式 Python 引导 + 双击即用

### Bug 修复
- **气泡无文字** — fade_alpha 强制 1.0
- **LLM 回复不走 Hana 通道** — 优先走 Hana 通道
- **TTS 读出标签** — parse_emotion 支持等号变体

### 安全修复
- **phone_receiver 鉴权** — auth_token 未配置时自动生成随机 token

### 文档更新
- **README.md** — 更新功能清单
- **WHY.md** — 叙事文档
- **SETUP-FOR-AI.md** — 设置指南
- **MODEL-ADAPTATION.md** — 模型适配文档
- **FAQ.md** — 常见问题

### 新增文件
```
core/perception/media.py                    # SMTC 媒体感知
core/perception/screen_observer_process.py  # 屏幕观察进程化
core/motion_slot.py                         # 动作槽位自动映射
core/service_health.py                      # 子服务健康四态
core/usage_tracker.py                       # Token/费用统计
core/memory_write_buffer.py                 # 记忆写入优化
core/plugin_kv.py                           # 插件级 KV 存储  ← 已于 2026-09-10 移除（架构错配，插件走 Node）
core/autonomy_panel.py                      # 主动能力面板
core/backup_service.py                      # 备份恢复服务  ← 已于 2026-09-10 接线生效
core/memory_maintenance.py                  # 记忆自动维护  ← 已于 2026-09-09 移除（零引用死代码）
scripts/embedded_python_bootstrap.py        # 嵌入式 Python 引导
setup-runtime.bat                           # 双击启动脚本
version.py                                  # 版本信息
```

### 修改文件
```
core/perception/proactive_generation.py     # 感知走 AI 发挥
core/perception/controller.py               # 集成新模块
core/harness_adapter.py                     # LLM 回复走 Hana 通道
core/companion_memory.py                    # 记忆写入优化
core/phone_receiver.py                      # 鉴权修复
ui/bubble.py                                # 气泡修复
pet.py                                      # 集成新模块
config.py                                   # 配置更新
requirements.txt                            # 依赖更新
README.md                                   # 文档更新
```

### Git 统计
- **37 commits** ahead of origin/master
- **435/435 tests** passing

---

## [0.8.0] - 2026-09-04

### 新增功能
- 基础对话系统
- Live2D 渲染
- 多桌宠并行
- Hanako 集成

### Bug 修复
- 初始版本修复

---

## 下一步计划

### 待验证
- **T3-1**：clone Amadeus 确认是否有感知系统
- **T3-2**：评估本地语音栈（Genie ONNX + faster-whisper）

### 未来功能
- 外部大脑接入模式
- 本地语音栈集成
- 更多插件支持

---

## 贡献

感谢所有贡献者！