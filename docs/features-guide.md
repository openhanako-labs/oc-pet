# 五大借鉴模块使用指南

> 从 Fritia_Online_NEXT 借鉴的功能，已集成到 OC Desktop Pet。

---

## 📋 快速检查

从你的启动日志看，所有模块都初始化成功了：

```
✅ MultiPetBridge 启动成功
✅ EnhancedEnvironmentScanner 初始化成功
✅ MemorySnapshotManager 初始化成功
✅ NarrativeEngine 启动成功
✅ 叙事事件已生成："你回来了！等你好久了～"
```

---

## 🎯 功能使用方法

### M1: 叙事引擎（自动生成桌面小事件）

**触发方式**：自动（每 10 分钟尝试一次）

**查看效果**：
- 日志中看 `Narrative event generated` 消息
- 桌宠气泡会显示叙事文本

**配置**（config.json）：
```json
{
  "narrative": {
    "enabled": true,
    "cooldown_minutes": 15,
    "prefer_local_template": false
  }
}
```

**手动测试**：
```python
# 在 Python 控制台中
from core.narrative_engine import NarrativeEngine
engine = NarrativeEngine("yuexinmiao")
event = engine.request_event()
print(event)
```

---

### M2: 环境感知（识别你正在用什么应用）

**触发方式**：自动（每次构建上下文时）

**查看效果**：
- 日志中看环境感知信息
- 叙事事件会引用你的当前活动（如"看到你在写代码"）

**手动测试**：
```python
from core.enhanced_environment import EnhancedEnvironmentScanner
scanner = EnhancedEnvironmentScanner()
snapshot = scanner.scan(window_title="main.py - VS Code")
print(f"应用: {snapshot.foreground_app}")
print(f"类别: {snapshot.category}")
print(f"文件: {snapshot.detected_files}")
```

---

### M3: 记忆快照（导出/导入记忆）

**使用方式**：通过代码调用

**导出记忆**：
```python
from core.memory_snapshot import MemorySnapshotManager
mgr = MemorySnapshotManager("yuexinmiao")
path = mgr.export_snapshot(description="测试导出")
print(f"导出到: {path}")
```

**导入记忆**：
```python
from core.memory_snapshot import MemorySnapshotManager
mgr = MemorySnapshotManager("yuexinmiao")
result = mgr.import_snapshot("path/to/snapshot.json", strategy="smart")
print(result)
```

**合并策略**：
- `overwrite`: 完全覆盖
- `smart`: 智能合并（去重追加）
- `skip_existing`: 跳过已存在的

---

### M4: 多宠协作（多个桌宠互动）

**前提**：需要运行多个桌宠实例

**查看效果**：
- 日志中看 `[MultiPetBridge]` 消息
- 多个桌宠会自动聊天

**手动测试**：
```python
from core.multi_pet_bridge import MultiPetBridge, PetEvent
bridge = MultiPetBridge()
bridge.start()

# 注册两个桌宠
bridge.register_pet("ophelia")
bridge.register_pet("rebecca")

# 发送跨宠消息
bridge.send_chat(from_agent="ophelia", text="嘿，你在忙什么？")

# 生成协作事件
bridge.generate_social_event()
```

**协作场景**（自动触发）：
- 送水事件：A 桌宠让 B 桌宠给用户倒水
- 一起玩耍：用户玩游戏时，桌宠们一起评论
- 深夜闲聊：深夜两个桌宠偷偷聊天
- 互相吐槽：用户不在时，桌宠们闲聊吐槽
- 合作关怀：多个桌宠分工关心用户
- 礼物接力：节日时，桌宠们接力送祝福

---

### M5: 角色包（打包和分享角色）

**使用方式**：设置面板 → 角色包标签页

**操作步骤**：

1. **打开设置**：右键桌宠 → 设置

2. **切换到"角色包"标签页**

3. **导入角色包**：
   - 点击"📦 导入 .pet"
   - 选择 .pet 文件
   - 角色会自动安装到 `characters/` 目录

4. **导出角色包**：
   - 在列表中选中一个角色
   - 点击"💾 导出选中"
   - 选择保存位置

5. **卸载角色包**：
   - 在列表中选中一个角色
   - 点击"🗑️ 卸载选中"

**角色包格式**：
```
角色名.pet (zip 格式)
├── manifest.json      # 元数据
├── identity.md        # 角色身份
├── awareness.md       # 意识文件
├── model.json         # 模型配置
├── sprites/           # 精灵图（可选）
└── memory/            # 记忆（可选）
```

---

## 🔧 常见问题

### Q: 叙事事件没有显示？
A: 检查日志中是否有 `Narrative event generated`。如果没有，可能是：
- 冷却时间未到（默认 15 分钟）
- LLM 不可用（会自动降级到本地模板）

### Q: 环境感知没有识别到应用？
A: 检查窗口标题格式是否正确。支持的格式：
- `filename.ext - AppName`
- `AppName - filename.ext`
- `AppName`

### Q: 角色包导入失败？
A: 检查 .pet 文件是否包含 `manifest.json`，且必填字段（name、agent_id）是否完整。

### Q: 多宠协作没有触发？
A: 需要至少运行 2 个桌宠实例。协作事件有 30 分钟冷却时间。

---

## 📊 日志说明

| 日志前缀 | 说明 |
|----------|------|
| `[MultiPetBridge]` | 多宠协作模块 |
| `NarrativeEngine` | 叙事引擎 |
| `EnhancedEnvironmentScanner` | 环境感知 |
| `MemorySnapshotManager` | 记忆快照 |
| `CharacterPackageManager` | 角色包管理 |

---

## 🎨 自定义配置

在 `config.json` 中添加以下配置：

```json
{
  "narrative": {
    "enabled": true,
    "cooldown_minutes": 15,
    "max_recent_events": 20,
    "prefer_local_template": false
  },
  "multi_pet": {
    "enabled": true,
    "queue_size": 256,
    "social_event_interval": 1800
  }
}
```

---

## 📝 下一步

1. **等待叙事事件触发**（每 10 分钟尝试一次）
2. **打开设置面板**，查看角色包管理
3. **导出一个记忆快照**，备份你的回忆
4. **运行多个桌宠**，体验多宠协作

有问题随时问我！
