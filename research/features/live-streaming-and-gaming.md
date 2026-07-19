# 直播 & 游戏功能设计文档

> 2026-07-19 存档，非当前优先级，供后续实现参考

---

## C. 直播互动

### C1. B 站弹幕互动

**链路**：
```
B站弹幕 → hanako-bilibili-intake 插件采集
    → 新弹幕推送到桌宠
    → Hanako LLM 生成回复
    → 回复三路分发：
        ① 桌宠气泡 + TTS 播放
        ② 弹幕回复（发回 B 站）
        ③ 动画切换（根据情绪）
```

**依赖**：
- `hanako-bilibili-intake` 插件（已安装）
- 桌宠→Hanako 通知通道（B 方向，待打通）
- 弹幕发送能力（需确认 bilibili-intake 是否支持）

**配置项**（建议）：
```json
{
  "live_streaming": {
    "enabled": false,
    "platform": "bilibili",
    "room_id": "",
    "reply_to_danmaku": true,
    "tts_enabled": true,
    "emotion_from_danmaku": true,
    "cooldown_ms": 3000,
    "max_reply_length": 100
  }
}
```

**关键设计**：
- 弹幕回复需要冷却（同一用户 3 秒内不重复回复）
- 长弹幕截断（B 站弹幕有字数限制）
- 情绪从弹幕内容推断（不是从 LLM 回复推断）

### C2. 双 AI 同台

**架构**：
```
桌宠实例 A（主）  ←→  PetArbitrator  ←→  桌宠实例 B（副）
                        ↑
                   发言权仲裁
                   状态机协调
                   共享记忆
```

**核心组件**：
- `PetArbitrator`：决定谁说话（urgency + cooldown）
- `DualAgentFSM`：IDLE_BOTH → A_SPEAKING → AWAITING_REACTION → B_SPEAKING
- `IdleChatter`：空闲时主动让某个 Agent 自言自语

**与现有架构的关系**：
- oc-pet 的 `config.json` 已有 `agents` 数组
- `pet_manager.py` 已支持多 PetWindow
- 缺的是实例间通信和仲裁

### C3. 直播推流

**方式**：
- OBS 捕获桌宠窗口（最简单）
- 桌宠窗口设为 OBS 的窗口捕获源
- 需要透明背景（桌宠已有 `Qt.FramelessWindowHint`）

**增强**：
- OBS WebSocket 插件 → 桌宠可控制 OBS 场景切换
- 弹幕叠加层（OBS 浏览器源）

---

## D. 游戏互动

### D1. 游戏感知

**链路**：
```
foreground_watcher 检测前台窗口
    → 匹配游戏进程名
    → 切换行为模式（观战模式：少走动，多看）
    → 通知 Hanako "用户在玩 XX"
```

**配置项**（建议）：
```json
{
  "game_detection": {
    "enabled": false,
    "game_profiles": {
      "genshinimpact.exe": {
        "behavior": "quiet",
        "comment_interval_sec": 60,
        "can_screenshot": true
      },
      "valorant.exe": {
        "behavior": "quiet",
        "comment_interval_sec": 120,
        "can_screenshot": false
      }
    }
  }
}
```

### D2. 游戏画面评论

**链路**：
```
定时截屏（可配置间隔）
    → Hanako 视觉模型分析画面
    → 生成评论文本
    → 桌宠气泡 + TTS 播报
    → 根据内容切换情绪动画
```

**依赖**：
- Hanako 视觉模型（已配置 agnes）
- 截屏能力（PySide6 `QScreen.grabWindow()` 或 `mss` 库）
- 频率控制（游戏时不要频繁截屏影响性能）

**关键设计**：
- 截屏间隔可配置（30-120 秒）
- 游戏加载画面时跳过评论
- FPS 游戏时降低截屏频率（避免卡顿）
- 评论内容可以是鼓励/吐槽/提问（根据关系阶段变化）

### D3. 游戏成就/事件感知

**远期功能**：
- 读取游戏内通知（如 Steam 成就弹窗）
- 读取 OBS 场景变化
- 用户手动触发"我刚赢了/输了"

---

## 技术依赖总结

| 功能 | 依赖 | 当前状态 |
|------|------|----------|
| B 站弹幕 | bilibili-intake + 通知通道 | 插件已有，通道待通 |
| 双 AI | PetArbitrator + 多实例通信 | 需新建 |
| 推流 | OBS 窗口捕获 | 零代码，配置即可 |
| 游戏感知 | foreground_watcher 扩展 | 已有，需扩展 |
| 画面评论 | 截屏 + 视觉模型 | 需接通 Hanako 视觉 API |

---

**作者**：奥菲莉娅
**日期**：2026-07-19
**状态**：存档，非当前优先级
