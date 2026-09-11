# Changelog

所有重要变更都会记录在此文件中。

## [未发布] - 2026-09-11

### 新增：窗口贴合扫描缓存（启动提速）

`_fit_window_to_model` 用 `HitDrawable` 网格扫描测角色 bbox，实测**每次启动 21.8s**。
但输入完全确定（模型文件 + 缩放系数 + 视口尺寸），而桌宠每次启动的视口都一样
（`config.window` 不随贴合写回），所以除首次外每次都在重算同一个结果。

新增 `avatar/fit_cache.py`：缓存**扫描结果 bbox**（不是最终窗口尺寸——
边距/宽高比那些是纯计算，每次重跑，这样将来调边距常量时缓存自动跟着生效）。

```
首次启动：27s（扫描 22.5s → 写缓存）
之后启动：5s （命中缓存，跳过扫描）
```

坏值一律当「没缓存」：越界 / 退化 / 反向 / 非数字 / 文件损坏 → 重扫，
绝不拿坏 bbox 去贴合窗口。读不到、写不进都只记日志，不影响启动。

### 修复：窗口尺寸与缩放成平方关系

`fit_window_to_model` 收到的 w/h 是**当前缩放视口里**量的像素，而 `_base_*` 是
「未缩放基准」（窗口 = 基准 × scale）。旧代码把测量值直接当基准 → **缩放被乘两次**：

```
scale=1.0 → 窗口 451x833    （看不出来，所以一直没被发现）
scale=1.8 → 窗口 1458x2698  （屏幕 2048x1152，窗口高出 2.3 倍）
```

改为 基准 = 测量值 / scale，窗口 = 测量值 = 模型实际大小（与 docstring 一致），
且尺寸与 scale 恢复线性。

### 修复：贴合后把自己挤出屏幕

两处 `setFixedSize` 都用「保持窗口中心」定位，窗口变高时会向上顶出屏幕。
实测 scale=1.8 时窗口从 833 长到 1499，上移 333px，Win32 报回的真实矩形是
`(1470,-325)-(2483,1549)` —— 桌宠顶部被推出了屏幕。

- 新增 `_resize_keeping_visible`：水平保持中心，**垂直保持底边**（桌宠是「站在
  桌面上」的，脚踩同一条线），并把窗口钳制进屏幕。fit 与滚轮缩放两条路共用。
- 新增 `_clamp_size_to_screen`：窗口超过屏幕可用区时**等比压缩**（不裁切，
  因为 `SetScale` 是相对窗口的，窗口多大角色就多大；裁切会丢脚，非等比会拉变形）。

```
修前：rect=(1470,-325)-(2483,1549)   头顶冲出屏幕
修后：rect=(1714,454)-(2220,1390)    506x936，全身可见
```

### 新增：桌宠起不来时的可见提示

`launch_all` 的早退分支（没有角色目录 / 没有启用的 agent / 启用的全缺资源）
此前只写日志 —— 而**没有窗口就没有托盘**，用户看到的是「点了启动，什么都没发生」。

- 新增 `PetManager.notify_user()`：优先借已有窗口的托盘弹气泡；一个窗口都没有时
  **自建托盘图标**（自绘占位图，不依赖素材），并给「忽略提示」菜单项。
- 覆盖四条路径：无角色目录 / 无启用 agent / 全部被跳过 / 首个窗口创建失败。
- 只在整个都没起来时提示（部分失败写日志不弹，否则长期缺模型的用户每次开机被弹一次）。
- `launch_window` 的 `from pet import PetWindow` 从 try **外**搬进 try 内：
  原来 pet.py 导入失败会穿过 `launch_all` → `main`，后续 agent 一个都不启动、
  用户什么都看不到，而那个 except 本来就是专门用来「把失败变成可见提示」的。

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