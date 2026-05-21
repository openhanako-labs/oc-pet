---
name: hanako-desktop-companion
description: 桌宠消息桥接器。连接 Hanako Agent 与 OC 桌面宠物，支持双向消息通信和动画控制。
---

# Hanako Desktop Companion

桌宠消息桥接器。连接 Hanako Agent 与 OC 桌面宠物，实现双向通信。

## 工具

### `companion_outbox`
读取桌宠发来的待处理消息。

**参数：**
- `markAsRead`（可选，默认 true）— 读取后是否清空 outbox

**返回：** 消息列表 `{messages: [{text, time, character}], count}`

### `companion_send`
向桌宠发送消息。

**参数：**
- `text`（必填）— 发送内容
- `character`（可选）— 指定回复的角色 ID
- `anim`（可选）— 触发动画状态：idle / extra / walk

**返回：** `{sent: true, text, character, anim}`

## 数据路径

所有数据文件位于 `~/.hanako/plugins/hanako-desktop-companion/`：
- `outbox.json` — 桌宠→Agent 的消息队列
- `response.json` — Agent→桌宠的回复

## 工作流程

1. 桌宠发送消息 → 写入 `outbox.json` + HTTP 通知 Hanako
2. Agent 调用 `companion_outbox` 获取消息
3. Agent 处理并调用 `companion_send` 回复
4. 桌宠轮询 `response.json` 获取回复并显示
