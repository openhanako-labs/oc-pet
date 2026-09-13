# 架构索引

> 给新对话的助手看：读完这份文档就能理解代码结构，不用逐文件扫描。
> 最后更新：2026-08-12（对照真实代码结构重写）

## 目录结构

```
oc-pet/
├── main.py                   # 入口：日志 + 配置 + PetManager.launch_all + Qt 事件循环
├── pet.py                    # 主窗口（1516 行）：PetWindow 多重继承 10 个 mixin + QWidget
├── pet_manager.py            # 多宠管理器：Agent 发现 / 窗口生命周期 / 托盘 / WS 注入
├── config.py                 # 配置加载/保存（含异步写盘 async_config_saver）
├── env_config.py             # .env 环境变量读取（API key 等敏感信息）
├── paths.py                  # 路径解析
├── voice_input.py            # 麦克风录音 + Whisper ASR（含持续监听 VAD 支持）
│
├── core/                     # 核心逻辑（无 UI 依赖）
│   ├── conversation_engine.py  # 对话引擎（728 行）：LLM + TTS + 工具调用一体化，后台线程 + 代际打断
│   ├── harness_adapter.py      # LLM 适配器（读 Hanako 配置 → API/WS 调用）
│   ├── capability_registry.py  # 能力路由器：关键词→直接执行，跳过 LLM
│   ├── hanako_context.py       # Hanako 配置读取器：身份/记忆/模型/Session
│   ├── hanako_ws_client.py     # Hanako WebSocket 客户端
│   ├── hanako_session_manager.py # Hanako 会话管理（工具事件转发）
│   ├── hanako_monitor.py       # Hanako 状态监控（会话/事件过滤）
│   ├── idle_chatter.py         # 空闲自言自语生成
│   ├── perception/             # 感知控制器（情绪/屏幕/日程/主动消息）
│   ├── event_bus.py            # 轻量事件总线（发布/订阅，解耦任务系统）
│   ├── pet_audio_bridge.py     # 桌宠音频事件桥接（TTS 口型分发）
│   ├── emotion_transitions.py  # 情绪→动画过渡
│   ├── memory_snapshot.py      # 记忆快照导出/导入
│   ├── tool_registry.py        # 插件工具注册表（扫描 manifest.json）
│   ├── tool_executor.py        # 插件工具执行器（Node.js subprocess，无 shell）
│   ├── multi_pet_bridge.py     # 多桌宠协作桥接（社交事件）
│   ├── character_package.py    # 角色包管理
│   ├── enhanced_environment.py # 增强环境扫描（窗口→结构化快照）
│   ├── window_interaction.py   # 窗口互动（桌宠靠近当前窗口）
│   ├── phone_activity.py       # 手机活动感知（MacroDroid HTTP 上报）
│   └── phone_receiver.py       # 手机数据 HTTP 接收器
│
├── pet_mixins/               # PetWindow 行为拆分（10 个 mixin，鸭子类型共享 self）
│   ├── interaction_mixin.py    # 鼠标事件过滤（拖拽/摸头/双击）
│   ├── animation_mixin.py      # 动画状态机
│   ├── audio_mixin.py          # 音频回调（TTS 口型）
│   ├── bubble_mixin.py         # 气泡显示/节流/右键菜单
│   ├── chat_mixin.py           # 对话入口（输入框/语音/发送/持续监听 VAD）
│   ├── behavior_mixin.py       # 行为模式（idle/proactive/拖拽跟随）
│   └── voice_provider_mixin.py # TTS/ASR provider 构建与热切换
│
├── ui/                       # UI 组件
│   ├── bubble.py               # 对话气泡（富文本/emoji/打字机/换行）
│   ├── tts_player.py           # TTS 播放器（QMediaPlayer）
│   ├── settings_dialog.py      # 设置对话框
│   ├── plugin_panel.py         # 插件面板
│   ├── startup_screen.py       # 启动画面
│   ├── onboarding.py           # 新手引导
│   ├── status_hud.py           # 状态 HUD
│   ├── emotion_face.py         # 情绪脸
│   ├── activity_feed.py        # 活动流
│   ├── crt_window.py           # CRT 特效窗口
│   ├── crt_effects.py / crt_overlay.py / scene_background.py  # 视觉特效
│   ├── heart_particles.py      # 爱心粒子
│   ├── ink_subtitle.py / sfx.py / amadeus_hud.py # 其他 UI
│   └── theme/                  # 主题管理器
│
├── avatar/                   # 渲染系统（AvatarRenderer 抽象）
│   ├── base.py                 # AvatarRenderer 抽象接口
│   ├── factory.py              # 按角色目录选择渲染器（live2d / vrm / sprite）
│   ├── sprite_renderer.py      # 2D 帧精灵渲染器（QLabel + QTimer 帧动画）
│   ├── live2d_renderer.py      # Live2D (Cubism) 渲染器（live2d-py，QOpenGLWidget）
│   ├── gl_char_widget.py       # 承载 Live2D 的透明 QOpenGLWidget
│   └── vrm_renderer.py         # VRM 占位（未实现，显示提示）
│
├── motion/                   # 运动系统
│   ├── physics.py              # 物理引擎（重力/弹跳/惯性）
│   ├── behavior.py             # 行为参数（idle/walk/模式切换）
│   ├── mouse_tracker.py        # 鼠标追踪
│   ├── foreground_watcher.py   # 前台窗口检测（ctypes Win32 API）
│   └── action_linker.py        # 动作联动
│
├── tts_provider/             # TTS 引擎（TTSProvider 抽象）
│   ├── base.py                 # TTSProvider 抽象
│   ├── cosyvoice.py            # CosyVoice 本地 TTS（子进程 worker + 缓存 TTL）
│   ├── cosyvoice_worker.py     # CosyVoice 子进程（patch_torchaudio + 模型加载）
│   ├── api_tts.py              # API TTS
│   └── mimo_tts.py             # Mimo TTS
│
├── asr_provider/             # ASR 引擎（ASRProvider 抽象）
│   ├── base.py                 # ASRProvider 抽象
│   ├── whisper_local.py        # 本地 Whisper
│   ├── mimo_asr.py             # Mimo ASR
│   └── api_asr.py              # API ASR
│
├── scripts/                  # 工具脚本
│   ├── setup_tts_env.py        # TTS 环境搭建
│   ├── download_cosyvoice_model.py  # CosyVoice 模型下载
│   └── procedural_emotion_frames.py # 程序化情绪帧生成
│
├── characters/yuexinmiao/    # 内置角色（live2d 模型 + 配置）
├── docs/                     # 文档（v1-plan / agent-binding-plan / 评审报告等）
├── tests/                    # 集成测试
├── test_core.py / test_agent_binding.py / test_live2d_smoke.py / test_session_loop_repro.py  # 单元测试（61 例）
└── data/                     # 运行时数据（gitignore）
```

## 关键机制

### 对话流水线（conversation_engine.py）
```
用户消息 → engine.send(新代际) → 后台线程 _run 出队 → _process_message
  → 内置帮助/能力路由（跳过 LLM）→ LLM(HanakoPetAdapter) → 工具调用(可选)
  → 动画映射 → TTS 合成(线程池 _synth_and_reply) → on_reply 信号 → 主线程气泡+播放
```
- **代际打断（P1）**：每次 send/interrupt 递增 generation，LLM 后 / TTS 前 / TTS 后三处 `_is_stale` 检查，作废旧回复。
- **TTS 异步化（P1-4）**：合成移入 `ThreadPoolExecutor(max_workers=1)`，不阻塞消息队列。
- **TTS 竞态保护（P1-3）**：`_tts`/`_tts_ready` 读写均持 `_lock`。

### 渲染器抽象（avatar/）
- `AvatarRenderer` 定义统一接口（load/play_anim/set_emotion/draw/cleanup）。
- `factory.detect_format` 按 `pet.json format → 目录结构 → 回退 sprite` 选择渲染器。
- Live2D 走 QOpenGLWidget + live2d-py；Sprite 走 QLabel + 帧动画；VRM 为占位。

### ⚠️ Live2D SetScale 语义（2026-08-12 血泪教训）
**`SetScale(1.0)` = 角色画布适配窗口**，不是“1200px 画布的缩放倍率”。
实测（HitDrawable 命中检测，窗口 400x600）：
- scale=1.00 → 角色占窗口 95.8%（上 10px + 下 15px 留白）
- scale=1.05 → 99.2%（顶部贴齐，底部 5px）
- scale=1.06 → 100%（上下左右全贴齐，窗口完全贴合模型）
- scale=1.08+ → 100% 但可能裁发尖

**结论**：
- fit 直接 = pet.json `live2d.scale`（1.0=填满，1.06=贴合，>1.1=特写裁剪），**与窗口大小无关**。
- 之前的 `fit=(gl_h/1200)*coef` 公式是叠床架屋——窗口放大角色跟着放大，导致“窗口和角色绑死”。
- 窗口大小（config.window）与贴合度（live2d.scale）是**两个独立参数**，解耦。
- 调贴合度用 HitDrawable 网格扫描实测，不要盲调系数。

### 语音流程（voice_input.py + chat_mixin.py）
- 按键模式：点按开始/停止 → ASR → 发送。
- 持续监听模式：VAD 能量检测（RMS > 0.02），静音 1.3s 切分语音段，Semaphore(2) 限流并发 ASR。

### 多宠（pet_manager.py）
- 扫描 `~/.hanako/agents/` 发现所有 agent，每个一个独立窗口（PetWindow 实例）。
- 每个窗口绑定自己的 Hanako WS 会话（set_agent_context）。
- 多宠 Live2D：`l2d.init()` 进程级只调一次（模块级 `_global_l2d_inited`），关闭单个宠不释放全局 GL（P1-1）。

## 配置与安全
- API key 全部来自 `.env` / `~/.hanako/provider-catalog.json`，无硬编码密钥。
- `.env`、`config.json`、`data/`、`logs/` 均在 `.gitignore`。
- 插件工具执行无 shell（`['node', tmp]`），无命令注入面。

## 已知技术债（2026-08-12 评审）
- `pet.py` 仍是 god-object：10 个 mixin 只搬方法，`__init__` 约 300 行集中接线。
- `_rebuild` 的 `old.cleanup()` 与 worker 正在合成的 use-after-cleanup 残留（需引用计数）。