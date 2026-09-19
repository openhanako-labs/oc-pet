# -*- coding: utf-8 -*-
"""动作**看得见吗**——配对法，带对照组。

## 为什么不用幅度大小

旧方法（`measure_motion.py`）比的是"动作期间的帧差总量"，而桌宠一直在动
（待机/眨眼/呼吸/跟鼠标），基线就有 3.8~10.7 —— 信号埋在噪声里，得不出结论。

## 这个方法怎么做的

**不比幅度，比"响应有没有和触发对齐"**：

* 每 5 秒触发一次动作，同时以 ~15fps 连续截图（**每帧重新定位窗口**，
  否则它跟着鼠标走会让背景错位被当成画面变化）；
* 对每次触发，取**触发前 1.5s** 和**触发后 1.5s** 各自的最大帧差，算比值；
* 前后配对 → 桌宠的"一直在动"被同时扣掉，只留下"触发带来了什么额外变化"。

**对照组**：`--bogus` 用一个不存在的动作名触发——报幕照发、时间点一样、
就是没有动作。如果对照组也出现同样的峰，说明那峰是噪声，不是我测的东西。

## 已知失效条件（2026-09-19 实测）

**桌宠会跟着鼠标走。**一旦它在两次截图之间移动，裁剪框跟着变，
画面内容整体错位 → 帧差出现 40~200 的假峰（灰度 0~255），把真实信号完全淹没。

实测结果：对照组（不存在的动作名）比值中位数 **54.95×**，
实测组（真动作 waving）**1.00×** —— **反着的，说明没测到东西**。

**结论：桌宠在走动时，本方法不可信。**要拿数字，必须先让它停下来
（停桌宠 → 关掉走动行为 → 测 → 还原）；否则只能用眼睛看。

## 副产品发现（待查）

对照组 4/4 次触发都出现了剧烈视觉跳变（最大帧差 204，= 画面完全变了），
而且**只在触发之后**、且那轮桌宠本来没在动。
→ **`play_anim` 收到听都没听过的动作名时，不是“安静地不做事”，
而是发生了某种视觉跳变**。这很可能是个真 bug（比如精灵序列被切到空）。
这正是“先校名字再派发”要解决的问题。

用法::

    python tools/probe_motion_visible.py --anim waving --shots 5
    python tools/probe_motion_visible.py --anim waving --shots 5 --bogus   # 对照组
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

FPS = 15.0
AFTER = 1.5      # 触发后观察窗
BEFORE = 1.5     # 触发前对照窗


def grab(box):
    x, y, w, h = box
    img = ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True)
    return np.asarray(img.convert("L").resize((w // 4, h // 4)), dtype=np.float32)


def main() -> int:
    ap = argparse.ArgumentParser(description="动作可见性配对测试")
    ap.add_argument("--anim", default="waving")
    ap.add_argument("--port", type=int, default=8979)
    ap.add_argument("--shots", type=int, default=5)
    ap.add_argument("--interval", type=float, default=5.0, help="两次触发的间隔秒")
    ap.add_argument("--bogus", action="store_true", help="对照组：触发不存在的动作名")
    args = ap.parse_args()

    anim = "___不存在的动作___" if args.bogus else args.anim
    box, pid = detect_box()
    print("区域 x=%d y=%d w=%d h=%d ｜ 进程 %s" % (box[0], box[1], box[2], box[3], pid))
    mc = Mcp(args.port)
    print("MCP session=%s ｜ 触发名=%r%s"
          % (bool(mc.sid), anim, "（对照组）" if args.bogus else ""))

    samples = []          # [(t, frame)]
    triggers = []
    t0 = time.monotonic()
    nxt = t0 + 1.0        # 第一次触发在 1 秒后
    end = t0 + 1.0 + args.interval * args.shots + AFTER + 0.5

    print("开始采样 %.1fs…" % (end - t0))
    while time.monotonic() < end:
        now = time.monotonic()
        if now >= nxt and len(triggers) < args.shots:
            try:
                mc.call("pet_play_anim", {"anim": anim})
            except Exception as e:  # noqa: BLE001
                print("  触发失败:", e)
            triggers.append(now)
            nxt = now + args.interval
        b, _ = detect_box(quiet=True)
        samples.append((time.monotonic(), grab(b)))
        time.sleep(max(0.0, 1.0 / FPS - 0.004))

    # ── 配对比较 ──
    diffs = []
    for i in range(len(samples) - 1):
        (t1, f1), (t2, f2) = samples[i], samples[i + 1]
        if f1.shape != f2.shape:
            continue
        diffs.append(((t1 + t2) / 2.0, float(np.abs(f2 - f1).mean())))

    print("有效帧差样本 %d ｜ 触发 %d 次" % (len(diffs), len(triggers)))
    print("%-6s %-12s %-12s %-8s" % ("次数", "触发前最大", "触发后最大", "比值"))
    ratios = []
    for k, tk in enumerate(triggers, 1):
        before = [d for t, d in diffs if tk - BEFORE <= t < tk]
        after = [d for t, d in diffs if tk <= t < tk + AFTER]
        if not before or not after:
            print("%-6d %-12s %-12s %-8s" % (k, "-", "-", "-"))
            continue
        bmax, amax = max(before), max(after)
        r = (amax / bmax) if bmax > 1e-6 else float("inf")
        ratios.append(r)
        print("%-6d %-12.2f %-12.2f %-8.2f" % (k, bmax, amax, r))

    if ratios:
        med = statistics.median(ratios)
        print("-" * 44)
        print("比值中位数 = %.2f×（>1 表示触发后确实多出了变化）" % med)
        if med >= 1.5:
            print("判定：**测得对齐响应**——触发之后画面确实多出变化")
        elif med >= 1.15:
            print("判定：**弱对齐**（有但不强，可能动作本身幅度小）")
        else:
            print("判定：**没测出对齐响应**")
    else:
        print("样本不足")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
