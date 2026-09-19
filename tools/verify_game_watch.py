# -*- coding: utf-8 -*-
"""陪玩 P0-1（游戏窗口识别）真机验证。

分两段，都用**真实** Windows API，不造假数据：

1. **窗口探测层**：枚举当前可见顶层窗口、按进程名模式查找——证明
   「游戏还在跑吗」这个判据在本机真的能用。
2. **会话流转（接真实探针）**：用真实存在的进程（explorer.exe）当"游戏"，
   走一遍 启动 → 切后台 → 切回 → 换游戏 → 退出，打印每一步事件与
   总线上收到的消息。

只读：不弹窗、不发气泡、不改配置、不碰桌宠进程。

用法：
    python tools/verify_game_watch.py
"""
from __future__ import annotations

import sys

sys.path.insert(0, ".")

from core.event_bus import EventBus  # noqa: E402
from core.game.registry import GameEntry, load_games, match_game  # noqa: E402
from core.game.session import EVENT_BUS_NAME, GameSessionWatcher, emit_session_events  # noqa: E402
from motion.foreground_watcher import find_process_windows, list_visible_windows  # noqa: E402

LINE = "-" * 68


def part1_probe() -> bool:
    print(LINE)
    print("① 窗口探测层（真实 Windows API）")
    print(LINE)
    wins = list_visible_windows()
    if wins is None:
        print("  ✗ EnumWindows 整体失败——探针不可用")
        return False
    print("  可见顶层窗口：%d 个" % len(wins))
    named = [w for w in wins if w.get("process")]
    print("  能取到进程名：%d 个（其余权限不足/已退出）" % len(named))

    procs = {}
    for w in named:
        procs[w["process"].lower()] = procs.get(w["process"].lower(), 0) + 1
    top = sorted(procs.items(), key=lambda kv: -kv[1])[:5]
    print("  进程分布 top5：%s" % "、".join("%s×%d" % (p, c) for p, c in top))

    # 拿一个真实存在的进程验证"正向命中"
    probe_target = named[0]["process"] if named else ""
    if not probe_target:
        print("  ⚠ 取不到任何进程名，跳过正向命中测试")
        return False
    hits = find_process_windows([probe_target])
    print("  正向命中测试：find_process_windows([%r]) → %s 个窗口"
          % (probe_target, len(hits) if hits is not None else "None"))

    ghost = find_process_windows(["zzz_definitely_not_running_game.exe"])
    print("  反向命中测试：不存在的进程 → %s（应为 0）" % (len(ghost) if ghost is not None else "None"))

    ok = bool(hits) and ghost is not None and len(ghost) == 0
    print("  结果：%s" % ("✓ 探测层可用" if ok else "✗ 探测层异常"))
    return ok


def part2_session(real_process: str) -> bool:
    print()
    print(LINE)
    print("② 会话流转（真实探针，真实进程当“游戏”）")
    print(LINE)
    # 真实存在的进程 + 一个绝不存在的"幽灵游戏"
    real = GameEntry(id="probe_real", name="探针·真实进程",
                     process=[real_process.lower()], title=[])
    ghost = GameEntry(id="probe_ghost", name="探针·幽灵游戏",
                      process=["zzz_definitely_not_running_game.exe"], title=[])

    seen: list[str] = []

    def _on_bus(payload=None):
        seen.append(payload["type"])

    EventBus.on(EVENT_BUS_NAME, _on_bus)
    w = GameSessionWatcher(games=[real, ghost])

    def step(label: str, events: list):
        emit_session_events(events)
        pretty = [e["type"] for e in events] or ["（无事件）"]
        print("  %-34s → %s" % (label, "、".join(pretty)))

    try:
        step("前台切到 %s" % real_process, w.on_foreground(real_process, "探针窗口", "other"))
        step("切去看网页（进程仍在跑）", w.on_foreground("chrome.exe", "网页", "browsing"))
        step("周期检查（游戏仍在后台运行）", w.poll())
        step("切回游戏", w.on_foreground(real_process, "探针窗口", "other"))
        step("切到幽灵游戏", w.on_foreground("zzz_definitely_not_running_game.exe", "Ghost", "gaming"))
        step("再切走（幽灵进程查不到→退出）", w.on_foreground("chrome.exe", "网页", "browsing"))
    finally:
        EventBus.off(EVENT_BUS_NAME, _on_bus)

    print()
    print("  总线收到的序列：%s" % " → ".join(seen) if seen else "  （总线没收到任何事件）")
    expect = ["game_started", "game_background", "game_foreground",
              "game_started", "game_exited"]
    ok = seen == expect
    print("  期望序列：      %s" % " → ".join(expect))
    print("  结果：%s" % ("✓ 会话流转正确" if ok else "✗ 序列不符"))
    return ok


def part3_whitelist() -> None:
    print()
    print(LINE)
    print("③ 白名单（内置表，零配置）")
    print(LINE)
    games = load_games(None)
    print("  内置 %d 款：" % len(games))
    for g in games:
        print("    - %-12s %-16s 进程 %s | 标题 %s"
              % (g.name, g.type, g.process or "—", g.title or "—"))
    print()
    cases = [
        ("Wuthering Waves.exe", "鸣潮"),
        ("YuanShen.exe", "原神"),
        ("StarRail.exe", "崩坏：星穹铁道"),
        ("javaw.exe", "Minecraft 1.21 - 单人游戏"),
        ("Terraria.exe", "Terraria"),
        ("chrome.exe", "新标签页"),
    ]
    print("  识别抽查：")
    for proc, title in cases:
        g = match_game(proc, title, games)
        print("    %-24s %-26s → %s" % (proc, title[:24], g.name if g else "（不是游戏）"))


def main() -> int:
    print("陪玩 P0-1 · 游戏窗口识别 · 真机验证")
    print("时间：", __import__("time").strftime("%Y-%m-%d %H:%M:%S"))

    ok1 = part1_probe()
    wins = list_visible_windows() or []
    real = next((w["process"] for w in wins
                 if w.get("process", "").lower() == "explorer.exe"), "")
    if not real:
        real = next((w["process"] for w in wins if w.get("process")), "")
    ok2 = part2_session(real) if real else False
    if not real:
        print("\n  ⚠ 找不到可用真实进程，跳过会话流转验证")
    part3_whitelist()

    print()
    print(LINE)
    print("总结：探测层 %s ｜ 会话流转 %s"
          % ("✓" if ok1 else "✗", "✓" if ok2 else "✗"))
    return 0 if (ok1 and ok2) else 1


if __name__ == "__main__":
    sys.exit(main())
