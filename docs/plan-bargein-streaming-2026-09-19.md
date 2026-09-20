# oc-pet 语音链路改进方案 —— barge-in 与分句流水线

> 状态：**方案（待实施）**
> 日期：2026-09-19
> 作者：奥菲莉娅
> 范围：`pet_mixins/chat_mixin.py`、`core/conversation_engine.py`、`ui/tts_player.py`
> 独立复核：爱莉丝（只读核实现状判断，结论并入 §1）

---

## 0. 需求复述

为 oc-pet 的 TTS/ASR 链路做两项改进，每项须给出：精确改动点（文件:行号）、回退策略、可执行验收、风险。**只出方案，不动手。**

- **方案 A**：持续监听模式下补 barge-in（用户开口时打断正在播放的 TTS）
- **方案 B**：TTS 分句流水线（首句提前出声 + 句间无缝）

---

## 1. 现状核实

> 本节每条判断均由爱莉丝独立复核（只读）。

**复核总评（原文）**：整体可信度 **高（约 85%）**——6 条判断：4 条完全成立、1 条成立但有澄清点、1 条部分成立（主结论对，细节描述错）。

**被推翻的具体条目**：§1.1 里「分支只有 `_stop_walking()`」这句**是错的**（该分支内根本没有 `_stop_walking()`，它在 `_toggle_chat()` 里）。主结论仍成立，描述已改正。

**复核额外提出 3 条我漏掉的关键路径**（§1.10）：完整触发矩阵（持续监听两行全空）、三处死代码、`_is_stale` 的真实语义。**其中第 1 条直接改变了方案 A 的设计**（从一个改动点变为两个）。

### 1.1 判断 A-1：持续监听无 barge-in ✅ 成立（描述有一处错，已改）

**位置**：`pet_mixins/chat_mixin.py:190-194`，`_on_voice_vad()` 的 `if is_speech:` 分支

```python
if is_speech:
    # 有人说话：累积音频，重置静音计数
    with self._voice_buffer_lock:
        self._voice_continuous_buffer.append(chunk.copy())
    self._voice_continuous_silence = 0
```

分支内只有这 3 条，**没有** `_tts_player.stop()`、**没有** `_engine.interrupt()`、**没有** `tts_stop_signal.emit()`。

> ⚠️ **初稿错误（已被复核推翻）**：初稿写「分支里只有 `_stop_walking()`」。**实际该分支里根本没有 `_stop_walking()`**——它出现在 `chat_mixin.py:269` 的 `_toggle_chat()` 里，与 VAD 回调无关。主结论（无打断路径）仍成立，但那句描述是错的。

**下游也无间接路径**：静音超时后的 `_do_continuous_asr` 闭包（`chat_mixin.py:222-253`）只调 `eng.send(text, character=...)`。

**对照证据（最有力）**：按住说话模式的 `_do_asr` 闭包里有 `tts_stop_signal.emit()`（`chat_mixin.py:73`），而持续监听的 `_do_continuous_asr` **完全没有这一步**。两者结构对称，唯独这里缺一环。

**复核结论**：主结论成立；细节描述错误（已改正）。**复核还给出了根因**：不是「忘了写打断判据」，而是「ASR 完成后缺一次 `tts_stop_signal.emit()`」——与按住说话模式对比，那里有两层 stop（开始时 barge-in 一次 + ASR 完成时清理一次），持续监听两层都没有。

### 1.2 判断 A-2：按住说话有 barge-in ✅ 成立

**位置**：`pet_mixins/chat_mixin.py:44-51`（`_toggle_voice` 的"开始录音"分支）

```python
if not self._voice_recording:
    # 开始录音 → 立即打断当前对话（barge-in），进入聆听
    if self._engine:
        try:
            self._engine.interrupt(reason="voice_start")   # ← 第 48 行
        except Exception:
            logger.debug("chat_mixin: 非致命异常(已静默吞掉)", exc_info=True)
    self._tts_player.stop()                                # ← 第 51 行
```

此处从**主线程**调用（按钮点击），故可直接碰 Qt 对象。

**复核结论**：行号 48/51 精准匹配。文件顶部注释（`chat_mixin.py:8`）亦确认「语音开始录音时触发打断（voice_start，barge-in）」。

**复核补充**：按住说话模式实际有**两层 stop**——第 51 行开始时 barge-in 一次，ASR 完成后经 `pet.py:2497` 的 `_stop_all_tts` 再清理一次。

### 1.3 判断 A-3：存在"音频线程不能直调 Qt stop"约束 ✅ 成立

**位置**：
- `pet.py:96` — `tts_stop_signal = Signal()`（初稿写 97，实际 96）
- `pet.py:94-95` — 原因注释：「QMediaPlayer 是 COM 组件，跨线程调用会触发 `RPC_E_SERVERCALL_RETRYLATER 0x8001010D`」
- `pet.py:323` — `self.tts_stop_signal.connect(self._do_tts_stop)`
- `pet.py:3178-3192` — `_do_tts_stop()` 槽

槽的 docstring 原文：

> QMediaPlayer（Windows 后端=Media Foundation，纯 COM）只能在创建它的主线程操作；任何后台线程（ASR 识别完成、持续监听）需要停 TTS 时一律通过 emit tts_stop_signal 走到这里。

**这是方案 A 必须遵守的约束**：`_on_voice_vad` 运行在音频回调线程，只能 emit 信号。

**复核结论**：成立。信号连接与槽的行号精准；设计动机、实现、注释、错误码四处一致。调用点 `chat_mixin.py:71-73` 的注释也带同一错误码。

### 1.4 判断 B-1：LLM 流式 chunk 不驱动 TTS ✅ 成立（需一处澄清）

**位置**：`pet.py:2912-2926` 的 `_do_engine_chunk`，**只有** `_show_bubble_stream` / `_append_bubble_text`，零 TTS 调用。

**复核澄清（重要）**：TTS **有**自己的流式通路（`on_tts_stream` → `pet.py:2890` `_do_engine_tts_stream` → `StreamingPcmPlayer.feed`），但那是**合成侧的流**，不是 LLM 层的流。其触发点是 `_synth_stream_and_play` 里的 `cb("begin", ...)`——**此时 LLM 回复已完整生成**。

所以完整表述是：

| 路径 | 是否 LLM chunk 驱动 | 何时开始 |
|---|---|---|
| 气泡显示 | ✅ 是（`on_llm_chunk`） | 第一个 chunk |
| Qwen local 流式 TTS | ❌ 否（合成侧流） | LLM 完整返回后 |
| Edge/CosyVoice/MiMo/API 文件式 TTS | ❌ 否 | LLM 完整返回 + 整段合成完 |

**复核结论**：成立。初稿的表述「TTS 必须等完整回复」需限定为「**所有路径**的 TTS 都等 LLM 完整返回」——这一点正确；但要说清 TTS 内部有自己的流式层。

### 1.5 判断 B-2：两个播放器都无 pause/resume ✅ 成立

- `ui/tts_player.py`（类名 `TTSTtsPlayer`）：`enabled / enable / disable / set_volume / play / enqueue / stop / position_seconds / current_level / is_playing`
- `ui/streaming_pcm_player.py`：`enabled / last_error / sample_rate / current_level / played_seconds / disable / set_volume / set_prebuffer / set_expected_duration / is_playing / prepare / feed / finish / stop`

**均无 pause/resume。** 全项目 `.pause(` / `.resume(` 的 20+ 处命中全是 QTimer / QMovie / audio_bridge / screen observer / thinking dots，**无一处针对 TTS 播放器**。

**复核补充**：两个 `disable()` 内部都调 `stop()`——但那是「开关 + stop」，不是 pause/resume。QMediaPlayer 底层 API 有 `pause()`，当前封装**未暴露**。

这封死了"假打断恢复"（被误打断后接回来说完）—— 那需要方案 A 之外的单独立项。

**复核结论**：成立。

### 1.6 判断 B-3（新发现，重要）：`enqueue()` 已写但从未接线 ✅ 成立

**证据**：`tests/test_perf_fixes.py:605` 的注释原文——

> ③ 分句预合成 enqueue() 写了但从未接线

全库 grep `enqueue`：除 `ui/tts_player.py` 自身的定义与文档、`core/multi_pet_bridge.py:410` 的无关同名方法、`tests/` 的注释外，**零调用点**。

`enqueue()`（`ui/tts_player.py:88-100`）的 docstring 写着它是"分句预合成专用"：

> 逐句合成 → enqueue，前一句播完自动接下一句，续播时不重触发 on_start（口型/说话状态跨句连续），全部播完才触发一次 on_end。

**即：方案 B 需要的播放端能力早就写好了，缺的是引擎侧的分句与接线。**

**复核结论**：成立（复核独立复现了零调用点）。

### 1.7 当前实际配置（影响方案优先级）

`config.json` 实测值（读取时间 2026-09-20 00:0x）：

```json
"tts": { "provider": "edge", "enabled": true, "edge_voice": "zh-CN-XiaoxiaoNeural", "stream": null }
"asr": { "provider": "whisper_local", "backend": "whisper", "model": "small", "language": "auto", "device": "" }
```

**三条推论**：

1. **provider = edge** → `EdgeTtsProvider` **没有** `can_stream` 方法（全库仅 `QwenTtsProvider` 有）。故 `_synth_and_reply` 的流式分支不进入，走**非流式整段合成** `tts.synthesize(tts_text, ...)`（`core/conversation_engine.py:2057`）。
2. **非流式路径完全没有分句**——`_split_tts_sentences` 全库只有一个调用点（`conversation_engine.py:1939`，在流式路径内）。
3. **`backend = "whisper"`**（非 `faster_whisper`）→ 走 openai-whisper 分支，**无 `vad_filter`、无 `avg_logprob`/`no_speech_prob` 置信度防御**（这些只在 faster_whisper 分支里）。`language = "auto"` → `initial_prompt` 中文提示也不注入（`whisper_local.py:169` 的守卫是 `if language is not None`）。

**第 3 点是一个此前未被指出的实际缺口**：`config.template.json` 里 backend 默认是 `faster_whisper`，但 `config.json` 实际是 `whisper`。两者能力不等价。

**推论对方案的影响**：方案 B 的收益在**当前配置下更大**（Edge 路径整段合成 → 首音延迟 = 全文合成时间），但也意味着 **B 的第一阶段应该先做 Edge 路径**，而不是 Qwen 流式路径。

### 1.8 判断 A-4（新核实）：帧长与阈值换算
`core/audio_input/vad.py:56-57`：

```
# Silero 固定窗口：16k 采样率下 512 样本 ≈ 32ms
WINDOW = 512
```

交叉验证：`chat_mixin.py:192` 的 `SILENCE_FRAMES_LIMIT = 40  # 约 1.3s（512 帧/帧）`——40 × 32ms = 1.28s，吻合（注释里的「512 帧/帧」表述不精确，应为「每帧 512 样本」）。

**结论**：15 帧 ≈ 480ms 成立。但 `voice_input.py:212` 的 `sd.Stream` 未指定 `blocksize`，回调间隔由驱动决定，需实测确认。

**复核结论**：未纳入本次复核范围（复核聚焦 6 条主判断）。

### 1.9 判断 A-5（新核实，**改变方案 A 的风险评估**）：声纹门卫已存在但未启用

**发现**：`core/speaker_verify.py`（2026-09-15 新增，264 行）是一个**说话人验证门卫**——判断一段录音是不是主人说的。

| 项 | 状态 | 证据 |
|---|---|---|
| 实现 | ✅ 完整 | `core/speaker_verify.py`，sherpa-onnx + 3d-speaker campplus（ONNX，CPU 推理） |
| 依赖 | ✅ 已装 | `sherpa_onnx` import 成功 |
| 模型 | ✅ 已下载 | `data/models/speaker/3dspeaker_..._advanced.onnx`（28.3 MB） |
| **主人样本** | ❌ **缺失** | `data/voiceprint/owner.wav` **不存在** |
| **配置开关** | ❌ **关闭** | `config.json` 的 `voiceprint.enabled = false` |
| 接线 | ✅ 已接 | `voice_input.py:79-80`（`_voiceprint_gate`），两个 transcribe 入口都调（约 281 行与 320 行） |
| 失败策略 | 放行 | 模型/样本/推理任一异常 → `return True`（绝不阻断语音） |

**为什么这直接改变方案 A 的风险评估**：

§2.6 我把「视频人声误打断」列为 A 的最大风险。但声纹门卫**正是为这个问题造的**——它的 docstring 第一句：

> 桌宠的持续监听/语音输入会被视频声、他人说话误触发。在 ASR 之前加一道声纹门卫：非主人声音直接丢弃。

**即：一个比 A 更根本的解法已经写好，只是没启用**（缺主人样本 + 开关关）。

**但它救不了 A**——理由：门卫在 **ASR 之前**（需要一段完整音频 → 落盘 wav → 提取 embedding），而 barge-in 需要在**音频回调帧级**实时判断。门卫有几百毫秒的延迟，且需要成段音频。**两者互补而非替代**：门卫管「这句话该不该进 ASR」，barge-in 管「用户开口时该不该停嘴」。

**对方案 A 的实际影响**：

1. **不能指望门卫保护 barge-in**——A 仍需自己的判据。
2. **但 A 的误打断风险可以降低**：若门卫启用，视频人声在**进 ASR 时就被拦**，故 barge-in 打断后不会有「误识别文本被发送」的二次伤害——只是白停一次嘴。
3. **建议顺序调整**：启用门卫（登记主人样本 + 开开关）**应该排在 A 之前**——它是纯配置动作（加一个 `owner.wav` + 改一个布尔值），零代码，却能同时改善「误触发识别」和降低 A 的副作用。

**待确认**：主人样本怎么登记？`ui/settings_dialog.py:1467` 调 `register_owner(src_wav)`，设置面板有采集入口（`voiceprint_enabled` 复选框在 425 行）。**登记流程未实测**。

**复核结论**：不在本次复核范围内（复核聚焦 6 条主判断）。§1.9 全部为奥菲莉娅单方核实，**可信度低于已复核的各条**。

### 1.10 复核额外发现（**这些是我漏掉的，且直接影响方案设计**）

#### （1）完整的 TTS 停止触发矩阵 —— 这就是判断 A-1 的**根因**

| 触发场景 | `_tts_player.stop()` | `interrupt()` | `tts_stop_signal.emit()` | `_stop_all_tts()` |
|---|---|---|---|---|
| 按住说话开始 | ✅ `chat_mixin:51` | ✅ `voice_start` | — | — |
| 按住说话 ASR 完成 | — | — | ✅ `chat_mixin:73` | ✅ `pet.py:2497` |
| **持续监听检测到人声** | — | — | — | — |
| **持续监听 ASR 完成** | — | — | — | — |
| 输入框发送 | ✅ `chat_mixin:301` | ✅ `new_message` | — | ✅ `pet.py:2497` |
| HTTP 直接发送 | — | ✅ `pet.py:815` | — | — |
| 新回复到达 | — | — | — | ✅ `pet.py:2964` |
| 新流式 TTS begin | — | — | — | ✅ `pet.py:2895` |

**持续监听两行全空。** 对比按住说话的两行——那里有**两层 stop**（开始时 barge-in 一次 + ASR 完成时清理一次）。

**这修正了我对方案 A 的理解**：不只是「检测到人声时缺一次打断」，而是「**持续监听整条路径上零次 TTS 停止**」。方案 A 需要补的是**两个点**，不是一个：

1. **检测到人声时**（帧级）→ barge-in
2. **ASR 完成、文本即将发送时** → 清理（对应 `chat_mixin:73` 的对称位置）

第 2 点尤其重要：即使 barge-in 没触发（比如用户说得短、没到 15 帧），**ASR 完成后也应该把 TTS 停掉**——否则用户的语音指令发出去了，桌宠还在说上一段。

#### （2）三处死代码（**影响我对代码的信任度**）

| 项 | 位置 | 状态 |
|---|---|---|
| `_interrupt_event` | `conversation_engine.py:196` 定义，707/844 处 `.set()` | **从未被 wait/is_set/clear**——注释里「LLM/工具/合成可检查」是期望，不是实现 |
| `_last_interrupt_state` / `_last_interrupt_reason` | `conversation_engine.py:845-846` 写入 | 只有 getter（868-872），**无生产代码读取** |
| `reason="user_stop"` | `conversation_engine.py:839` 定义 | **全项目零调用**——`state_map` 里「user_stop: 旧回复保留待恢复」是预留设计，**无任何恢复路径** |

**对方案的意义**：第 3 项确认了 §1.5 的结论——「假打断恢复」不是「没接好」，而是**从未实现**。文档里那句「保留待恢复」是意图，不是能力。

#### （3）`_generation` / `_is_stale` 的真实语义

`conversation_engine.py:1027-1034`。作用：**跳过旧代际的后续 chunk 与 TTS 合成**，但**不停掉已经在播的 TTS**。

**这是关键区分**：代际机制是「不再生产」，不是「停止消费」。方案 A 要的是后者，所以必须显式调 `stop`/`emit`，不能指望代际机制代劳。

【复核】（已回填，见上）

---

## 2. 方案 A：持续监听 barge-in

### 2.1 问题

持续监听模式下用户插话，桌宠不会停嘴——得等它说完，或手动点停止。按住说话模式有，持续监听没有。

### 2.2 为什么不能直接照搬按住说话那条路

两个原因：

1. **线程不同**。按住说话在主线程（按钮点击），可持续监听在**音频回调线程**。直调 `_tts_player.stop()` 会碰 Qt/COM，触发 `0x8001010D`（§1.3 的既有教训）。
2. **误触发**。一有声音就打断 → 播着视频会被反复打断。需要判据。

### 2.3 设计

> ⚠️ **本节已根据复核结论修订**（§1.10 第 1 项）。初稿只设计了「检测到人声时打断」一个点；复核的触发矩阵表明持续监听整条路径上**零次 TTS 停止**，需要补**两个点**。

#### 2.3.1 改动点 1：检测到人声时打断（帧级）

在 `_on_voice_vad` 的 `if is_speech:` 分支里加**三级判据**，全部满足才打断：

| 条件 | 建议值 | 依据 |
|---|---|---|
| 连续语音帧数 ≥ N | 15 帧 ≈ 480ms | 对应 livekit `min_interruption.duration = 0.5s`；帧长按 512 样本/16000Hz = 32ms 折算 |
| 距上次打断 ≥ 冷却 | 1.0s | 防连续 emit |
| 当前确实在播 TTS | `self._renderer._speaking` | **不能用 `is_playing()`**——见下方修正 |

> ⚠️ **实施期修正（原方案有错）**：初稿写「当前确实在播 TTS」用 `is_playing()`。**这是错的**——`_on_voice_vad` 跑在音频回调线程，而 `ui/tts_player.py:291` 与 `ui/streaming_pcm_player.py:358` 的 `is_playing()` 都会调 `sink.state()` / `playbackState()`，**那是 Qt 内部锁**。`streaming_pcm_player.is_playing()` 的 docstring 还专门警告了这一点（「绝不能持 self._lock 调 sink.state()……主线程会冻死（事故 2026-09-15）」）。
>
> **改用 `self._renderer._speaking`**（`avatar/live2d_renderer.py:453/3109`）：`set_speaking()` 只是一次 `self._speaking = bool(speaking)` 纯 Python 赋值，**不碰 Qt、不持锁**，从音频线程读安全。
>
> 它由 `pet_mixins/audio_mixin.py:24`（`on_tts_start`）置 True、`:62`（`on_tts_end`）置 False，**恰好覆盖“正在说话”语义**。

**帧长依据**（本次核实）：`core/audio_input/vad.py:56-57` 注释「Silero 固定窗口：16k 采样率下 512 样本 ≈ 32ms」，`WINDOW = 512`。交叉验证：`chat_mixin.py:192` 的 `SILENCE_FRAMES_LIMIT = 40` 注释写「约 1.3s」——40 × 32ms = 1.28s，吻合。故 15 帧 = 480ms。

⚠️ **但帧长不保证是 32ms**：`voice_input.py:212` 的 `sd.Stream(...)` **未指定 `blocksize`**，实际块长由声卡驱动决定；Silero 的 `is_speech` 会把不足 512 的补零、超过的截断（`vad.py:147-150`）。所以 `_bargein_frames` 计数的是**回调次数**而非严格 32ms 单位。若驱动块长偏大，480ms 会变长。**首次实施时应打一条日志记录实测帧间隔。**

**不做** backchannel 抑制（"刚开口/快说完的 1 秒内短促声音不算打断"）——那需要知道 agent 说话的起止时刻，而 `_on_voice_vad` 手上没有。**先做最小可用版**，把 backchannel 留给后续。

**新增状态**（在 `chat_mixin` 的语音状态区）：

```python
self._bargein_frames = 0          # 连续语音帧计数
self._bargein_last_emit = 0.0     # 上次 emit 的 monotonic 时刻
```

**改动位置**：`pet_mixins/chat_mixin.py`，`_on_voice_vad` 的 `if is_speech:` 分支

```python
if is_speech:
    with self._voice_buffer_lock:
        self._voice_continuous_buffer.append(chunk.copy())
    self._voice_continuous_silence = 0
    if not self._voice_continuous_started:
        self._voice_continuous_started = True
        self._stop_walking()

    # ── 新增：barge-in ──
    self._bargein_frames += 1
    if self._bargein_frames >= _BARGEIN_FRAMES:            # 15
        now = time.monotonic()
        if now - self._bargein_last_emit >= _BARGEIN_COOLDOWN_S:   # 1.0
            self._bargein_last_emit = now
            self._bargein_frames = 0
            # 音频回调线程：只能 emit 信号（见 pet.py:3178 的 COM 约束）
            try:
                self.tts_stop_signal.emit()
            except Exception:
                logger.debug("chat_mixin: barge-in 信号发送失败", exc_info=True)
            if self._engine:
                try:
                    self._engine.interrupt(reason="voice_bargein")
                except Exception:
                    logger.debug("chat_mixin: barge-in 引擎打断失败", exc_info=True)
else:
    self._bargein_frames = 0        # 静音即重置
```

**两个细节**：

- `_engine.interrupt()` 跨线程调用**符合既有模式**——持续监听的 ASR 完成后本就在后台线程调 `eng.send(...)`（`chat_mixin.py` 的 `_do_continuous_asr`）。`interrupt()` 内部只做 `self._generation += 1` 与状态映射，不碰 Qt。
- `_bargein_frames` 在 `else` 分支重置——**不是**在语音段结束时重置。这样"持续说话"不会累积到无限大，但"说一句停一下再说"会重新计数。符合"连续语音"的语义。

#### 2.3.2 改动点 2：ASR 完成后清理（**复核新增，与改动点 1 同等重要**）

**位置**：`pet_mixins/chat_mixin.py` 的 `_do_continuous_asr` 闭包（约 222-253 行）

**对照对象**：按住说话的 `_do_asr`（`chat_mixin.py:73`）有 `self.tts_stop_signal.emit()`，持续监听的 `_do_continuous_asr` **没有**。

```python
# 现状（约 230-244 行）
def _do_continuous_asr(audio_data=audio, vi_ref=vi, eng=engine, sem=sem):
    try:
        text = vi_ref.transcribe_audio(audio_data)
        if text and eng:
            ...  # 去重
            eng.send(text, character=self._current_char)
    finally:
        sem.release()
```

改为（在 `eng.send` 之前插入）：

```python
if text and eng:
    ...  # 去重（保持原样）
    # 复核补充：与按住说话模式对齐——文本即将发送，先停掉正在播的 TTS
    # 即使改动点 1 的 barge-in 未触发（用户说得短、没到 15 帧），这里也必须停
    try:
        self.tts_stop_signal.emit()
    except Exception:
        logger.debug("chat_mixin: 持续监听 ASR 后停 TTS 失败", exc_info=True)
    eng.send(text, character=self._current_char)
```

**为什么必须补这一点**：即使 barge-in 没触发，用户的语音指令已经发出去了，而桌宠还在说上一段——**两个声音叠着播**。这与 `chat_mixin.py:305` 注释里警告的情形同源（「旧回复不作废且旧 TTS 不被打断 → 两条声音叠着播」）。

**这行改动本身只有 4 行**，但它是持续监听模式缺得最久的一环。

### 2.4 回退

配置项 `asr.bargein.enabled`（默认 `true`）；置 `false` 则整段跳过，行为与改动前完全一致。

新增字段缺失时按默认值走（`config.get("asr", {}).get("bargein", {}).get("enabled", True)`），不因配置未升级而失效。

### 2.5 验收

**可执行（单元级）**：
- 新增 `tests/test_bargein.py`：构造 mock 的 `_on_voice_vad` 环境，连续投喂 15 帧 `is_speech=True`，断言 `tts_stop_signal` 被 emit **恰好一次**；再投喂 14 帧，断言**不** emit。
- 断言静音一帧后计数归零。
- 断言 `asr.bargein.enabled=false` 时任何帧数都不 emit。
- **新增**：构造 mock 的 `_do_continuous_asr`，断言 ASR 返回非空文本时 `tts_stop_signal.emit()` 被调用（改动点 2）。

**手动级**：
- 持续监听开启 → 触发一段长回复 → 在桌宠说话中途对麦克风正常音量说"等一下" → **应在约 0.5s 内停嘴**。
- 反向：播放 B 站视频（人声）→ 桌宠说话时**不应**被打断（若被打断，说明 15 帧阈值过低，需上调）。

> ⚠️ 第二条反向验收是**这条方案真正的风险所在**。Silero VAD 能区分人声与音乐，但**区分不了"你说话"和"视频里的人说话"**——这是 §1.1 里 `_vad_noise_floor` 一直在补但补不干净的洞。**方案 A 会把这个洞从"误触发识别"放大成"误打断说话"。**
>
> **缓解（见 §1.9）**：启用已有的声纹门卫（`voiceprint.enabled=true` + 登记 `owner.wav`）。它拦不住 barge-in 本身，但能拦住「误打断后视频声被当成用户指令发送」的二次伤害。**建议先做这个，再做 A。**

### 2.6 风险与缓解

| 风险 | 缓解 |
|---|---|
| 视频人声误打断 | 阈值可配（`asr.bargein.frames`）；默认保守取 15 帧；提供开关 |
| 冷却期内连续插话被吞 | 冷却 1.0s 后重新计数，不会永久失效 |
| `tts_stop_signal.emit()` 在音频回调线程的开销 | Qt 跨线程信号是队列投递，非阻塞；emit 本身微秒级 |
| 打断后文字气泡被吞 | **不会**——文字先行（P1-6），气泡在 LLM 返回时已显示，与 TTS 无关 |
| 双点重复 emit（barge-in + ASR 完成） | 无害：`_do_tts_stop` 是幂等的（对已停的 player 再 `stop()` 无副作用） |

**已知未解决**：打断后无法恢复（无 pause/resume，见 §1.5）。用户插话后桌宠会从头重说或干脆不说——这是"听着像机器"的根源之一，但**不在本方案范围内**。

---

## 3. 方案 B：分句流水线

### 3.1 问题

首音延迟 = **全文合成时间**（当前 Edge 配置下）。且句间无预取，长回复是"一段合成完才出声"。

### 3.2 两条路径，分别处理

| 路径 | 触发条件 | 现状 | 目标 |
|---|---|---|---|
| **文件式** | `can_stream()` 为假（Edge / CosyVoice / MiMo / API） | 整段 `synthesize()`，**无分句** | 分句 → 首句先播，后续 `enqueue` |
| **流式** | `can_stream()` 为真（Qwen local） | 分句串行 `synthesize_stream()` | 分句 + 预取下一句 |

**建议顺序：先 B1（文件式），后 B2（流式）**——理由见 §1.7：当前配置走文件式，且文件式改动风险更低（`enqueue` 已就绪）。

### 3.3 B1：文件式分句 + enqueue 接线

#### 3.3.1 引擎侧（`core/conversation_engine.py`）

**位置**：`_synth_and_reply` 的非流式分支，约 2057 行

现状：
```python
audio_path = tts.synthesize(tts_text, character_id=character, instruct=instruct,
                            voice=voice, emotion=emotion) or ""
if audio_path:
    logger.info("TTS done: %s", os.path.basename(audio_path))
```

改为分句循环 + 逐句回调：

```python
_sents = _split_tts_sentences(tts_text)
if not _sents:
    _sents = [tts_text]
for _i, _sent in enumerate(_sents):
    if self._is_stale(gen):          # 逐句检查代际，打断即停
        break
    _p = tts.synthesize(_sent, character_id=character, instruct=instruct,
                        voice=voice, emotion=emotion) or ""
    if not _p:
        continue
    if _i == 0:
        _call_reply_cb(self.on_reply, reply, emotion, anim, _p, None)   # 首句：走现有路径
    else:
        _enqueue_cb = getattr(self, "on_tts_enqueue", None)             # 后续句：新回调
        if callable(_enqueue_cb):
            _enqueue_cb(_p, gen)
```

**新增回调** `on_tts_enqueue`（与既有 `on_tts_stream` 同形，`conversation_engine.__init__` 里初始化为 `lambda path, gen: None`）。

#### 3.3.2 主线程侧（`pet.py`）

新增一条信号（仿 `tts_stream_signal`，`pet.py:102`）：

```python
tts_enqueue_signal = Signal(str, int)   # audio_path, gen
```

槽实现（仿 `_do_engine_tts_stream`，`pet.py:2890`）：

```python
def _do_tts_enqueue(self, audio_path: str, gen: int):
    """主线程：把后续句追加到播放队列（前一句播完自动接）。"""
    try:
        if not audio_path or not os.path.exists(audio_path):
            return
        player = getattr(self, "_tts_player", None)
        if player is not None and player.enabled:
            player.enqueue(audio_path)
    except Exception as e:
        logger.warning("_do_tts_enqueue error: %s", e)
```

接线：`self.tts_enqueue_signal.connect(self._do_tts_enqueue)`（挨着 `pet.py:323` 那组）。

**关键点**：`enqueue()` 不重触发 `on_start`（`ui/tts_player.py:106`），所以口型与"说话中"状态**跨句连续**——正是我们要的。全部播完才触发一次 `on_end`。

#### 3.3.3 一个必须处理的边界

`ui/tts_player.py:88` 的 `play()` 会 **`self._queue.clear()`**。

若首句用 `play()`、后续句用 `enqueue()`，顺序正确。但若首句合成**失败**（`_p` 为空）而第二句成功，第二句会走到 `else` 分支 → `enqueue()` → 队列空且无播放器 → **立即播放**（`enqueue` 的 `if self._player is not None or self._queue:` 为假 → 走 `_play_file`）。行为正确，无需特殊处理。

但反过来的情况要注意：若**首句成功、第二句失败**，第三句仍走 `enqueue`——队列里有第二句吗？没有（第二句 `continue` 了）。此时若首句还在播，第三句进队列；若首句已播完（`on_end` 已触发、`stop()` 已清队列），第三句会立即播。**行为仍正确。**

所以**只需保证"第一句用 play、其余用 enqueue"这一条**，中间失败不会错乱。这得益于 `enqueue` 的实现（队列空且无播放器时直接播）。

#### 3.3.4 顺带修掉的一个真问题

`tts_provider/edge_tts.py:112`：

```python
text = text.strip()[:500]
```

**Edge 路径下单句超过 500 字会被静默截断。** 分句后单句几乎不可能到 500 字，**这个问题被分句顺带解决**。但要在验收里加一条：构造一段 600 字的回复，确认全部内容被读出（而非前 500 字）。

### 3.4 B2：流式路径预取

#### 3.4.1 现状

`core/conversation_engine.py:1948`：

```python
for _si, _sent in enumerate(sentences):
    if self._is_stale(gen): break
    for pcm in tts.synthesize_stream(_sent, voice=voice):
        if self._is_stale(gen): break
        cb("chunk", pcm, gen)
```

**串行**：第 N 句的合成在第 N-1 句**播完之后**才开始（因为 `cb("chunk")` 是喂给播放器，而生成器返回即代表该句合成完）。句间必有空隙。

#### 3.4.2 设计：生产者-消费者预取

引入一个**单槽预取**：当前句在播时，后台线程已开始合成下一句。

```python
from concurrent.futures import ThreadPoolExecutor

def _synth_stream_and_play(...):
    ...
    prefetch = ThreadPoolExecutor(max_workers=1, thread_name_prefix="TTSPrefetch")
    try:
        next_fut = None
        for _si, _sent in enumerate(sentences):
            if self._is_stale(gen): break
            # 取当前句：要么是上一轮预取的结果，要么现取
            if next_fut is not None:
                cur_gen = next_fut
                next_fut = None
            else:
                cur_gen = prefetch.submit(_synth_one, _sent)
            # 立刻提交下一句的预取
            if _si + 1 < len(sentences):
                next_fut = prefetch.submit(_synth_one, sentences[_si + 1])
            # 消费当前句（阻塞等它的 chunk）
            for pcm in cur_gen.result():
                if self._is_stale(gen): break
                cb("chunk", pcm, gen)
    finally:
        prefetch.shutdown(wait=False)
```

`_synth_one(sent)` 是个薄包装：`return tts.synthesize_stream(sent, voice=voice)`——但**生成器不能跨线程 result()**（`Future.result()` 返回生成器对象，迭代仍发生在调用线程）。所以要么在 worker 里 `list()` 化（**会吃掉流式的低延迟优势**），要么用 `queue.Queue` 做真正的生产者-消费者。

**正确做法**（生产者线程推 chunk 到队列，消费线程取）：

```python
import queue as _queue

def _prefetch_worker(sent, voice, q, gen_ref, is_stale):
    try:
        for pcm in tts.synthesize_stream(sent, voice=voice):
            if is_stale(gen_ref[0]):
                break
            q.put(pcm)
    except Exception as e:
        q.put(("__error__", str(e)))
    finally:
        q.put(None)          # 哨兵
```

消费端从 `q.get()` 取，遇到 `None` 切下一句。

**这个改动比 B1 复杂得多**，涉及线程生命周期、队列容量（要不要限长防止内存膨胀）、打断时的清理。

> ⚠️ **B2 的前提是 Qwen local 模式实际在用。** 当前配置是 Edge，B2 的收益为零。**建议：B1 落地并实测后，再决定是否做 B2。**

### 3.5 回退

- B1：配置项 `tts.stream.sentence_fanout`（默认 `true`）；置 `false` 则走原整段合成路径。**但要注意**：`tts.stream` 当前为 `null`（§1.7），配置读取必须容忍缺失（`.get("stream", {}) or {}`，既有代码已是这个写法）。
- B2：同一开关；B2 未实施则天然回退。

### 3.6 验收

**B1 可执行**：
- 新增 `tests/test_tts_sentence_fanout.py`：
  - mock 一个返回不同音频路径的假 TTS provider，投喂三句文本
  - 断言 `synthesize()` 被调用 **3 次**（而非 1 次），且每次文本是单句
  - 断言 `on_reply` 被调用 1 次（首句）、`on_tts_enqueue` 被调用 **2 次**
  - 断言第二句合成前 `_is_stale` 为真时，后续句不再合成
- 断言 `_split_tts_sentences` 对无标点长文本返回单元素（不强行切）

**B1 手动**：
- 说一句话让桌宠回一段 3 句以上的话 → 首音应在**第一句合成完**即出声，而非全文合成完
- 日志应出现多条 `TTS done: edge_xxx.mp3`（句数条），而非一条
- 600 字回复 → 内容完整读出

**B2 可执行**（若实施）：
- 断言第二句的合成启动时刻**早于**第一句的最后一个 chunk 被消费（需埋时间戳）
- 断言打断时预取线程被清理（无泄漏线程）

### 3.7 风险

| 风险 | 说明 | 缓解 |
|---|---|---|
| 口型与音频错位 | 分句后每句一个音频文件，`_current_audio_path` 随 `enqueue` 更新（`ui/tts_player.py:_play_file` 内），侧车按句加载 | 逐句验证；`word_timings` 侧车本就按文件生成，天然支持 |
| 情绪跨句不一致 | `instruct`/`emotion` 在整段内固定，分句后仍用同一份 | 无风险（同一回复共用同一 emotion） |
| 首句过短 | `_split_tts_sentences` 已有 `min_len=2` 合并逻辑，过短片段并入前句 | 既有逻辑，无需改 |
| 分句后单句合成失败 | 该句跳过，后续句仍走 enqueue，播放不断（§3.3.3 已验证） | 日志告警 + 继续 |
| B2 线程泄漏 | 预取线程未在打断时清理 | `finally: shutdown(wait=False)` + 队列哨兵 |

---

## 4. 两个方案的交叉影响

**B 会改变 A 的时机判断。**

方案 B 落地后，TTS 变成"多段、有句间空隙"。方案 A 的 `_bargein_frames >= 15` 判据在**句间空隙**期间会误判——用户在那 200ms 里说一个字，就会打断整条回复。

**缓解**：B 落地后，A 的阈值应改为"连续语音 ≥ 15 帧 **且** 当前句正在播"（而非仅 `is_playing()` 为真，因为 `is_playing` 在句间空隙可能仍为真——`enqueue` 的续播不触发 `on_end`）。

**建议**：**A 先做，B 后做**。A 在单段播放下逻辑简单；B 落地时同步复核 A 的判据。

---

## 5. 建议顺序

```
A0  启用声纹门卫（新增，优先级最高）
    ├─ 纯配置：登记 data/voiceprint/owner.wav + voiceprint.enabled = true
    ├─ 零代码，却同时改善"误触发识别"与降低 A 的副作用
    └─ 前置：确认设置面板的采集入口可用（ui/settings_dialog.py:1467）

A   持续监听 barge-in（两个改动点）
    ├─ 改动点 1：帧级 barge-in（~20 行）
    ├─ 改动点 2：ASR 完成后清理（~4 行，复核新增）
    ├─ 风险集中在"视频人声误打断"——已由 A0 降低
    └─ 需你拍板：持续监听的沉默是刻意的还是漏的？

B1  文件式分句 + enqueue 接线
    ├─ 当前配置（Edge）收益最大
    ├─ enqueue 已就绪，缺的只是引擎侧分句与接线
    └─ 顺带修掉 Edge 500 字截断

B2  流式预取（Qwen local）
    ├─ 前提是切到 Qwen local 模式
    ├─ 复杂度显著高于 B1（线程 + 队列）
    └─ 建议 B1 实测后再决定
```

**A0 与 A 有先后关系（A0 先）；A 与 B1 互不依赖，可并行。** B2 依赖 B1 的落地结论。

---

## 6. 未验证项（诚实标记）

1. **本方案未运行任何代码**。所有"现状"来自静态阅读，所有"效果"是设计推断。
2. ~~爱莉丝的独立复核结论尚未返回~~ → **已返回**（见 §1 各条与 §1.10）。总评可信度约 85%；**推翻了一处描述**（§1.1 的 `_stop_walking()`），并新增了 3 条我漏掉的关键路径。
3. **§1.9（声纹门卫）不在本次复核范围内**——爱莉丝未核这一节，**可信度低于已复核的各条**。
4. ~~`backend="whisper"` 与 `backend="faster_whisper"` 的能力差异~~ → **已核实**：`faster_whisper 1.2.1` 已安装，`openai-whisper` 也在；日志实测 `voice_input: Whisper 模型加载中... (small, backend=whisper)`。**不是依赖缺失导致的降级，是配置本身**。`faster-whisper` 分支多出 `vad_filter=True` 与 `avg_logprob/no_speech_prob` 置信度防御（`whisper_local.py:176-178`、`220-223`），openai 分支无。**实际影响未测**。
5. ~~15 帧 ≈ 480ms 的阈值是借 livekit 的默认值~~ → **已核实帧长**：Silero 窗口 512 样本 / 16000Hz = 32ms（`vad.py:56-57`），且与 `chat_mixin.py:192` 的「40 帧 ≈ 1.3s」交叉吻合。**但 `sd.Stream` 未指定 `blocksize`，实际回调间隔由驱动决定**——需首次实施时实测。
6. **B2 的线程模型未设计完整**——只给了方向（生产者-消费者 + 队列哨兵），未定队列容量、未处理背压。
7. **未跑过测试基线**。`tests/` 有 109 个文件、11 个语音相关（`test_asr1_silero_vad.py`、`test_lip1_lipsync.py`、`test_perf_fixes.py` 等）。**动手前应先跑一遍确认基线为绿。**
8. **三处默认值不一致**：`config.py:54` 与 `config.template.json` 都是 `faster_whisper`，但 `ui/settings_dialog.py:362` 的下拉标签写的是「whisper (默认)」且回退值取 `"whisper"`（`settings_dialog.py:365`）。实际 `config.json` 是 `whisper`。**无法判定是谁写进去的**（config.json 在 .gitignore 里，无历史），但下拉标签与实际代码默认相反，容易误导。
9. **`config.json` 不在 git 跟踪内**（`.gitignore:2`）——无法从历史追溯配置变更。本次分析期间它被改动过（mtime 2026-09-19 23:56）。
10. **声纹门卫的登记流程未实测**（§1.9）。
11. **复核新增的 3 处死代码未再交叉验证**（§1.10 第 2 项）——`_interrupt_event` / `_last_interrupt_*` / `reason="user_stop"` 由爱莉丝独立 grep 得出，可信度较高，但**未测其是否影响其他隐藏路径**。

---

## 7. 待你拍板

| # | 事项 | 选项 | 影响 |
|---|---|---|---|
| **D1** | 持续监听的"不打断"是刻意还是遗漏 | 刻意（防视频误触发）/ 遗漏 | 决定方案 A 是否要做、阈值取多保守 |
| **D2** | 先做 A 还是先做 B1 | 见 §5 | 两者独立，但 A0（声纹）建议无条件先做 |
| **D3** | `asr.backend` 是否切回 `faster_whisper` | 是 / 否 / 先实测对比 | **依赖已装（faster_whisper 1.2.1）、日志实测走 whisper**；切换即拿回 `vad_filter` + 置信度防御 |
| **D4** | B2 是否在本次范围 | 做 / 缓 | 复杂度差异大，且当前配置用不上 |
| **D5** | 是否启用声纹门卫（新增） | 启用 / 不启用 | 纯配置；模型已到位（28.3MB），只差主人样本 |

---

## 8. 实施记录（2026-09-19）

### 8.1 已完成

| 项 | 状态 | 位置 |
|---|---|---|
| **A0** 声纹门卫 | ⏸ **待用户**（需录主人样本） | 见 §8.3 |
| **A** 持续监听 barge-in（两个改动点） | ✅ **已实施** | `pet_mixins/chat_mixin.py` |
| **B1** 文件式分句 + enqueue | ⏸ 未开始 | — |
| **B2** 流式预取 | ⏸ 未开始 | — |

### 8.2 方案 A 实施细节

**基线**：改动前 `1652 passed, 1 skipped`（96s）。
**改动后**：`1669 passed, 1 skipped`（含新增 17 项），无回归。

**新增代码**（均在 `pet_mixins/chat_mixin.py`）：

| 符号 | 作用 |
|---|---|
| `_BARGEIN_FRAMES = 15` / `_BARGEIN_COOLDOWN_S = 1.0` | 默认阈值 |
| `_bargein_cfg_cache` | **进程级配置缓存**（见下） |
| `_bargein_config()` | 读 `asr.bargein.*`，带下限保护 |
| `_strip_comments()` | tokenize 去注释/docstring（供测试断言用） |
| `ChatMixin._maybe_bargein()` | 判据主体 |

**两个改动点**：
1. `_on_voice_vad` 的 `if is_speech:` 分支 → 调 `_maybe_bargein()`
2. `_do_continuous_asr` 闭包 → `eng.send()` 前 `tts_stop_signal.emit()`

### 8.3 实施期发现的三个问题（方案阶段未预见）

#### 问题 1：`is_playing()` 会碰 Qt（**原方案写错了**）

原方案把「当前在播 TTS」的判据写成 `is_playing()`。但 `_maybe_bargein` 跑在音频回调线程，而两个播放器的 `is_playing()` 都会调 `sink.state()` / `playbackState()`——**Qt 内部锁**。`streaming_pcm_player.py:358` 的 docstring 有完整事故记录。

**改为** `self._renderer._speaking`：`set_speaking()` 只是 `self._speaking = bool(...)` 纯 Python 赋值，从音频线程读安全。

#### 问题 2：配置每帧读盘（**性能陷阱**）

初版 `_bargein_config()` 每帧调 `load_config()`，而那个函数**每次都读文件**。音频回调每 ~32ms 一次 → 每秒读盘 30+ 次。

**改为**进程级缓存 `_bargein_cfg_cache`（改配置需重启生效，对 barge-in 参数可接受）。

#### 问题 3：短停顿未断连续（**逻辑漏洞**）

初版把 `_bargein_frames = 0` 只放在「静音且无语音段」的最内层分支。但静音超 40 帧（1.3s）才进那个分支——**用户中途换气（短停顿）时计数被保留**，两次断续的语音会被当成一次连续语音。

**改为**放在静音 `else` 的**开头**：任何静音帧都重置。

### 8.4 顺带修正：计数时机

初版先 `+1` 再判「是否在说话」。这会让「用户早在桌宠开口前就在说话」被算成连续语音，桌宠一开口就被打断。

**改为**先判 `_speaking`，未说话时**计数归零**。

### 8.5 测试

新增 `tests/test_bargein.py`（18 项）：

- 阈值行为：15 帧触发 / 14 帧不触发
- 冷却：期内不重复 / 过后可再触发
- 说话判据：未说话不触发 / 计数归零 / 无渲染器安全
- 静音重置 + **短停顿断连续**（回归保护）
- 配置：开关 / 自定义帧数 / 下限保护 / 缺失回退默认 / **缓存生效**
- 音频线程约束：`_maybe_bargein` 可执行代码里**不得出现** `_tts_player` / `_stream_player` / `is_playing`（用 tokenize 去注释后断言）
- 改动点 ② 接线：`emit` 必须在 `eng.send` **之前**
- 回归：按住说话的 barge-in 未被破坏

### 8.6 配置项

`config.template.json` 的 `asr` 段新增：

```json
"bargein": {
  "enabled": true,
  "frames": 15,
  "cooldown_seconds": 1.0
}
```

缺省即开启（保守阈值）。置 `"enabled": false` 可完全关掉，行为回到改动前。

**未写入实际 `config.json`**：缺省值已在代码里，不写也能工作；写入会改动用户配置（见 §8.7）。

### 8.7 未做与待确认

- **A0（声纹门卫）未做**：需一段用户本人的录音作 `data/voiceprint/owner.wav`。**这一步只能用户自己完成。**
- **未手动实测**：所有验证都是单元测试 + 静态检查。真实麦克风下的 480ms 手感、视频声是否误打断——**必须真机跑才能确认**。
- **未写 `config.json`**：那是用户的实际配置（且在 `.gitignore` 里），不属于本次改动范围。
- **工作树里有两个别人未提交的改动**（`ARCHITECTURE.md`、`docs/架构图.md`）——**未碰**。

---

*作者：奥菲莉娅（方案设计 + A 方案实施；现状核实：爱莉丝）*
*核实时间：2026-09-20 00:0x–02:0x（含依赖检查与日志实测）*
*实施时间：2026-09-20 02:0x（基线 1652 → 1669 passed）*
