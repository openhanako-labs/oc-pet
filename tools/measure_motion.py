# -*- coding: utf-8 -*-
"""客观测一次动作**看得见吗**——量像素，不靠肉眼。

背景：动作"播了但看不见"这件事吵不出结论。日志只能证明 `StartMotion` 被调了，
证明不了屏幕上有没有变化。所以直接量：截桌宠那块区域，看前后帧差多少。

做法：
  1. 先量一段时间**不触发**的基线帧差（视线跟随/呼吸/鼠标经过都会造成差异，
     必须扣掉，否则会把"它在看鼠标"误判成"动作生效了"）；
  2. 通过 MCP 调 ``pet_play_anim`` 触发动作，同时连续截图；
  3. 报 基线 vs 触发期间 的帧差，比值就是判据。

区域默认**自动找**：读最新的 ``logs/heartbeat_<pid>.txt`` 拿桌宠进程号，
再枚举该进程的可见窗口取矩形（不靠 config 里的坐标猜——实测那里是旧值）。

用法::

    python tools/measure_motion.py                       # 测当前 waving
    python tools/measure_motion.py --no-trigger          # 只量基线（对照）
    python tools/measure_motion.py --box 100,100,237,523 # 手动指定

前提：桌宠在跑，且 ``mcp_server.enabled=true``、``allow_actions=true``。

## 已知失效条件（2026-09-19 实测，必须知道）

**桌宠会跟着鼠标走**，窗口一直在漂。实测两次对照：

* 原版“挥手”：16 帧里 **13 帧因窗口移动被剔除**，剩下 3 帧；
* 同一方法下**原版动作也测不出差别**。

所以：**本工具在“桌宠会走动”的环境下分辨不出动作，不能拿它的输出来
断言“看得见/看不见”。**它的输出只在桌宠不动时可信（例如关了跟随、
或把鼠标抬离）。要判断动作是否可见，目前只有一条信息源：**人看**。
"""
from __future__ import annotations

import argparse
import ctypes
import glob
import json
import os
import re
import time
import urllib.request
from ctypes import wintypes

import numpy as np
from PIL import ImageGrab

#: 备用的兜底区域（config 里的位置；实测会过期，仅当自动探测失败时用）
FALLBACK_BOX = (1223, 353, 237, 523)


def pet_pid() -> int | None:
    """从最新的心跳文件里拿桌宠进程号。"""
    files = glob.glob(os.path.join("logs", "heartbeat_*.txt"))
    if not files:
        return None
    newest = max(files, key=os.path.getmtime)
    m = re.search(r"heartbeat_(\d+)", os.path.basename(newest))
    return int(m.group(1)) if m else None


def windows_of_pid(pid: int) -> list:
    """枚举该进程的可见顶层窗口，返回 ``[(hwnd, (x,y,w,h), title)]``。"""
    u = ctypes.windll.user32
    out = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _lparam):
        if not u.IsWindowVisible(hwnd):
            return True
        wpid = wintypes.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if wpid.value != pid:
            return True
        r = wintypes.RECT()
        if not u.GetWindowRect(hwnd, ctypes.byref(r)):
            return True
        w, h = r.right - r.left, r.bottom - r.top
        if w < 60 or h < 60:                      # 忽略小气泡/工具窗
            return True
        n = u.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(hwnd, buf, n + 1)
        out.append((hwnd, (r.left, r.top, w, h), buf.value))
        return True

    u.EnumWindows(cb, 0)
    return out


def detect_box(quiet: bool = False) -> tuple:
    """自动定位桌宠窗口区域（失败则回退到 FALLBACK_BOX）。"""
    pid = pet_pid()
    if not pid:
        if not quiet:
            print("！ 没找到心跳文件，用备用区域")
        return FALLBACK_BOX, pid
    wins = windows_of_pid(pid)
    if not wins:
        if not quiet:
            print("！ 进程 %d 没有可见窗口，用备用区域" % pid)
        return FALLBACK_BOX, pid
    wins.sort(key=lambda t: t[1][2] * t[1][3], reverse=True)
    hwnd, box, title = wins[0]
    if not quiet:
        print("桌宠进程 %d ｜ 窗口 hwnd=%s 标题=%r ｜ 共 %d 个可见窗口"
              % (pid, hwnd, title, len(wins)))
    return box, pid


def _post(url: str, payload: dict, sid: str | None = None, timeout: float = 15.0):
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json, text/event-stream")
    if sid:
        req.add_header("Mcp-Session-Id", sid)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return dict(resp.headers), resp.read().decode("utf-8", "replace")


def _parse(body: str):
    body = (body or "").strip()
    if not body:
        return None
    if body[0] in "{[":
        try:
            return json.loads(body)
        except Exception:
            return None
    out = None
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            try:
                out = json.loads(line[5:].strip())
            except Exception:
                pass
    return out


class Mcp:
    def __init__(self, port: int, host: str = "127.0.0.1") -> None:
        self.url = "http://%s:%d/mcp" % (host, port)
        self.sid = None
        hdrs, body = _post(self.url, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "measure-motion", "version": "0.1"}},
        })
        self.sid = next((v for k, v in hdrs.items() if k.lower() == "mcp-session-id"), None)
        if self.sid:
            try:
                _post(self.url, {"jsonrpc": "2.0", "method": "notifications/initialized",
                                 "params": {}}, self.sid, timeout=6.0)
            except Exception:
                pass
        self.msg = _parse(body) or {}

    def call(self, tool: str, args: dict):
        _, body = _post(self.url, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                   "params": {"name": tool, "arguments": args}}, self.sid)
        res = (_parse(body) or {}).get("result") or {}
        content = res.get("content") or []
        if content and isinstance(content[0], dict):
            return content[0].get("text")
        return json.dumps(res, ensure_ascii=False)[:400]


def _grab(box) -> np.ndarray:
    x, y, w, h = box
    img = ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True)
    img = img.convert("L").resize((w // 4, h // 4))
    return np.asarray(img, dtype=np.float32)


def _sample(box, seconds: float, fps: float = 12.0, track: bool = True) -> list:
    """连续截图。``track=True`` 时每帧重新定位窗口。

    **必须跟踪**：桌宠会跟着鼠标走，窗口位置一直在漂。不跟的话背景错位
    会被当成“画面在变化”（实测基线 10.7，而真实变化没这么大）。

    Returns:
        ``[(box, frame, moved), ...]``，``moved`` 表示这一帧窗口位移超过 2px。
    """
    frames = []
    t0 = time.monotonic()
    last_box = box
    while time.monotonic() - t0 < seconds:
        b = box
        if track:
            try:
                b, _ = detect_box(quiet=True)
            except Exception:
                b = last_box
        moved = abs(b[0] - last_box[0]) > 2 or abs(b[1] - last_box[1]) > 2
        frames.append((b, _grab(b), moved))
        last_box = b
        time.sleep(max(0.0, 1.0 / fps - 0.005))
    return frames


def _diffs(frames: list) -> dict:
    """相邻帧差；**剔除窗口移动的帧**（那是背景错位，不是动作）。"""
    vals = []
    skipped = 0
    for i in range(len(frames) - 1):
        _b1, f1, _m1 = frames[i]
        _b2, f2, m2 = frames[i + 1]
        if m2 or f1.shape != f2.shape:
            skipped += 1
            continue
        vals.append(float(np.abs(f2 - f1).mean()))
    if not vals:
        return {"n": 0, "median": 0.0, "max": 0.0, "skipped": skipped, "series": []}
    return {
        "n": len(vals),
        "median": float(np.median(vals)),
        "max": float(np.max(vals)),
        "skipped": skipped,
        "series": [round(v, 2) for v in vals],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="量一次动作看不看得见")
    ap.add_argument("--anim", default="waving")
    ap.add_argument("--port", type=int, default=8979)
    ap.add_argument("--box", default=None, help="桌宠窗口 x,y,w,h（不给就自动探测）")
    ap.add_argument("--baseline", type=float, default=2.0, help="基线采样秒数")
    ap.add_argument("--window", type=float, default=2.5, help="触发后采样秒数")
    ap.add_argument("--no-trigger", action="store_true", help="只量基线（对照）")
    args = ap.parse_args()

    if args.box:
        box = tuple(int(v) for v in args.box.split(","))
        print("区域（手动）x=%d y=%d w=%d h=%d" % box)
    else:
        box, _pid = detect_box()
    print("区域 x=%d y=%d w=%d h=%d" % box)

    mcp = None
    if not args.no_trigger:
        try:
            mcp = Mcp(args.port)
            print("MCP: session=%s serverInfo=%s" % (
                bool(mcp.sid), json.dumps((mcp.msg.get("result") or {}).get("serverInfo") or {},
                                          ensure_ascii=False)))
        except Exception as e:  # noqa: BLE001
            print("!! MCP 连不上：%s: %s" % (type(e).__name__, e))
            return 1

    print("基线采样 %.1fs…" % args.baseline)
    base = _sample(box, args.baseline)
    b = _diffs(base)
    print("   基线：中位 %.3f ｜ 最大 %.3f ｜ 有效帧 %d（剔掉移动帧 %d）"
          % (b["median"], b["max"], b["n"], b["skipped"]))

    if args.no_trigger:
        print("   序列: %s" % b["series"])
        print("(对照：未触发)")
        return 0

    print("触发 pet_play_anim(%r) 并采样 %.1fs…" % (args.anim, args.window))
    t0 = time.monotonic()
    try:
        ret = mcp.call("pet_play_anim", {"anim": args.anim})
        print("   工具返回: %s" % ret)
    except Exception as e:  # noqa: BLE001
        print("!! 触发失败：%s: %s" % (type(e).__name__, e))
        return 1
    print("   (调用耗时 %.2fs)" % (time.monotonic() - t0))
    during = _sample(box, args.window)
    d = _diffs(during)

    print("-" * 46)
    print("基线：中位 %.3f ｜ 最大 %.3f ｜ 有效帧 %d" % (b["median"], b["max"], b["n"]))
    print("动作：中位 %.3f ｜ 最大 %.3f ｜ 有效帧 %d（剔掉移动帧 %d）"
          % (d["median"], d["max"], d["n"], d["skipped"]))
    print("动作期间序列: %s" % d["series"])
    # 判据只认“峰值明显超过基线峰值”——桌宠一直在动，比值低不等于没动作
    if d["n"] == 0:
        print("判定：**测不到**（动作期间窗口一直在移动，样本全被剔除）")
        return 0
    if b["max"] > 0 and d["max"] >= 1.3 * b["max"]:
        print("判定：**测到明显变化**（动作峰值 %.2f 超过基线峰值 %.2f 的 1.3 倍）"
              % (d["max"], b["max"]))
    elif d["median"] >= 1.3 * b["median"] and b["median"] > 0:
        print("判定：**整体变化抬升**（中位 %.2f → %.2f）" % (b["median"], d["median"]))
    else:
        print("判定：**没测出差别**（峰值与中位都没超过基线的 1.3 倍）——"
              "注意这不等于“没播”，只等于“没测到”")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
