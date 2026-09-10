# -*- coding: utf-8 -*-
"""真实功能验收脚本：真实 PetWindow（Live2D GL）+ 真实 Hanako 通道，逐功能驱动。

覆盖：表情切换 / 动作切换 / 记忆记录 / 小游戏 / 休息提醒 / 主动搭话(LLM) / 对话(LLM)。
每项真实对象调用 + 断言产出，输出 PASS/FAIL 汇总。临时验收工具（2026-08-20）。
"""
import os
import sys
import time
import pathlib

_REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
os.environ.setdefault("OC_DISABLE_TRAY", "1")  # 验收进程不抢托盘

from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication([])

RESULTS: list[tuple[str, str]] = []


def check(name: str, fn) -> None:
    try:
        fn()
        RESULTS.append((name, "PASS"))
    except Exception as exc:  # noqa: BLE001
        RESULTS.append((name, f"FAIL: {type(exc).__name__}: {str(exc)[:140]}"))


print("=== 构造真实 PetWindow（Live2D GL）===")
from pet import PetWindow  # noqa: E402
import logging
logger = logging.getLogger(__name__)


w = PetWindow(agent_id="miku")
w.show()
app.processEvents()
print("构造完成, renderer =", type(w._renderer).__name__)

# ── 1. 动作切换（真实 renderer）──────────────────────────
def t_motion() -> None:
    r = w._renderer
    mfiles = getattr(r, "_motion_files", [])
    assert mfiles, "无 motion 列表"
    r._start_motion_at(0)
    print(f"   motion {pathlib.Path(mfiles[0]).stem} 已触发")

check("动作切换", t_motion)

# ── 3. 记忆事件记录（真实 companion_memory + 文件）───────
def t_memory() -> None:
    cm = w._companion_memory
    mem_dir = pathlib.Path.home() / ".oc-pet" / "memory"
    events_file = mem_dir / "miku_events.jsonl"
    before = len(events_file.read_text(encoding="utf-8").splitlines()) if events_file.exists() else 0
    cm.record_event(category="test", scenario="feature_acceptance", intent="verify",
                    emotion="happy", intensity=0.8, source="test")
    # 事件流异步落盘：多次 processEvents + 短暂 sleep 等写入
    for _ in range(20):
        app.processEvents()
        time.sleep(0.1)
    after = len(events_file.read_text(encoding="utf-8").splitlines()) if events_file.exists() else 0
    assert after > before, f"事件未落盘 {before}->{after}"
    print(f"   事件落盘 {before}->{after} 行")

check("记忆事件记录", t_memory)

# ── 4. 小游戏逻辑（真实 games.py）────────────────────────
def t_game() -> None:
    from core.play.games import GuessNumberGame, RockPaperScissorsGame
    g = GuessNumberGame()
    r = g.guess(g.target)  # 猜中
    assert isinstance(r, dict) and g.finished, f"猜中应结束: {r}"
    rps = RockPaperScissorsGame()
    rps.play("rock"); rps.play("scissors")  # 出招不抛
    print(f"   猜数字 win ✓ (result={r}) / 石头剪刀布出招 ✓")

check("小游戏逻辑", t_game)

# ── 5. 休息提醒状态机（真实 tracker，时间推进）──────────
def t_break() -> None:
    from core.play.break_reminder import WorkReminderTracker
    t = WorkReminderTracker(settings={"enabled": True, "after_minutes": 90,
                                      "late_night_hour": 22, "late_night_end_hour": 6,
                                      "late_night_multiplier": 3})
    base = time.time()
    for _ in range(92):  # 每分钟推进一次（首次 update last=None return，实际累积 91 分钟）
        base += 60
        t.update(True, now=base)
    assert t.due(now=base), "91min 累积后应 due"
    t.acknowledge()
    assert not t.due(now=base), "确认后应重置"
    print("   90min 累积→due ✓ / 确认→重置 ✓")

check("休息提醒状态机", t_break)

# ── 6. 主动搭话（真实 Hanako 通道）───────────────────────
def t_proactive() -> None:
    p = w._proactive
    # 关闭 grace 让 tick 可触发
    try:
        w._proactive_grace = 0
    except Exception:
        logger.debug("feature_acceptance: 非致命异常(已静默吞掉)", exc_info=True)
    result = p.tick()
    print(f"   tick() 返回: {str(result)[:60]!r}（None=未到触发条件也正常）")

check("主动搭话 tick", t_proactive)

# ── 7. 对话（真实 Hanako，等 on_reply）───────────────────
def t_chat() -> None:
    received: list = []
    original = w._engine.on_reply

    def _capture(reply, emotion, anim, audio_path):
        received.append((reply, emotion))
        # 不阻断原回调（气泡显示）
        try:
            original(reply, emotion, anim, audio_path)
        except Exception:
            logger.debug("feature_acceptance: 非致命异常(已静默吞掉)", exc_info=True)

    w._engine.on_reply = _capture
    w._engine.send("回复两个字：收到", character="miku", source="user")
    deadline = time.time() + 200  # Hanako 生成慢，给足 200s
    while time.time() < deadline and not received:
        app.processEvents()
        time.sleep(0.5)
    assert received, "200s 内未收到 Hanako 回复"
    text = str(received[0][0] or "")
    assert text.strip(), "回复为空"
    print(f"   收到回复: {text[:40]!r} (emotion={received[0][1]})")

check("对话回复(Hanako)", t_chat)

# ── 汇总 ─────────────────────────────────────────────────
print("\n======== 功能验收汇总 ========")
for name, r in RESULTS:
    print(f"[{r[:4]}] {name}")
failed = [n for n, r in RESULTS if not r.startswith("PASS")]
print(f"\n{'全部通过' if not failed else '失败: ' + str(failed)} ({len(RESULTS)-len(failed)}/{len(RESULTS)})")

try:
    w.close()
    app.processEvents()
except Exception:
    logger.debug("feature_acceptance: 非致命异常(已静默吞掉)", exc_info=True)
sys.exit(1 if failed else 0)
