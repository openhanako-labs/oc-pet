# AUDIO-07 ↔ PET-02 对接方案

> 桌宠音频事件桥接器与基础口型系统的集成说明

## 1. 架构概览

```
┌─────────────────────────────────────────────────────┐
│                   Hanako Audio Player                │
│                                                      │
│  audio-event-bus.js (AUDIO-05)                       │
│  ┌──────────┬──────────┬──────────┬──────────┐      │
│  │ play     │ pause    │ volume   │ end      │      │
│  └──────────┴──────────┴──────────┴──────────┘      │
│         │ CustomEvent('audio-event')                 │
└─────────┼────────────────────────────────────────────┘
          │
          ▼
┌─────────────────────────────────────────────────────┐
│              PetAudioBridge (AUDIO-07)               │
│                                                      │
│  AudioTypeDetector                                   │
│  ┌─────────────┬──────────────┬──────────────────┐  │
│  │ tts         │ music        │ notification     │  │
│  │ → 口型       │ → 闭嘴+摆动   │ → 瞬时反应        │  │
│  └─────────────┴──────────────┴──────────────────┘  │
│         │                                            │
│         ▼ PetAudioCallbacks                          │
└─────────┼────────────────────────────────────────────┘
          │
          ▼
┌─────────────────────────────────────────────────────┐
│                  PetWindow (pet.py)                  │
│                                                      │
│  on_tts_start()  → SpriteRenderer.play_anim(speak_*)│
│  on_tts_end()    → _set_anim_seq('idle')            │
│  on_music_start()→ 强制 idle（不触发嘴型）            │
│  on_notification()-> 可选 extra 帧                   │
│                                                      │
│  TTSTtsPlayer.on_start → _on_tts_start()            │
│  TTSTtsPlayer.on_end   → _on_tts_end()              │
└─────────────────────────────────────────────────────┘
          │
          ▼
┌─────────────────────────────────────────────────────┐
│              SpriteRenderer (PET-02)                 │
│                                                      │
│  play_anim('speak_open' / 'speak_half' /             │
│            'speak_closed')                           │
│  → 从 _frames 中选取对应帧序列                        │
│  → 动画定时器推进帧                                   │
└─────────────────────────────────────────────────────┘
```

## 2. 音频类型区分策略

### 2.1 检测优先级

| 优先级 | 来源 | 说明 |
|--------|------|------|
| 1 | `event.audioType` | 事件总线显式标记 |
| 2 | 文件名/路径关键词 | `tts/speech/voice/cosyvoice/mimo_tts` → TTS |
| 3 | `payload.mode/type` | 曲目元数据中的模式字段 |
| 4 | 默认 fallback | → `music` |

### 2.2 关键词表

**TTS 关键词**: `tts`, `speech`, `voice`, `合成`, `语音`, `cosyvoice`, `mimo_tts`, `api_tts`

**提示音关键词**: `notification`, `beep`, `click`, `ding`, `alert`, `提示音`, `通知`, `叮`, `滴`

**音乐**: 以上都不命中 → 默认为音乐

### 2.3 各类型的行为矩阵

| 事件 | TTS | Music | Notification |
|------|-----|-------|-------------|
| **play** | 张嘴 + 情绪映射 | 强制闭嘴 | 瞬时 extra 帧 |
| **pause** | 保持口型冻结 | 保持闭嘴 | — |
| **resume** | 重新张嘴 | 保持闭嘴 | — |
| **end** | 恢复 idle | 恢复正常 | 无操作 |
| **volume** | 更新显示 | 更新显示 | 更新显示 |

## 3. 防止音乐触发错误嘴型

### 3.1 核心机制

1. **互斥锁**: `_music_muted_speaking` 标志位
   - 音乐播放时设为 `True`
   - 音乐结束时重置为 `False`

2. **强制闭嘴**: `_force_mouth_closed()`
   - 调用 `TTSTtsPlayer.on_end` 回调
   - 将 `_mouth_state.state` 设为 `"speak_closed"`
   - 确保任何残留的说话帧被清除

3. **曲目切换保护**: `_handle_track_change()`
   - 旧类型为 TTS 时先恢复 idle
   - 新类型为音乐时强制闭嘴

### 3.2 时序保证

```
音乐播放:  [play music] → _force_mouth_closed() → [idle]
TTS 打断:  [play tts]   → on_tts_start(emotion) → [speak_open/half]
TTS 结束:  [end tts]    → on_tts_end() → [idle]
音乐继续:  保持 idle（不恢复说话）
音乐结束:  [end music]  → on_music_end() → [idle]
```

## 4. 与现有代码的对接点

### 4.1 TTSTtsPlayer (ui/tts_player.py)

已有接口无需修改：

```python
self._tts_player.on_start = self._on_tts_start  # 已存在
self._tts_player.on_end = self._on_tts_end      # 已存在
self._tts_player.on_error = lambda msg: self._on_tts_end()  # 已存在
```

桥接器在 `__init__` 中接管这些回调，旧方法改为兼容转发。

### 4.2 SpriteRenderer (avatar/sprite_renderer.py)

无需修改。`play_anim()` 已经支持 `speak_open` / `speak_half` / `speak_closed` 等序列名，
只要角色帧目录中有对应子目录即可。

### 4.3 HanakoMonitor (core/hanako_monitor.py)

无需修改。它通过 `_on_hanako_state` → `_tts_player.play(audio_path)` 驱动 TTS 播放，
桥接器在 `TTSTtsPlayer.on_start` 处自动捕获。

### 4.4 PetWindow (pet.py)

新增：
- 导入 `PetAudioBridge, PetAudioCallbacks, AudioType`
- `__init__` 中创建并连接桥接器
- 实现 `PetAudioCallbacks` 接口方法（8 个回调）
- 保留 `_on_tts_start` / `_on_tts_end` 作为兼容层
- `closeEvent` 中断开桥接器

## 5. 扩展点

### 5.1 节奏跟随（未来）

`_handle_progress` 中已有 beat_phase 计算框架，
可在 `_idle_wobble()` 中实现音乐节奏驱动的轻微摆动。

### 5.2 多桌宠支持

`PetAudioBridge` 设计为单实例，但 `PetAudioCallbacks` 接口可被多个桌宠实例实现，
桥接器通过注入不同 callbacks 实例来支持多桌宠。

### 5.3 调试面板

`get_status()` 返回当前桥接状态，可接入设置面板显示：
- 当前音频类型
- 口型状态
- 订阅数量
- 是否处于音乐静音状态
