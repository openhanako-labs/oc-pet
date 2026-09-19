# VRM（3D）渲染

> 2026-09-19 落地。此前 `avatar/vrm_renderer.py` 是空壳（`load()` 直接返回 False）。

## 一句话

角色目录里放一个 `.vrm`，桌宠就用 **QWebEngineView + three.js + @pixiv/three-vrm**
把它渲染成真正的 3D 模型——有归一化人形骨骼、MToon 材质、弹簧骨骼（头发/裙摆晃动）
和 52 个 ARKit/VRM 预设表情通道。

## 为什么是浏览器，而不是自己写 OpenGL

VRM 1.0 的规范细节（humanoid 归一化骨骼、springBone、MToon、表情绑定）都在
`@pixiv/three-vrm` 里，它是这个格式的事实标准实现。自写渲染管线等于把规范重做一遍，
而且永远落后于它。所以 Python 这边只做两件事：**给出模型路径**、**每帧把参数喂进去**。

代价是内存：WebEngine 进程 150~300MB，比 Live2D 重。因此它只在角色显式声明
`format: "vrm"`（或目录里确实有 `.vrm`）时才会被工厂选中。

## 怎么用

```
characters/
  my_avatar/
    pet.json          # { "format": "vrm", ... }
    my_avatar.vrm     # 模型本体（也可放在 vrm/ 子目录）
```

- `pet.json` 里写 `"format": "vrm"` 最明确；不写也可以——目录里出现 `.vrm` 就会被
  自动识别（见 `avatar/factory.detect_format`）。
- 模型查找顺序：角色目录根 → `vrm/` 子目录 → 递归兜底（`find_vrm_model`）。
- 设置面板里切换角色前会做资源预检（`factory.resource_available`）：没有 `.vrm`
  的角色不会被允许切过去，避免白屏。

## 依赖

| 依赖 | 说明 |
|---|---|
| `PySide6-Addons` | 提供 `QtWebEngineWidgets` / `QtWebChannel`（已在 `requirements.txt`） |
| `avatar/vrm_view/vendor/` | three.js 0.180.0 + @pixiv/three-vrm 3.5.5 的本地副本，离线可用；版本与刷新方式见该目录 `README.md` |

不需要 npm、不需要联网、不需要构建步骤。

## 桌宠参数怎么进 3D

| 桌宠侧 | JS 侧 | 说明 |
|---|---|---|
| `set_emotion(name, i)` | `setEmotion(name, i)` | 映射 VRM 预设：happy / angry / sad / relaxed / surprised / neutral |
| `apply_action_intent({"params": …})` | `setExpression(…)` | Live2D 参数名映射成 VRM 表情：`ParamMouthOpenY→aa`、`ParamEyeLOpen→blink`（负值取绝对值，因为 Live2D 用负值表示"闭"，VRM 用 0） |
| `apply_action_intent({"va": [x, y]})` | `va_x` / `va_y` | 连续 VA 坐标拆成两个自定义表情通道（模型没有这两个表情时 JS 侧安全忽略） |
| 口型（TTS 电平 / LIP-1 时间轴） | `setSpeaking(on)` + `setMouth(level)` | 说话态每帧插值到 `aa` 表情；非说话态直写 |
| `look_at(x, y)` | `lookAt(nx, ny)` | 屏幕坐标归一化后驱动 `vrm.lookAt.target` |
| `set_alpha(a)` | `setAlpha(a)` | **走页面 CSS opacity**——`QWebEngineView` 是子控件，`setWindowOpacity` 对它无效 |
| `set_facing(right)` | `setFacing(right)` | 镜像相机 |

眨眼是 JS 侧自带的简易定时器（VRM 模型不自带自动眨眼）。

## 上报通道（JS → Python）

两条并存，谁先到用谁：

1. **QWebChannel**：页面加载 `qrc:///qtwebchannel/qwebchannel.js`，把 `reportReady` /
   `reportError` / `log` 打到 Python 的 `VrmBridge`。
2. **轮询后备**：每 400ms 读一次 `window.VRM.status()`。当页面拿不到
   `qwebchannel.js`（Qt 资源未注册、或用外部浏览器打开页面）时，仍有完整状态。

页面里的 JS 异常也记进 `status().events`（`js-error:` / `js-reject:` 前缀），
否则模块导入失败会表现为"永远白屏但没有任何报错"。

## 验证

```powershell
python tools/verify_vrm_view.py                 # 真渲染一次并截图（默认用样片）
python tools/verify_vrm_view.py --model x.vrm --emotion happy --mouth 0.8 --out shot.png
```

它起真实的 QWebEngineView、载入模型、等就绪、驱动一次表演，然后从**页面内的
`canvas.toDataURL()`** 取一帧 PNG（比抓 `QWidget` 可靠——WebEngine 的合成面
常常抓不到）。退出码 0 = 成功出图。

纯逻辑单测（不需要模型、不装配 WebEngine）：

```powershell
python -m pytest tests/test_vrm_renderer.py -q
```

样片模型（10.7MB，仅本地验证用，不入库——`data/` 在 `.gitignore` 里）：

```
data/models/vrm_sample/VRM1_Constraint_Twist_Sample.vrm
```
来源：pixiv/three-vrm 仓库的官方示例模型。

## 已知边界

- **没有 motion 文件**：VRM 侧没有"走路/挥手"这类骨骼动画剪辑。`gesture` 名只映射到
  表情；要做动作得自己写骨骼动画或接 Mixamo 之类的剪辑（未做）。
- **Live2D 专属参数无对应**：`ParamAngleX/Y`（头部朝向）、`ParamBodyAngleZ` 等
  在 VRM 里要驱动骨骼，当前忽略。
- **WebEngine 缺失即降级**：导入失败时显示占位 label + 原因，不崩、不影响其他渲染格式。
- **首帧开销**：WebEngine 进程冷启动 + 解析 10MB 模型需要几秒；`load()` 是异步的，
  就绪通过 `on_ready(cb)` / `is_ready` 观察（`wait_until_ready` 只给测试用，
  生产路径不要自旋事件循环）。
