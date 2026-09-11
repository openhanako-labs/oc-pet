"""窗口贴合 bbox 缓存。

`Live2DRenderer._fit_window_to_model` 靠 `HitDrawable` 网格扫描测角色 bbox
（约 1.5 万次命中检测），实测**每次启动都要 21.8s**。

但输入是完全确定的：

    模型文件 + 缩放系数 + 视口尺寸  →  bbox

而桌宠每次启动的视口尺寸都相同（`config.window` 不随贴合结果写回，
见 `pet_manager.launch_all` 的「运行时启发式不得修改持久配置」不变量），
所以除首次外，每次都在重算同一个结果。

## 缓存的是 bbox，不是最终窗口尺寸

补齐边距 / 宽高比下限那些是纯计算（微秒级），每次重跑。
这样将来调边距常量时缓存自动跟着生效，不用清缓存——
缓存只挡在最贵的那一步前面。

## 关于动画相位

bbox 是 3 帧并集，本来就不是时间的确定函数（扫到哪一相位有随机性）。
缓存反而让每次启动的外观一致；代价是那一次采样固定下来。
若模型换了动作或改了 pet.json，key 变化 → 自动重扫。
"""
from __future__ import annotations

import json
import logging
import os
import tempfile

logger = logging.getLogger(__name__)

# 语义版本：key 里带上，将来改了扫描口径（步长/帧数/坐标系）就升版本，
# 老缓存自动失效，不需要用户手动清。
_CACHE_VERSION = 1

_CACHE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "fit_cache.json",
)


def make_key(model_path, fit_scale, fit_scale_x, gl_w, gl_h) -> str:
    """把「决定 bbox 的全部输入」编成缓存键。

    这四项之外，没有别的东西会影响扫描结果——所以键相同 = 结果必然相同。
    """
    return "|".join((
        f"v{_CACHE_VERSION}",
        # 大小写不敏感地规范化路径：Windows 上同一文件可能以不同大小写出现
        os.path.normcase(os.path.abspath(str(model_path or ""))),
        f"{float(fit_scale):.4f}",
        f"{float(fit_scale_x):.4f}",
        str(int(gl_w)),
        str(int(gl_h)),
    ))


def _read_all() -> dict:
    try:
        with open(_CACHE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        # 缓存坏了不是错误，只是没命中。绝不能让读缓存把桌宠搞崩。
        logger.info("fit_cache: 读取失败（当作无缓存）: %s", e)
        return {}


def load(key: str, gl_w: int, gl_h: int):
    """取缓存 bbox，返回 (min_x, min_y, max_x, max_y)；无缓存或不合法返回 None。

    `gl_w/gl_h` 用来做越界校验：键里虽然已经带了视口尺寸，但文件可能被改坏，
    而一个越界的 bbox 会算出比屏幕还大的窗口。宁可重扫。
    """
    rec = _read_all().get(key)
    if not isinstance(rec, (list, tuple)) or len(rec) != 4:
        return None
    try:
        min_x, min_y, max_x, max_y = (float(v) for v in rec)
    except (TypeError, ValueError):
        logger.info("fit_cache: 缓存值非法，丢弃并重扫: %r", rec)
        return None

    if not (min_x <= max_x and min_y <= max_y):
        logger.info("fit_cache: 缓存矩形反向，丢弃并重扫: %r", rec)
        return None
    if min_x < 0 or min_y < 0 or max_x > gl_w or max_y > gl_h:
        logger.info("fit_cache: 缓存越界 %r（视口 %dx%d），丢弃并重扫",
                    rec, gl_w, gl_h)
        return None
    # 退化矩形（宽度或高度只有几像素）说明扫描时模型没画出来，不能用。
    if (max_x - min_x) < 8 or (max_y - min_y) < 8:
        logger.info("fit_cache: 缓存矩形退化，丢弃并重扫: %r", rec)
        return None

    return (min_x, min_y, max_x, max_y)


def save(key: str, bbox) -> None:
    """写入缓存。失败只记日志——缓存写不进去不该影响桌宠启动。"""
    try:
        min_x, min_y, max_x, max_y = (float(v) for v in bbox)
    except (TypeError, ValueError):
        return

    data = _read_all()
    data[key] = [min_x, min_y, max_x, max_y]
    # 只保留最近若干条：正常只有一两个角色，但用户反复换模型/缩放会累积。
    # ponytail: 全量读改写、无锁；多桌宠并写时可能丢一条记录（代价=下次重扫一次）。
    if len(data) > 32:
        data = dict(list(data.items())[-32:])

    try:
        os.makedirs(os.path.dirname(_CACHE_PATH), exist_ok=True)
        # 原子写：先临时文件再 replace，避免写到一半崩溃留下半个 JSON
        # （半个 JSON 会让 _read_all 解析失败，所有缓存一起失效）。
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(_CACHE_PATH),
                                   suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, _CACHE_PATH)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception as e:
        logger.info("fit_cache: 写入失败（不影响本次启动）: %s", e)
