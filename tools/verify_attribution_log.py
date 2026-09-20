"""实测：LLM 归因日志是否真的会输出（2026-09-19）。

不是单测，是**端到端冒烟**：真的建 QApplication + PetWindow，
把 _llm_attribution_timer 的间隔压到 100ms，等它超时，看日志有没有打出来。

验证三件事：
  1. 定时器被创建并启动（不是静默失败）
  2. 超时后真的调用了 attribution_report()
  3. closeEvent 会把它停掉（不泄漏）

用法：python tools/verify_attribution_log.py
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("OC_DISABLE_LIVE2D", "1")
os.environ.setdefault("OC_DISABLE_PERCEPTION", "1")
os.environ.setdefault("OC_DISABLE_TRAY", "1")

import logging  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)


class _Capture(logging.Handler):
    """抓日志，判断归因行是否出现。"""
    def __init__(self):
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record):
        try:
            self.lines.append(record.getMessage())
        except Exception:
            pass


def main() -> int:
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv)

    cap = _Capture()
    logging.getLogger().addHandler(cap)
    logging.getLogger().setLevel(logging.INFO)

    # ── 构造 PetWindow ──
    from pet import PetWindow

    w = PetWindow(agent_id="ophelia")

    timer = getattr(w, "_llm_attribution_timer", None)
    print(f"[1] 定时器存在: {timer is not None}")
    if timer is None:
        print("    ✗ 未创建 —— 归因日志不会输出")
        return 1
    print(f"[1] 运行中: {timer.isActive()}, 间隔: {timer.interval()}ms")

    # ── 压到 100ms，并制造一些归因数据 ──
    from core.llm_gate import get_gate

    g = get_gate()
    g.notify_429("vision")
    g.notify_429("enrich")
    g.notify_429("proactive")
    print(f"[2] 归因数据: {g.attribution_report()}")

    timer.setInterval(100)
    timer.start()

    # ── 等超时 ──
    # ⚠ 不能用 "LLM 归因" 做判据：启动日志「LLM 归因日志已启用：每 10 分钟一条」
    # 也含这个子串，会造成假阳性。真正的归因行以「LLM 归因 | 」开头（带竖线）。
    before = len(cap.lines)
    deadline = time.time() + 5.0
    hit = False
    while time.time() < deadline:
        app.processEvents()
        if any(ln.startswith("LLM 归因 |") for ln in cap.lines):
            hit = True
            break
        time.sleep(0.02)

    print(f"[3] 超时后打出归因行（严格匹配 'LLM 归因 |'）: {hit}")
    if hit:
        for ln in cap.lines[before:]:
            if ln.startswith("LLM 归因 |"):
                print(f"    -> {ln}")
    else:
        print("    x 定时器超时未打出归因行")

    # ── 关闭应停掉定时器 ──
    try:
        w.close()
        app.processEvents()
        stopped = not timer.isActive()
    except Exception as e:
        print(f"    close 异常: {e}")
        stopped = False
    print(f"[4] closeEvent 后已停止: {stopped}")

    ok = hit and stopped
    print()
    print("=" * 50)
    print("归因日志端到端: 通过 ✓" if ok else "归因日志端到端: 失败 ✗")
    print("=" * 50)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
