# Hatch Pet — 桌宠精灵生成指南

基于 OpenAI Codex 的 hatch-pet 规范，适配 OC Desktop Pet。

## 规格

### Atlas 格式

精灵表是一张 8 列 × 9 行的网格图：

| 属性 | 值 |
|------|-----|
| 列数 | 8 |
| 行数 | 9 |
| 单格宽度 | 192px |
| 单格高度 | 208px |
| 总尺寸 | 1536×1872px |
| 背景 | 透明或纯色（便于抠图） |

### 9 种动画

| 行 | 名称 | 帧数 | 对应状态 | 说明 |
|----|------|------|---------|------|
| 0 | idle | 6 | 空闲 | 微呼吸、眨眼、轻微摇摆 |
| 1 | running-right | 8 | 向右移动 | 身体朝右的拖拽运动 |
| 2 | running-left | 8 | 向左移动 | 可镜像 running-right |
| 3 | waving | 4 | 打招呼 | 爪/手/翅膀抬起挥手 |
| 4 | jumping | 5 | 跳跃/悬停 | 预备→起跳→空中→落地→站稳 |
| 5 | failed | 8 | 失败/阻塞 | 垂头丧气、闭眼 |
| 6 | waiting | 6 | 等待输入 | 期待姿态、歪头 |
| 7 | working | 6 | 正在工作 | 专注、思考、打字（非跑步） |
| 8 | review | 6 | 审查完成 | 前倾检查、眯眼、歪头 |

### 状态驱动动画映射

```
agent 状态        →  动画
─────────────────────────
idle              →  row 0 (idle)
walking           →  row 1/2 (running-right/left)
greeting          →  row 3 (waving)
excited           →  row 4 (jumping)
error/failed      →  row 5 (failed)
waiting_input     →  row 6 (waiting)
thinking/working  →  row 7 (working)
done/review       →  row 8 (review)
```

## 生成流程

### 1. 概念图

生成一张角色概念图，确认风格后作为后续所有行的参考。

```
prompt: A cute [description] character for a desktop pet, [style] style.
        [颜色/特征/表情描述]. Clean transparent background, sprite-ready.
```

### 2. 行精灵条

以概念图为参考，逐行生成 8 帧精灵条：

```
prompt: Sprite sheet for [角色名], 8 frames in a horizontal row.
        [动画描述]. Same character as reference image.
        Clean transparent background, each frame clearly separated.
        Consistent size and style across all frames.
        No floating effects (speed lines, dust, stars, tears).
        No shadows (drop shadow, contact shadow, ground shadow).
        No text, labels, UI, or readable logos.
```

**生成顺序建议**：
1. idle（确认基础形象）
2. running-right（确认运动风格）
3. running-left（如可镜像则跳过）
4. waving → jumping → failed → waiting → working → review

### 3. running-left 处理

- 先检查 running-right 翻转后是否自然
- 自然：直接水平镜像（省一次生图）
- 不自然：单独生成

### 4. 组装 Atlas

将 9 行精灵条拼成 8×9 的 atlas：
- 每行 8 格，帧从左到右排列
- 不足 8 帧的行，右侧留空（透明）
- 背景统一透明

### 5. 验证

- 每格 192×208，角色居中
- 背景透明无残留
- 同行动画帧之间过渡自然
- 跨行角色外观一致

### 6. 打包

```
characters/<name>/
├── pet.json          # 角色配置
├── atlas.png         # 8×9 精灵表
├── concept.png       # 概念图（可选）
└── preview/          # QA 预览（可选）
    ├── contact-sheet.png
    └── motion-preview.gif
```

## pet.json 格式

```json
{
  "name": "角色名",
  "description": "一句话描述",
  "style": "auto",
  "atlas": {
    "src": "atlas.png",
    "columns": 8,
    "rows": 9,
    "cellWidth": 192,
    "cellHeight": 208
  },
  "animations": {
    "idle":          { "row": 0, "frames": 6, "fps": 3 },
    "running-right": { "row": 1, "frames": 8, "fps": 6 },
    "running-left":  { "row": 2, "frames": 8, "fps": 6 },
    "waving":        { "row": 3, "frames": 4, "fps": 4 },
    "jumping":       { "row": 4, "frames": 5, "fps": 5 },
    "failed":        { "row": 5, "frames": 8, "fps": 4 },
    "waiting":       { "row": 6, "frames": 6, "fps": 3 },
    "working":       { "row": 7, "frames": 6, "fps": 5 },
    "review":        { "row": 8, "frames": 6, "fps": 3 }
  },
  "emotions": {
    "happy":     { "anim": "waving" },
    "angry":     { "anim": "failed" },
    "surprised": { "anim": "jumping" },
    "thinking":  { "anim": "working" },
    "sad":       { "anim": "failed" },
    "neutral":   { "anim": "idle" }
  },
  "scale": 1.0
}
```

## 分帧模式（备选）

如果不使用 atlas，也可以用分帧目录结构：

```
characters/<name>/
├── pet.json
├── frames/
│   ├── idle/
│   │   ├── idle_0.png
│   │   ├── idle_1.png
│   │   └── ...
│   ├── running-right/
│   │   ├── running-right_0.png
│   │   └── ...
│   └── .../
└── preview/
```

对应 pet.json 格式：

```json
{
  "spritesheet": {
    "src": "frames/",
    "frameWidth": 192,
    "frameHeight": 208,
    "scale": 1.0
  },
  "animations": {
    "idle":          { "start": 0,  "count": 6, "fps": 3 },
    "running-right": { "start": 6,  "count": 8, "fps": 6 },
    "running-left":  { "start": 14, "count": 8, "fps": 6 },
    "waving":        { "start": 22, "count": 4, "fps": 4 },
    "jumping":       { "start": 26, "count": 5, "fps": 5 },
    "failed":        { "start": 31, "count": 8, "fps": 4 },
    "waiting":       { "start": 39, "count": 6, "fps": 3 },
    "working":       { "start": 45, "count": 6, "fps": 5 },
    "review":        { "start": 51, "count": 6, "fps": 3 }
  },
  "emotions": {
    "happy":     { "anim": "waving" },
    "angry":     { "anim": "failed" },
    "surprised": { "anim": "jumping" },
    "thinking":  { "anim": "working" },
    "sad":       { "anim": "failed" },
    "neutral":   { "anim": "idle" }
  }
}
```

---

## 风格规范

### 允许的风格
- pixel（像素）、plush（毛绒）、clay（粘土）、sticker（贴纸）
- flat-vector（扁平矢量）、3d-toy（3D 玩具）、painterly（绘画）
- auto（自动推断）

### 必须遵守
- 全身轮廓在 192×208 格内可读
- 跨行保持面部、比例、材质、配色一致
- 背景干净可移除（透明或纯色背景）
- 细节足够大，缩小后仍可辨认

### 禁止
- 文字、标签、UI、可读 logo
- 浮动特效（速度线、灰尘、星星、泪滴）
- 阴影（投影、接触影、地面影）
- 跨格的姿势重叠
- 残像素、断线、噪点
