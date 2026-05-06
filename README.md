# OC 桌面宠物 — 月曦夜 & 奥菲莉娅

基于 PySide6 的透明桌面宠物，支持对话。

## 首次使用

### 1. 安装依赖

```bash
pip install PySide6 requests Pillow
```

### 2. 配置 API Key

启动后 **右键 → 设置**，填入：
- **API 地址**：DeepSeek 用 `https://api.deepseek.com/v1`，其他厂商填对应的兼容地址
- **API Key**：你的 key
- **模型**：如 `deepseek-chat`

充一块钱能用很久。

### 3. 启动

双击 `启动桌宠.bat`，或终端执行：

```bash
python main.py
```

## 操作

| 操作 | 效果 |
|------|------|
| 左键点击角色 | 打开/关闭输入框 |
| 输入文字按回车 | 发送对话 |
| 拖拽空白区域 | 移动窗口 |
| 右键 | 切换角色 / 设置 / 退出 |

## 角色

- **月曦夜**：失忆的旅人，暗铜色单片眼镜，温和安静
- **奥菲莉娅**：黑白发，蓝色单片眼镜，幽蓝疏离

## 文件结构

```
oc-pet/
├── main.py              # 启动入口
├── pet.py               # 桌面宠物窗口
├── api.py               # LLM API 客户端
├── config.py            # 配置管理
├── 启动桌宠.bat         # 快捷启动
├── characters/
│   ├── yuexiye/idle.png
│   └── ophelia/idle.png + stand.png
```

## 自定义

想换角色图，把对应的透明 PNG 替换到 `characters/角色名/idle.png` 即可。
