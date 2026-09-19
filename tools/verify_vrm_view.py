# -*- coding: utf-8 -*-
"""VRM 视图端到端验证 —— 真渲染一次并截图。

用法（在项目根目录）：

    python tools/verify_vrm_view.py                       # 用 data/models/vrm_sample 下的样片
    python tools/verify_vrm_view.py --model path/to/x.vrm
    python tools/verify_vrm_view.py --out shot.png --emotion happy --mouth 0.8

它做的事：起 QApplication → 装配 QWebEngineView（真实 WebGL）→ 载入 .vrm →
等 JS 上报 ready（含轮询后备）→ 从页面内 `canvas.toDataURL()` 取一帧 PNG 落盘 →
打印 status()（含 JS 事件与错误）。

注意：会短暂弹出一个窗口——WebGL 需要有可见表面才会出帧。
退出码：0 = 成功出图；1 = 失败（原因见输出）。
"""
from __future__ import annotations

import argparse
import base64
import json
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 页面控制台/诊断日志：ES 模块导入失败只有这里能看到
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

DEFAULT_SAMPLE_DIR = ROOT / "data" / "models" / "vrm_sample"


def _pick_model(explicit: str | None) -> Path | None:
    if explicit:
        p = Path(explicit)
        return p if p.is_file() else None
    if DEFAULT_SAMPLE_DIR.is_dir():
        hits = sorted(DEFAULT_SAMPLE_DIR.glob("*.vrm"))
        if hits:
            return hits[0]
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=None, help="要渲染的 .vrm；缺省用 data/models/vrm_sample/*.vrm")
    ap.add_argument("--out", default="vrm_view_shot.png", help="截图输出路径")
    ap.add_argument("--timeout", type=float, default=90.0, help="等待模型就绪的秒数")
    ap.add_argument("--emotion", default="happy", help="截图前设置的情绪预设")
    ap.add_argument("--mouth", type=float, default=0.85, help="截图前设置的口型开合 0~1")
    args = ap.parse_args()

    model = _pick_model(args.model)
    if model is None:
        print("[FAIL] 找不到 .vrm 模型；用 --model 指定，或先下载样片到 "
              f"{DEFAULT_SAMPLE_DIR}")
        return 1
    print(f"[info] 模型: {model}  ({model.stat().st_size / 1048576:.1f} MB)")

    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QWidget

    app = QApplication.instance() or QApplication(sys.argv)
    # 让 WebEngine 的 OpenGL 上下文与宿主共享（否则部分驱动上黑屏）
    try:
        QApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
    except Exception:
        pass

    from avatar.vrm_renderer import VRMRenderer

    host = QWidget()
    host.resize(420, 480)
    host.show()

    renderer = VRMRenderer(host, char_dir=model.parent)
    if not renderer.load("_verify"):
        print(f"[FAIL] load() 返回 False：{renderer.unsupported_reason}")
        return 1
    if renderer.view is not None:
        renderer.view.loadFinished.connect(lambda ok: print(f"[info] 视图载入完成 ok={ok}"))

    ok = renderer.wait_until_ready(args.timeout)
    status = _status(renderer)
    print("[info] ready=", ok, " page_error=", renderer.page_error or "(none)")
    print("[info] status=", json.dumps(status, ensure_ascii=False))
    print("[info] metrics=", json.dumps(_metrics(renderer), ensure_ascii=False))
    if not ok:
        print("[FAIL] 模型未就绪")
        return 1

    # 驱动一次表演：情绪 + 口型 + 视线，再截一帧
    renderer.set_emotion(args.emotion, 1.0)
    renderer._js_args("setSpeaking", True)
    renderer._js_args("setMouth", float(args.mouth))
    renderer.look_at(host.width() // 2, host.height() // 3)
    _spin(app, 2.2)   # 让口型插值/眨眼/springbone 跑几帧

    data_url = _snapshot(renderer, app)
    if not data_url or "," not in data_url:
        print("[FAIL] 页面未能返回 canvas 截图（toDataURL 为空）")
        print("[info] status=", json.dumps(_status(renderer), ensure_ascii=False))
        return 1

    out = Path(args.out)
    out.write_bytes(base64.b64decode(data_url.split(",", 1)[1]))
    print(f"[OK] 截图已保存: {out}  ({out.stat().st_size} B)")
    print("[info] metrics=", json.dumps(_metrics(renderer), ensure_ascii=False))
    renderer.cleanup()
    return 0


def _spin(app, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


class _Catcher:
    """runJavaScript 回调的容器：对象本身被局部变量强引用，跨等待期不会被回收。"""

    def __init__(self) -> None:
        self.got = False
        self.value = None

    def __call__(self, value) -> None:
        self.got = True
        self.value = value

    def on_result(self, value) -> None:
        """绑定方法形式——PySide 下只有被强引用的绑定方法才能可靠回调。"""
        self.got = True
        self.value = value


def _run_js(renderer, expr: str, timeout: float = 6.0):
    """跑一段 JS 并等回调。

    注意：回调必须传**绑定方法**（catcher.on_result）。PySide 下用局部闭包
    可能在 JS 执行前就被回收，表现为「runJavaScript 永不回调」。
    另外调用方应尽量让表达式返回 JSON 字符串——PySide 对 JS 对象/数组的
    转换不可靠（会回 None）。
    """
    catcher = _Catcher()
    from PySide6.QtWidgets import QApplication
    try:
        renderer.view.page().runJavaScript(expr, catcher.on_result)
    except Exception:  # pragma: no cover
        return None
    end = time.monotonic() + timeout
    while not catcher.got and time.monotonic() < end:
        QApplication.instance().processEvents()
        time.sleep(0.01)
    return catcher.value


def _status(renderer) -> dict:
    v = _run_js(renderer, "window.VRM ? JSON.stringify(window.VRM.status()) : null")
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return {}
    return v if isinstance(v, dict) else {}


def _snapshot(renderer, app) -> str:
    v = _run_js(renderer, "window.VRM ? window.VRM.snapshot() : ''")
    return v if isinstance(v, str) else ""


def _metrics(renderer) -> dict:
    v = _run_js(renderer, "window.VRM ? JSON.stringify(window.VRM.metrics()) : null")
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return {}
    return v if isinstance(v, dict) else {}


if __name__ == "__main__":
    raise SystemExit(main())
