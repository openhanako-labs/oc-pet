"""程序化生成 Live2D 动作文件（``motion3.json``）——**不用 Cubism Editor**。

## 为什么要这个

"动作包生态"最硬的坎不是加载器（oc-pet 本来就会扫描 `motions/` 自动发现），
而是**动作文件从哪来**：`motion3.json` 的正统做法要用 Cubism Editor（商业软件）
手搓。所以真正值得试的是——**简单动作能不能算出来**。

`motion3.json` 就是 JSON 关键帧，能算。

## 段编码（**我第一版写错了，代价是桌宠原生崩溃**）

官方规范（Live2D/CubismSpecs `FileFormats/motion3.json.md`）：

    每条曲线先给第一个点，再给段标识符。
    线性段 = 标识符 + 1 个点；贝塞尔段 = 标识符 + 3 个点。

所以 ``Segments`` 的真实形状是::

    [x0, y0, id1, x1, y1, x2, y2, x3, y3, id2, ...]

**标识符在点之后，不在数组最前面。**

第一版我按 ``[0, 初始值, 时间, 值, …]`` 写（把标识符放在了开头）。
SDK 于是把 ``初始值`` 当 x、把后面的数字当**段标识符**解析，读到非法 id
（如 0.375）→ 越界读 → **0xC0000005 ACCESS_VIOLATION**，
整个桌宠被看门狗反复拉起来又崩（2026-09-19 实测）。

**教训：这个格式写错不是"动作不播"，是"宠物死"。**
所以本模块同时提供 :func:`parse_segments`——**装包前必须先解析校验**。

参数名必须用模型真有的（拿现有动作文件里的 ``Id`` 对过：
`ParamAngleX/Y/Z`、`ParamBreath`、`ParamEyeLOpen`… 都在）。
写一个不存在的参数名不会报错，只会**静静地什么都不做**。

用法::

    from tools.make_motion import build_motion, write_motion
    motion = build_motion({"ParamAngleY": [(0, 0), (0.2, -12), (0.4, 0), (0.6, -12), (0.9, 0)]})
    write_motion(motion, "characters/miku/live2d/motions/nod.motion3.json")
"""
from __future__ import annotations

import json
from pathlib import Path

#: 段标识符 → 该段后面跟几个点（官方规范）
SEGMENT_POINTS = {
    0: 1,   # 线性
    1: 3,   # 贝塞尔（起点、两个控制点……实为 3 个点）
    2: 1,   # 阶梯
    3: 1,   # 反向阶梯
}
SEGMENT_LINEAR = 0
#: 默认帧率（与模型自带动作一致）
DEFAULT_FPS = 30.0
#: 默认淡入淡出（本模型 7 个动作文件一律 0.3）
DEFAULT_FADE = 0.3


def parse_segments(segments, *, duration: float | None = None) -> list:
    """解析 ``Segments``，返回 ``[(时间, 值), ...]``。

    **这就是装包前的守门人**：把结构从头走到尾，不合规就抛 ``ValueError``。
    一个解析不过的动作文件（如我第一版那种错编码）会让原生 SDK 越界读、
    直接打死整个进程——所以宁可不装，也不能带着错文件启动。

    Raises:
        ValueError: 数组被截断、标识符非法、或时间不递增。
    """
    if not isinstance(segments, list) or len(segments) < 4:
        raise ValueError(f"Segments 太短或类型不对: {segments!r}")
    points = [(float(segments[0]), float(segments[1]))]
    i = 2
    n = len(segments)
    while i < n:
        kind = segments[i]
        if not isinstance(kind, int) or isinstance(kind, bool) or kind not in SEGMENT_POINTS:
            raise ValueError(f"非法段标识符 {kind!r}（位置 {i}）——这种文件会让 SDK 越界读")
        i += 1
        for _ in range(SEGMENT_POINTS[kind]):
            if i + 1 >= n:
                raise ValueError(f"段被截断：标识符 {kind} 后不足 {SEGMENT_POINTS[kind]} 个点")
            points.append((float(segments[i]), float(segments[i + 1])))
            i += 2
    times = [t for t, _ in points]
    for a, b in zip(times, times[1:]):
        if b < a:
            raise ValueError(f"时间不递增：{a} → {b}")
    if duration is not None and times and times[-1] > float(duration) + 1e-6:
        raise ValueError(f"最后一个点({times[-1]})超出 Duration({duration})")
    return points


def validate_motion(data: dict) -> dict:
    """校验一份 motion3.json 结构；通过返回统计信息，否则抛 ``ValueError``。"""
    if not isinstance(data, dict):
        raise ValueError("不是 JSON 对象")
    if data.get("Version") != 3:
        raise ValueError(f"Version 必须是 3，实际 {data.get('Version')!r}")
    meta = data.get("Meta")
    if not isinstance(meta, dict):
        raise ValueError("缺 Meta")
    dur = meta.get("Duration")
    if not isinstance(dur, (int, float)) or dur <= 0:
        raise ValueError(f"Meta.Duration 非法: {dur!r}")
    curves = data.get("Curves")
    if not isinstance(curves, list) or not curves:
        raise ValueError("Curves 必须是非空数组")
    total_points = 0
    for c in curves:
        if not isinstance(c, dict) or not c.get("Id"):
            raise ValueError(f"曲线缺 Id: {c!r}")
        pts = parse_segments(c.get("Segments"), duration=dur)
        total_points += len(pts)
    if meta.get("CurveCount") != len(curves):
        raise ValueError(f"Meta.CurveCount({meta.get('CurveCount')}) 与 Curves 数量({len(curves)}) 不符")
    if meta.get("TotalPointCount") != total_points:
        raise ValueError(f"Meta.TotalPointCount({meta.get('TotalPointCount')}) 与实际点数({total_points}) 不符")
    return {"curves": len(curves), "points": total_points, "duration": dur}


def build_motion(curves: dict, *, duration: float | None = None,
                 fps: float = DEFAULT_FPS, loop: bool = False,
                 fade_in: float = DEFAULT_FADE, fade_out: float = DEFAULT_FADE) -> dict:
    """把 ``{参数名: [(时间, 值), ...]}`` 编成 motion3.json 结构。

    段形状严格按官方规范：先第一个点，再逐个段（线性 = 标识符 0 + 一个点）。

    Args:
        curves: 参数名 → 关键帧列表。**必须含 t=0**（第一帧是 t=0 的点）。
        duration: 时长；不给则取所有关键帧里的最大时间。
        fps / loop: 写进 Meta。
        fade_in / fade_out: Meta 里的淡入淡出；本模型 7 个动作一律 0.3。

    Raises:
        ValueError: 关键帧为空、时间不从 0 开始、或时间不是递增。
    """
    if not curves:
        raise ValueError("至少要有一条曲线")
    out_curves = []
    max_t = 0.0
    total_points = 0
    total_segments = 0
    for name, frames in curves.items():
        if not frames:
            raise ValueError(f"曲线 {name} 没有关键帧")
        times = [float(t) for t, _ in frames]
        if abs(times[0]) > 1e-9:
            raise ValueError(f"曲线 {name} 的第一个关键帧必须在 t=0（当前 {times[0]}）")
        for a, b in zip(times, times[1:]):
            if b < a:
                raise ValueError(f"曲线 {name} 的时间必须递增：{a} → {b}")
        # 官方形状：先第一个点 (x0, y0)，之后每段是 [标识符, 点…]
        segments = [float(frames[0][0]), float(frames[0][1])]
        for t, v in frames[1:]:
            segments.append(SEGMENT_LINEAR)
            segments.append(float(t))
            segments.append(float(v))
        out_curves.append({"Target": "Parameter", "Id": name, "Segments": segments})
        max_t = max(max_t, times[-1])
        total_points += len(frames)
        total_segments += len(frames) - 1

    dur = float(duration) if duration is not None else max_t
    if dur < max_t - 1e-9:
        raise ValueError(f"duration({dur}) 小于最后关键帧时间({max_t})")
    motion = {
        "Version": 3,
        "Meta": {
            "Duration": round(dur, 3),
            "Fps": float(fps),
            "Loop": bool(loop),
            "AreBeziersRestricted": True,
            "CurveCount": len(out_curves),
            "TotalSegmentCount": total_segments,
            "TotalPointCount": total_points,
            "UserDataCount": 0,
            "TotalUserDataSize": 0,
            "FadeInTime": float(fade_in),
            "FadeOutTime": float(fade_out),
        },
        "Curves": out_curves,
    }
    validate_motion(motion)          # 自己造的自己先过一遍守门人
    return motion


def write_motion(motion: dict, path) -> Path:
    """写盘（``ensure_ascii=False``；**不带 BOM**——BOM 会让解析端炸）。

    写之前再校验一次：**宁可报错，也不让一个解析不过的文件落到磁盘上**。
    """
    validate_motion(motion)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(motion, ensure_ascii=False, indent="\t"), encoding="utf-8")
    return p


def nod_motion(amplitude: float = 12.0, cycles: float = 2.0) -> dict:
    """一个最朴素的"点头"：``ParamAngleY`` 来回摆几下再回正。

    振幅取 12（保守）——不同模型俯仰的正负方向不一定一致，
    但**来回摆动**在两种约定下都看得出是点头。
    """
    frames = [(0.0, 0.0)]
    step = 0.25
    n = max(1, int(cycles * 2))
    for i in range(n):
        frames.append(((i + 0.5) * step, -amplitude if i % 2 == 0 else amplitude * 0.4))
        frames.append(((i + 1) * step, 0.0))
    frames.append((frames[-1][0] + 0.2, 0.0))          # 收尾停住
    return build_motion({"ParamAngleY": frames}, loop=False)


if __name__ == "__main__":  # pragma: no cover - 手跑用
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else "nod_test.motion3.json"
    print(write_motion(nod_motion(), target))
