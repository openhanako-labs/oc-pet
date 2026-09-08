"""渲染工厂 - 按角色目录格式选择渲染器

检测规则（在 characters/<id>/ 下）：

    格式优先级：
      1. pet.json 中的 "format" 字段（显式声明，最权威）：
           - "q6"     -> Q6 帧精灵（本项目原生格式，atlas / 精灵图网格）
           - "live2d" -> Live2D (Cubism)
           - "vrm"    -> VRM（3D，未来扩展钩子，尚未实现）
      2. 目录结构推断（无 format 字段时）：
           - live2d/*.model3.json 或 *.model.json -> Live2D (Cubism)
           - *.vrm (含 vrm/ 子目录)               -> VRM（3D，未来钩子）
           - 其他                                  -> Q6 帧精灵（原生格式）

重要区分：
    Q6 是本桌宠项目的【原生 2D 帧精灵格式】（精灵图 + 网格 pet.json，
    见 phoebe / yuexinmiao），由 SpriteRenderer 渲染。
    Q6 与 VRM 是两件不同的事：VRM 是独立的 3D 模型格式，目前仅作未来
    扩展钩子保留，尚未实现真实渲染。

live2d-py 未安装 / Live2DRenderer 构造失败时，自动回退到 Q6 SpriteRenderer，
保证 pet 始终能跑。
"""
from __future__ import annotations

import json
import logging
import os

from avatar.base import AvatarRenderer

logger = logging.getLogger(__name__)

# pet.json "format" 字段 -> 内部渲染格式标记
_FORMAT_MAP = {
    "q6": "sprite",
    "sprite": "sprite",
    "live2d": "live2d",
    "vrm": "vrm",
}


def _read_format_field(character_id: str):
    """读取 characters/<id>/pet.json 的 "format" 字段；无则返回 None。

    用于让角色显式声明自己的格式（如 Q6 / live2d / vrm），
    避免仅靠目录结构推断造成的歧义。
    """
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    pet_json = os.path.join(base, "characters", character_id, "pet.json")
    if not os.path.isfile(pet_json):
        return None
    try:
        with open(pet_json, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("format")
    except Exception as e:
        logger.warning("读取 %s 的 format 字段失败：%s", pet_json, e)
        return None


def detect_format(character_id: str) -> str:
    """返回 'live2d' | 'vrm' | 'sprite'(Q6 原生格式)。"""
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    char_dir = os.path.join(base, "characters", character_id)
    if not os.path.isdir(char_dir):
        return "sprite"

    # 1) 优先用 pet.json 的 format 字段（显式声明）
    fmt_field = _read_format_field(character_id)
    if fmt_field and str(fmt_field).lower() in _FORMAT_MAP:
        return _FORMAT_MAP[str(fmt_field).lower()]

    # 2) 否则按目录结构推断
    # Live2D：live2d/ 子目录下有 .model3.json (Cubism3+) 或 .model.json (Cubism2)
    live2d_dir = os.path.join(char_dir, "live2d")
    if os.path.isdir(live2d_dir):
        for f in os.listdir(live2d_dir):
            low = f.lower()
            if low.endswith(".model3.json") or low.endswith(".model.json"):
                return "live2d"

    # VRM：任意位置有 .vrm（未来 3D 扩展钩子，当前未实现真实渲染）
    for root, _, files in os.walk(char_dir):
        for f in files:
            if f.lower().endswith(".vrm"):
                return "vrm"

    # 其余：Q6 原生帧精灵格式（默认路径）
    return "sprite"


def create_renderer(character_id: str, parent, override_format: str = None) -> AvatarRenderer:
    """为角色创建合适的渲染器实例（不调用 load）。

    渲染格式优先级：
    1. 角色有 live2d 资源 → live2d（最高优先级，避免用户 override 导致 Live2D 角色白屏）
    2. override_format（用户手动指定，非 "auto"）
    3. pet.json "format" 字段（角色显式声明）
    4. 目录结构推断（live2d/ → live2d, 其他 → sprite）
    """
    # P2 Fix: 如果角色有 live2d 资源，优先使用 live2d（避免 sprite override 导致白屏）
    auto_fmt = detect_format(character_id)
    if auto_fmt == "live2d":
        fmt = "live2d"
        if override_format and override_format != "auto" and override_format != "live2d":
            logger.warning(
                "create_renderer('%s'): 用户指定 %s，但角色有 live2d 资源，强制使用 live2d",
                character_id, override_format,
            )
    elif override_format and override_format != "auto":
        fmt = override_format
        logger.info("create_renderer('%s') -> format=%s (user override)", character_id, fmt)
    else:
        fmt = auto_fmt
        logger.info("create_renderer('%s') -> format=%s (auto)", character_id, fmt)

    # P0 调试：OC_DISABLE_LIVE2D=1 时强制走 Q6 精灵渲染器，
    # 完全不 import live2d-py，用于二分法隔离"live2d C 层是否引发 0x8001010d"。
    import os as _os
    if _os.environ.get("OC_DISABLE_LIVE2D", "") == "1" and fmt == "live2d":
        logger.warning("OC_DISABLE_LIVE2D=1：跳过 Live2D，强制使用 Q6 SpriteRenderer")
        from avatar.sprite_renderer import SpriteRenderer
        return SpriteRenderer(parent)

    if fmt == "live2d":
        try:
            from avatar.live2d_renderer import Live2DRenderer
            return Live2DRenderer(parent)
        except Exception as e:
            logger.warning("Live2DRenderer 不可用 (%s)，回退 Q6 Sprite：%s", type(e).__name__, e)

    if fmt == "vrm":
        # 未来 3D 扩展钩子：当前 VRMRenderer 为占位，load() 返回 False。
        # 若需要立即可用的角色，请改用 Q6 / Live2D 格式。
        logger.warning(
            "create_renderer('%s'): VRM 格式尚未实现，将显示占位（空白/未实现提示）。"
            "请改用 Q6 或 Live2D 角色。", character_id)
        from avatar.vrm_renderer import VRMRenderer
        return VRMRenderer(parent)

    # Q6 原生帧精灵（默认路径，phoebe / yuexinmiao 等）
    from avatar.sprite_renderer import SpriteRenderer
    return SpriteRenderer(parent)


def resource_available(character_id: str) -> tuple[bool, str]:
    """判断角色是否具备可加载的模型/帧资源。

    用于设置面板「切换桌宠」前的预校验，避免切到「声明 live2d 但模型未下载」
    （如 shizuku，其 pet.json 明确说明模型本体不随仓库分发）这类角色时
    静默白屏 / 加载失败。

    Returns:
        (True, "")                 资源齐备，可安全加载
        (False, "<原因>")           缺失关键资源（不应作为可加载角色切换过去）
    """
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    char_dir = os.path.join(base, "characters", character_id)
    if not os.path.isdir(char_dir):
        # 已知角色（如 shizuku）声明 live2d 但目录不存在 → 缺少模型文件
        known_live2d = {"shizuku", "miku", "yuexinmiao"}
        if character_id in known_live2d:
            return False, f"缺少 Live2D 模型文件（{character_id}，请按 README 下载放置）"
        return False, f"角色目录不存在: {character_id}"

    fmt = detect_format(character_id)
    if fmt == "live2d":
        # 2026-09-08: 支持直接检查 char_dir 下的 .model3.json（live2d zip 导入）
        has_model = any(
            f.lower().endswith((".model3.json", ".model.json"))
            for f in os.listdir(char_dir)
        )
        if not has_model:
            # 回退到 live2d/ 子目录
            live2d_dir = os.path.join(char_dir, "live2d")
            if os.path.isdir(live2d_dir):
                has_model = any(
                    f.lower().endswith((".model3.json", ".model.json"))
                    for f in os.listdir(live2d_dir)
                )
        if not has_model:
            return False, "缺少 Live2D 模型文件（*.model3.json，请按 README 下载放置）"
        return True, ""
    if fmt == "vrm":
        return False, "VRM 格式尚未实现，暂不可加载"
    # sprite / Q6 原生帧精灵
    if os.path.isdir(os.path.join(char_dir, "frames")):
        return True, ""
    if os.path.isfile(os.path.join(char_dir, "spritesheet.webp")):
        return True, ""
    pet_json = os.path.join(char_dir, "pet.json")
    if os.path.isfile(pet_json):
        try:
            with open(pet_json, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("frames") or data.get("atlas") or data.get("emotions"):
                return True, ""
        except Exception:
            pass
    return False, "缺少精灵帧资源（frames/ 目录或 spritesheet.webp）"
