# OC Desktop Pet Companion — Agent 使用指南

> 适用于通过 hanako-desktop-companion 插件与桌宠通信的 Agent

---

## 桌宠消息来源

### 1. 用户通过桌宠输入 (companion_outbox)

桌宠用户通过输入框发送的消息会写入：
```
~/.hanako/plugins/hanako-desktop-companion/outbox.json
```

消息格式：
```json
{
  "text": "用户消息内容",
  "character": "ophelia",
  "time": 1234567890.123
}
```

**或动作消息**（用户点击右键菜单动作项）：
```json
{
  "type": "action",
  "action": "tea",
  "label": "一起喝茶",
  "emoji": "🍵",
  "character": "ophelia",
  "time": 1234567890.123
}
```

### 2. 如何读取

Agent 回复桌宠前，应先检查 outbox：

1. 读取 `outbox.json` 全部消息
2. 如果发现 `type: "action"` 消息，先处理动作事件
3. 普通 `text` 消息按正常对话处理

### 3. 消息压缩（TokenJuice 规则）

当 outbox 积压超过 3 条时，压缩后再送入 Agent prompt：

```
你有 5 条新消息：
[12:30] 在吗
[12:31] 今天天气不错
… 还有 2 条未显示
[最新] 来一起喝茶吧
```

压缩函数：`compress_messages(messages: list[dict]) -> str`
（3 条以内直接原文传递，超过 3 条只显示首 2 条 + 计数 + 最新 1 条）

### 4. 回复桌宠 (companion_send)

Agent 向桌宠发送回复：
```json
{
  "reply": "回复文字内容",
  "anim": "idle",
  "emotion": "neutral",
  "audioPath": "C:/path/to/audio.wav"
}
```

写入 `~/.hanako/plugins/hanako-desktop-companion/response.json`

桌宠端通过 HTTP 轮询（50ms）或 WebSocket（`ws://localhost:19900/companion`）实时接收。

### 5. 动作事件处理示例

当收到动作消息时，Agent 应该：
- 确认用户的动作意图
- 给出相应回复
- 可以触发新的动作作为回应

```
用户发送：一起喝茶
→ 你收到 {"type": "action", "action": "tea", ...}
→ 回复："好呀，正好我泡了龙井～"
→ 可选：再次触发动作 {"type": "action", "action": "tea", ...}
```

---

## 状态同步

### 6 态情绪（可用于 anim/emotion 字段）

| emotion | 说明 | 动画表现 |
|---------|------|----------|
| idle | 默认空闲 | 浮空轻晃 |
| thinking | Agent 思考中 | 歪头、眼神游移 |
| talking | Agent 流式输出 | 口型微动 |
| working | 执行工具 | 专注表情 |
| happy | 完成/被夸奖 | 跳跃、亮色 |
| error | 连接失败 | 抖动、暗色 |

---

## 注意事项

1. **及时清理**：读取 outbox 后建议清空或标记已处理
2. **不要遗漏**：每次回复前都应检查 outbox，特别是 `type: "action"` 消息
3. **简短回复**：桌宠气泡显示空间有限，保持回复在 50 字以内
4. **动作优先**：动作消息优先级高于普通对话
5. **消息压缩**：outbox 积压超过 3 条时用 `compress_messages` 压缩，省 token