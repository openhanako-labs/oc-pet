"""
OC-Claw SpriteSheet Builder
将现有逐帧 PNG 拼成 OC-Claw 所需的 8×9 精灵图集（1536×1872）。

用法: python build_spritesheet.py <角色名>
角色名: ophelia 或 yuexiye
"""

import os, sys, json
from PIL import Image

# ── 配置 ──────────────────────────────────────────────
CELL_W, CELL_H = 192, 208
COLS, ROWS = 8, 9
SHEET_W = CELL_W * COLS   # 1536
SHEET_H = CELL_H * ROWS   # 1872

# 每行需要的帧数（来自 codexPet.ts 的 ANIMATION_ROWS）
ROW_FRAMES = [6, 8, 8, 4, 5, 8, 6, 6, 6]

# 每行对应的状态名（仅注释用）
ROW_NAMES = ['idle', 'run-right', 'run-left', 'waving', 'jumping',
             'failed', 'waiting', 'running', 'review']

PET_META = {
    'ophelia': {
        'id': 'ophelia',
        'displayName': '奥菲莉娅 / Ophelia',
        'description': '黑白发，蓝色单片眼镜，幽蓝疏离',
        'kind': 'person',
    },
    'yuexiye': {
        'id': 'yuexiye',
        'displayName': '月曦夜 / Yuexiye',
        'description': '失忆的旅人，暗铜色单片眼镜，温和安静',
        'kind': 'person',
    },
}

# ── 工具函数 ──────────────────────────────────────────

def load_frames(char_dir: str, anim: str) -> list[Image.Image]:
    """加载某个角色的某个动画的所有帧，按文件名排序"""
    d = os.path.join(char_dir, 'frames', anim)
    files = sorted([f for f in os.listdir(d) if f.endswith('.png')])
    return [Image.open(os.path.join(d, f)).convert('RGBA') for f in files]


def fit_to_cell(img: Image.Image, target_w: int, target_h: int,
                padding: int = 8) -> Image.Image:
    """
    将图像等比缩放到适应 target_w×target_h 的区域内，并水平居中。
    保留透明背景。
    """
    # 计算缩放比例：优先撑满高度
    scale = (target_h - padding * 2) / max(img.height, 1)
    # 如果宽度超出，则改用宽度缩放
    if img.width * scale > target_w - padding * 2:
        scale = (target_w - padding * 2) / max(img.width, 1)

    new_w = max(1, int(img.width * scale))
    new_h = max(1, int(img.height * scale))
    resized = img.resize((new_w, new_h), Image.LANCZOS)

    # 居中放置
    canvas = Image.new('RGBA', (target_w, target_h), (0, 0, 0, 0))
    x = (target_w - new_w) // 2
    y = (target_h - new_h) // 2
    canvas.paste(resized, (x, y), resized)
    return canvas


def mirror(img: Image.Image) -> Image.Image:
    """水平镜像"""
    return img.transpose(Image.FLIP_LEFT_RIGHT)


def pick_frame(frames: list[Image.Image], idx: int) -> Image.Image:
    """循环取帧"""
    return frames[idx % len(frames)]


# ── 行生成器 ──────────────────────────────────────────

def build_row(frames_idle: list[Image.Image],
              frames_walk: list[Image.Image],
              frames_extra: list[Image.Image],
              row_idx: int, count: int) -> list[Image.Image]:
    """
    为指定行生成 count 帧。
    根据行号分配不同的动画源。
    """
    cells: list[Image.Image] = []

    if row_idx == 0:       # idle：4帧循环
        for i in range(count):
            cells.append(pick_frame(frames_idle, i))

    elif row_idx == 1:     # run-right：walk 帧 + 镜像形成完整步态循环
        for i in range(count):
            if i < 4:
                cells.append(pick_frame(frames_walk, i))
            else:
                cells.append(mirror(pick_frame(frames_walk, i - 4)))

    elif row_idx == 2:     # run-left：run-right 全部镜像
        for i in range(count):
            src = pick_frame(frames_walk, i % 4) if i < 4 else mirror(pick_frame(frames_walk, i - 4))
            cells.append(mirror(src))

    elif row_idx == 3:     # waving：extra 帧
        for i in range(count):
            cells.append(pick_frame(frames_extra, i))

    elif row_idx == 4:     # jumping：extra + idle 混合，带轻微上移
        for i in range(count):
            if i < 2:
                cells.append(pick_frame(frames_extra, i))
            elif i < 4:
                cells.append(pick_frame(frames_idle, i))
            else:
                cells.append(pick_frame(frames_extra, 0))

    elif row_idx == 5:     # failed：idle 和 extra 交错
        for i in range(count):
            cells.append(pick_frame(frames_extra if i % 2 == 0 else frames_idle, i // 2))

    elif row_idx == 6:     # waiting：idle 帧，略微放慢（直接用 idle）
        for i in range(count):
            cells.append(pick_frame(frames_idle, i))

    elif row_idx == 7:     # running：walk 帧
        for i in range(count):
            cells.append(pick_frame(frames_walk, i))

    elif row_idx == 8:     # review：idle 帧
        for i in range(count):
            cells.append(pick_frame(frames_idle, i))

    return cells


# ── 主流程 ────────────────────────────────────────────

def main(char_name: str):
    base_dir = os.path.dirname(os.path.abspath(__file__))
    char_dir = os.path.join(base_dir, 'characters', char_name)

    if not os.path.isdir(char_dir):
        print(f'错误：找不到角色目录 {char_dir}')
        sys.exit(1)

    # 加载帧
    print(f'加载 {char_name} 帧...')
    frames_idle  = [fit_to_cell(f, CELL_W, CELL_H) for f in load_frames(char_dir, 'idle')]
    frames_walk  = [fit_to_cell(f, CELL_W, CELL_H) for f in load_frames(char_dir, 'walk')]
    frames_extra = [fit_to_cell(f, CELL_W, CELL_H) for f in load_frames(char_dir, 'extra')]
    print(f'  idle: {len(frames_idle)}帧, walk: {len(frames_walk)}帧, extra: {len(frames_extra)}帧')

    # 创建 spritesheet
    sheet = Image.new('RGBA', (SHEET_W, SHEET_H), (0, 0, 0, 0))

    for row in range(ROWS):
        count = ROW_FRAMES[row]
        cells = build_row(frames_idle, frames_walk, frames_extra, row, count)
        for col, cell in enumerate(cells):
            x = col * CELL_W
            y = row * CELL_H
            sheet.paste(cell, (x, y), cell)
        print(f'  行{row} ({ROW_NAMES[row]}): {count}帧')

    # 保存
    out_dir = os.path.join(base_dir, 'output')
    os.makedirs(out_dir, exist_ok=True)

    png_path = os.path.join(out_dir, f'{char_name}-spritesheet.png')
    webp_path = os.path.join(out_dir, f'{char_name}-spritesheet.webp')
    json_path = os.path.join(out_dir, f'{char_name}', 'pet.json')

    sheet.save(png_path)
    sheet.save(webp_path, lossless=True)
    print(f'\nPNG:  {png_path}')
    print(f'WebP: {webp_path}')

    # pet.json
    meta = PET_META[char_name]
    pet_data = {
        'id': meta['id'],
        'displayName': meta['displayName'],
        'description': meta['description'],
        'spritesheetPath': f'{char_name}-spritesheet.webp',
        'kind': meta['kind'],
    }

    pet_dir = os.path.join(out_dir, char_name)
    os.makedirs(pet_dir, exist_ok=True)
    with open(os.path.join(pet_dir, 'pet.json'), 'w', encoding='utf-8') as f:
        json.dump(pet_data, f, ensure_ascii=False, indent=2)
    print(f'JSON: {os.path.join(pet_dir, "pet.json")}')

    print(f'\n完成！')


if __name__ == '__main__':
    if len(sys.argv) != 2:
        print('用法: python build_spritesheet.py <ophelia|yuexiye>')
        sys.exit(1)
    main(sys.argv[1])
