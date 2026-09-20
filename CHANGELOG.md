# Changelog

所有重要变更都会记录在此文件中。

## [0.15.0] - 2026-09-20

这一版主题是**「让情绪与表达真正闭环」**，附带修掉三个真 bug 和打包瘦身。

### 情绪分类层（新增）

原先两条路都撞墙：VA 坐标映射把正价区全塌成 `happy`；改 LLM 契约
又测不出主 LLM 行为（历史教训：必填标签 → 模型全不写，14 次 0 命中）。

第三条路：**不碰契约、不问模型**，直接对文本做 embedding 近邻。
移植 Soullink Emotion SDK（MIT）的 `classifier-embedding`：

- 语料 14 类 × 100 条（中文），归属见 `emotion_corpus.json` 的 `_source`
- top-K 近邻 → 相似度加权投票 → 加权 VAD → 置信度
- 两个视角：`user`（用户倾诉）/ `pet`（桌宠嘱咐），实测差异巨大
  （88% vs 33%——分布错配是真实的）
- 低置信**一律回退 neutral**：宁可不动，不能乱动

### 表达决策层（新增，默认关）

主 LLM 定情绪，本地引擎在同情绪的候选组里挑具体表情预设；
引擎离线/低置信时自动走兜底，不阻断对话。

> 默认 `enabled=false`：引擎 `/api/run-rlcd` 实测卡死（60s+），
> 开启会让主线程每次回复卡 8 秒。等端点可用后再开。

### 修掉三个真 bug

1. **`memory_write_buffer` 自死锁** —— `mark_dirty()` 持锁后又调
   `_schedule_flush()` 重入同一把锁。真实影响：约 10 次记忆写入后
   **整个桌宠冻结**。改 `Lock` → `RLock`。
2. **`emotion_classifier` 单例键不完整** —— 只按 `name` 缓存，
   忽略 `extra_corpus`，导致 154 秒的缓存重建。
3. **`pet_expression` MCP 工具静默失效** —— 把表情名传给收情绪名的
   `_apply_expression`，永远 `no-match` 却仍返回「已派发」。

### 会话 pin 漂移（真机事故）

桌宠 pin 到了**助手主对话**。根因 `ensure_session` 在
`preferred_session_id` 找不到时，会回退「该 agent 最近修改的会话」
——对助手而言就是主对话。桌宠 pin 失效后静默接管了它，
待机自言自语/输出规则/身份文本全写进用户与助手的对话。

修：点名要的会话不存在 → **新建**，绝不换成另一个；
pin 文件加 `owned` 归属标记，无标记的启动时丢弃。

### 人格来源修正

`_current_char`（模型包名，决定**画什么**）被当成人格来源
（决定**说什么**），导致桌宠读的是 `miku` 的身份而非助手的。
现改用 `dialog.agent_id`；`{{userName}}` 模板变量做替换；
待机模板里与输出规则冲突的 `加 [emotion:xxx]` 已删。

### MCP 工具 9 → 21

- 表现类 6 个（原 `QTimer.singleShot` 在 HTTP 线程不触发 → 全静默失效，已修）
- 电脑操作 8 个（`pet_computer_*`，默认只读，需显式开启动作）
- Hanako 4 个（`pet_hana_*`，读状态/会话/agent/app）

### 打包瘦身

| | 修前 | 修后 |
|---|---|---|
| 产物总计 | 828.7 MB | **542.1 MB** |
| `oc_pet.exe` | 262.7 MB | **23.9 MB** |
| Live2D 模型 | 47.9 MB | **0**（按承诺剔除） |

三个根因：onedir 模式的 EXE 误拿了 `a.binaries`/`a.datas`（onefile 写法，
同一批文件装两遍）；本机环境残留（`cv2`/`pyarrow`）被传递依赖拉进来；
`characters/` 整目录打包带出模型素材。

### 其他

- 新增 `core/hana_client.py`：通用 Hana HTTP 客户端
- 会话 pin 落盘（跨重启保持同一会话）
- CI 修复：本地全绿但 CI 红 6 个（依赖本机 `config.json` / Live2D 模型 / `~/.hanako`）
- README：引导教程 6 步 + MCP 章节 + 打包发布章节

### 测试

本地 2054 passed / 5 skipped；CI（干净环境）2023 passed / 28 skipped。

---

## [0.13.0] - 2026-09-16

这一版把**口型与语气**从「只有 Edge 能用」推到「全引擎通用」，
并解决了桌宠长期寄生在主 agent 上的三个可见症状。

### 口型：从「嘴一直匀速动」到「跟词跟声」

原先文件式 TTS（Edge / CosyVoice / MiMo / API）走 QMediaPlayer 拿不到 PCM，
口型只能用播放位置驱动的**正弦包络**——嘴全程匀速开合，句读处也不闭，看着假。

三级来源，逐级回退：

1. **音素级**（`feat(lip)`）文本 → 拼音 → 口型时间轴
2. **词级**（新增 `tts_provider/word_timings.py`）Edge TTS 的 `WordBoundary`
   落盘为 `<音频>.words.json`——词内开合、**词间归零（真的闭嘴）**
3. **能量分段**（新增 `tts_provider/audio_timings.py`）**任何 provider 都能用**：
   分帧 RMS → 自适应阈值（峰值×0.12）→ 合并近邻 → 滤短段，
   落盘 `<音频>.segments.json`

实测（Edge 合成「好的，我知道了。」）：

```
能量分段 [(240,560), (940,1560)]   ← 正确切出「好的，」与「我知道了。」
0.60s → 0.0000（停顿处闭嘴）       ← 旧正弦包络此处是 0.1350（一直在动）
分析耗时 149ms
```

解码三级兜底：soundfile → pydub → ffmpeg 子进程。
**侧车缺失/损坏一律回落原包络，绝不因口型增强影响出声。**

### 语气：情绪只改音高与响度，不改语速

新增 `tts_provider/emotion_prosody.py`。核心是一条硬规矩：

> **语速绝对不能变。只允许 pitch / volume / 语气描述改变语气。**

理由：语速是「这个人说话的样子」里最稳的特征。一改语速，听感从
「她心情变了」变成**「换了一个人在说话」**。

旧实现恰好踩坑：`emotion_tts_map` 同时改 rate 与 pitch
（`happy: +15%` / `angry: +25%` / `surprised: +30%`——最不该变声的时候变得最厉害），
且只覆盖 6 种情绪（渲染器用 7 种，`cute` 一直漏着）。

实测（同文本五种情绪）：neutral/sad/angry/surprised 时长均 2.136s、
happy 2.160s——**时长几乎相同 → 语速确实未变**。

### 桌宠专属 agent：规则不再污染用户消息

**症状**：会话标题变成 `[pet-output-rules] 1. 回复简短自然…`；
历史里存着带规则的原文；桌宠语气跑偏。

**根因**：规则包在 `text` 里发给 Hanako，而标题从第一条 user message 生成。

**协议调查**：WS 的 `prompt` 消息只透传 `text`/`displayMessage`/`uiContext`/
`sessionFileRefs`；内部 API 的 `context.system`（进 system prompt）WS 层不转发；
`uiContext` 只用于 annotations。→ 规则只能走 agent 的 `AGENTS.md`。

**落地**：新增 `~/.hanako/agents/ophelia-pet/`，其中 `AGENTS.md` / `identity.md`
**符号链接**到本仓库 `persona/`——内容源在桌宠仓库可用 git 管，
Hana 每次读盘无缓存，改源文件即刻生效（无需重启）。
输出规则移出 `text`（留 `dialog.inject_output_rules_in_text` 回退开关）。

### 声纹门卫：只认主人的声音

新增 `core/speaker_verify.py`（3d-speaker campplus，CPU 推理不吃显存），
ASR 前判是否为主人，拦截视频声/他人声。**失败放行是硬不变量**——
声纹是过滤器不是关卡。设置面板「功能 → 语音」新增声纹分组
（启用开关 / 阈值 0.30~0.90 / 录入主人声纹 / 校准 / 测试）。

### 修复

- **流式 TTS 卡死**：`frames_q.get()` 加 45s 超时；`StreamingPcmPlayer`
  的 Qt 调用全部移出 `self._lock`（根治 ABBA 死锁）
- **全局 TTS 被独立配置的保存动作改写**：`_save()` 原先无条件写全局 TTS，
  用户在「桌宠独立配置」改东西时功能页那个下拉的值也被一起落盘。
  改为脏标记——只写用户真动过的字段
- **4 处运行时崩溃**：`chat_thinking_dots` 少一个下划线（思考动画一画就炸）、
  `apply_glass_shadow`/`QApplication` 漏 import、`harness_adapter` 未定义名
- **Live2D Cubism 2 导入**：认 `.model.json`、剥包装目录、`agent_id` 从模型名推断
- **`tmp_file_to_cleanup` 作用域**：zip 校验失败路径必炸的 `NameError`

### 依赖

- `edge-tts` 改为 `>=7.0`（代码用 `Communicate(volume=, boundary=)` 与 `stream()`，6.x 无这些参数）
- 新增 `pydub~=0.25`、`imageio-ffmpeg~=0.4`（口型能量分段的 mp3 解码）
- `oc_pet.spec` 显式声明 `pydub` / `imageio_ffmpeg` / `soundfile` / `numpy` 隐藏导入

### 测试

1083 passed（新增 `test_word_timings` / `test_audio_timings` /
`test_emotion_prosody` / `test_speaker_verify` / `test_settings_tts_isolation`）。

---

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