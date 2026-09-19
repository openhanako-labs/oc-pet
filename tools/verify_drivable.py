# -*- coding: utf-8 -*-
"""系统验证：这个模型**到底能被驱动什么**，逐项量。

## 为什么换方法

前三次按"响应有没有和触发对齐"测动作，全失败了——桌宠自己在动、我又连发触发，
"触发前"的对照窗口根本不干净。

这次分两类，各用合适的判据：

* **表情（贴图开关）** = **状态**类：开上之后一直亮（5 秒后自动超时重置）。
  → 比"开着的帧"和"基线的帧"的差异，**不需要时间对齐**，最稳。
* **动作（motion3.json）** = **事件**类：播 3 秒就回待机。
  → 单发一次（前后各留 8 秒静默期，**绝不连发**），比触发前后各自的最大帧差。

## 对照

每一项都在**同一个进程、同一段时间**里量：先基线、再驱动、再基线。
运行期间不要做别的事，也不要用别的工具触发桌宠。

用法::

    python tools/verify_drivable.py --part expression
    python tools/verify_drivable.py --part motion
    python tools/verify_drivable.py --part all
"""
from __future__ import annotations

import argparse
import statistics
import sys
import time

sys.path.insert(0, ".")
from tools.measure_motion import Mcp, detect_box  # noqa: E402

import numpy as np  # noqa: E402
from PIL import ImageGrab  # noqa: E402

FPS = 12.0
#: 脸占整只宠物的上部；只裁这里，小范围贴图变化才不会被全图均值稀掉
HEAD_RATIO = 0.42
#: 模型自带的 7 个动作文件 + 8 个表情（来自 miku.model3.json 的清单，不是猜的）
MOTIONS = ["idle", "happy", "waving", "angry", "sad", "thinking", "touch"]
EXPRESSIONS = ["比心", "唱歌", "葱", "脸红", "前倾", "圈圈", "QQ人", "水印"]


def grab(box, head_only: bool = True):
    """截桌宠窗口；``head_only`` 时只取上部（脸）。"""
    x, y, w, h = box
    if head_only:
        h = max(20, int(h * HEAD_RATIO))
    img = ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True)
    return np.asarray(img.convert("L").resize((w // 2, h // 2)), dtype=np.float32)


def sample(seconds: float):
    """连续截图，返回帧差列表（每帧重新定位窗口）。**适合事件类（动作）**。"""
    return _diffs(sample_frames(seconds))


def sample_frames(seconds: float) -> list:
    """连续截图，返回帧列表。"""
    frames = []
    t0 = time.monotonic()
    while time.monotonic() - t0 < seconds:
        b, _ = detect_box(quiet=True)
        frames.append(grab(b))
        time.sleep(max(0.0, 1.0 / FPS - 0.004))
    return frames


def state_delta(frames_a: list, frames_b: list) -> tuple:
    """**状态差**：前后两段画面的差异（取中位帧，抗噪声）。

    返回 ``(平均差, 变化像素比例)``。
    **比例比均值重要**：脸红/圈圈这类只改脸上一小块，全图均值会被面积稀掉
    （实测脸红只有 0.56，阈值卡 3.0 就误判成“无变化”）。

    贴图开关是"停在某个状态"的变化，量“相邻帧差”永远是 0——第一版就因此
    报过 `0/0 = inf = 可见` 的假结论。
    """
    if not frames_a or not frames_b:
        return 0.0, 0.0
    a = np.median(np.stack(frames_a), axis=0)
    b = np.median(np.stack(frames_b), axis=0)
    if a.shape != b.shape:
        return 0.0, 0.0
    d = np.abs(b - a)
    return float(d.mean()), float((d > 12).mean())


def _diffs(frames):
    out = []
    for i in range(len(frames) - 1):
        if frames[i].shape != frames[i + 1].shape:
            continue
        out.append(float(np.abs(frames[i + 1] - frames[i]).mean()))
    return out


def _stat(vals):
    if not vals:
        return 0.0, 0.0
    return statistics.median(vals), max(vals)


def run_expressions(mc, box) -> list:
    """表情 = **状态类**：量“开之前”和“开之后”的画面差，不看帧间抖动。"""
    rows = []
    print("%-8s %-10s %-12s %-10s" % ("表情", "平均差", "变化像素比例", "判定"))
    for name in EXPRESSIONS:
        base = sample_frames(2.0)
        try:
            mc.call("pet_expression", {"name": name})
        except Exception as e:  # noqa: BLE001
            print("%-8s 触发失败: %s" % (name, e))
            continue
        time.sleep(0.8)
        on = sample_frames(2.0)

        delta, ratio = state_delta(base, on)
        verdict = "可见" if ratio >= 0.04 else ("弱" if ratio >= 0.015 else "无变化")
        rows.append((name, delta, ratio, verdict))
        print("%-8s %-10.2f %-12.3f %-10s" % (name, delta, ratio, verdict))
        time.sleep(5.5)   # 等它自己超时重置（日志里是 5s）
    return rows


def run_motions(mc, box) -> list:
    rows = []
    print("%-10s %-10s %-10s %-8s" % ("动作", "基线最大", "动作最大", "倍数"))
    for name in MOTIONS:
        time.sleep(8.0)                     # 静默期：绝不连发（前三次就是栽在这）
        base = sample(2.5)
        try:
            mc.call("pet_play_anim", {"anim": name})
        except Exception as e:  # noqa: BLE001
            print("%-10s 触发失败: %s" % (name, e))
            continue
        during = sample(3.0)
        _, b_max = _stat(base)
        _, d_max = _stat(during)
        ratio = (d_max / b_max) if b_max > 1e-6 else float("inf")
        verdict = "可见" if ratio >= 1.4 else ("弱" if ratio >= 1.1 else "未测出")
        rows.append((name, b_max, d_max, ratio, verdict))
        print("%-10s %-10.2f %-10.2f %-8.2f %s" % (name, b_max, d_max, ratio, verdict))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description="系统验证模型能被驱动什么")
    ap.add_argument("--part", choices=["expression", "motion", "all"], default="all")
    ap.add_argument("--port", type=int, default=8979)
    args = ap.parse_args()

    box, pid = detect_box()
    print("区域 x=%d y=%d w=%d h=%d ｜ 进程 %s" % (box[0], box[1], box[2], box[3], pid))
    mc = Mcp(args.port)
    print("MCP session=%s\n" % bool(mc.sid))

    if args.part in ("expression", "all"):
        print("########## 表情（贴图开关，状态对比）##########")
        run_expressions(mc, box)
        print()
    if args.part in ("motion", "all"):
        print("########## 动作（motion3.json，单发事件）##########")
        run_motions(mc, box)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
