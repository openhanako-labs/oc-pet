#!/usr/bin/env python3
"""OC 桌宠父进程监督器（watchdog）。

为什么需要它：
    pet.py / main.py 一旦崩溃（如 0x8001010d COM apartment 错误），进程直接死掉，
    用户桌面的宠物消失 —— 信任清零。本 launcher 作为父进程，负责：
      1) 拉起 main.py 子进程；
      2) 子进程异常退出（非 0 / 被信号杀）时，等 3 秒后自动重拉（自复活）；
      3) 子进程正常退出（退出码 0，用户主动退出）时不重启；
      4) 防疯转：连续重启过于频繁时放弃，避免死循环刷屏。

启动方式：
    start_pet.bat 现已指向本文件。用户无感，双击行为不变。
"""
from __future__ import annotations

import os
import sys
import time
import signal
import subprocess
import logging
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [LAUNCHER] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("launcher")

HERE = Path(__file__).resolve().parent
# 默认入口；可用 OC_MAIN 环境变量覆盖（便于测试或将来换入口），
# 传入的路径若非绝对路径则相对仓库根解析。
_main_env = os.environ.get("OC_MAIN", "")
if _main_env:
    MAIN = (HERE / _main_env) if not os.path.isabs(_main_env) else Path(_main_env)
else:
    MAIN = HERE / "main.py"

# ── 可调参数 ──
RESTART_DELAY = 3.0          # 崩溃后等待秒数再重启
MAX_RESTARTS_PER_WINDOW = 20 # 时间窗内最多重启次数
RESTART_WINDOW = 600.0       # 重启计数时间窗（秒，默认 10 分钟）
HEALTHY_UPTIME = 30.0        # 收到就绪哨兵后运行超过此秒数视为健康，重置重启计数
READY_TIMEOUT = 120.0        # 子进程业务就绪超时（秒）：import/初始化超过此时间视为启动失败，kill
READY_POLL_INTERVAL = 0.5    # 就绪哨兵轮询间隔（秒）


def _ready_flag_path(pid: int) -> Path:
    """子进程业务就绪哨兵路径：logs/ready_<pid>.flag（由 main.py 在业务就绪后写入）。"""
    return HERE / "logs" / f"ready_{pid}.flag"


def _remove_ready_flag(pid: int) -> None:
    """删除就绪哨兵（子进程退出后兜底清理，防残留）。"""
    try:
        flag = _ready_flag_path(pid)
        if flag.exists():
            flag.unlink()
    except Exception:
        log.debug("launcher: 非致命异常(已静默吞掉)", exc_info=True)


def _latest_crash_dump() -> "Path | None":
    """返回 logs/ 下最新的 crash_dump_*.zip（不存在返回 None）。"""
    logs_dir = HERE / "logs"
    if not logs_dir.is_dir():
        return None
    try:
        zips = sorted(
            logs_dir.glob("crash_dump_*.zip"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except Exception:
        return None
    return zips[0] if zips else None


def _resolve_python() -> str:
    """优先用与 launcher 相同的解释器；否则回退到 start_pet.bat 的逻辑。"""
    # 若在 .venv 内运行则直接用当前解释器
    if (HERE / ".venv" / "Scripts" / "python.exe").exists() and ".venv" in sys.executable:
        return sys.executable
    # 优先 .venv
    venv_py = HERE / ".venv" / "Scripts" / "python.exe"
    if venv_py.exists():
        return str(venv_py)
    # 否则用当前解释器
    return sys.executable


def main() -> int:
    python = _resolve_python()
    log.info("OC 桌宠监督器启动 | python=%s | main=%s", python, MAIN.name)

    restart_timestamps: list[float] = []
    child: "subprocess.Popen | None" = None
    stopping = False

    def _terminate_child():
        if child and child.poll() is None:
            log.info("正在终止子进程 pid=%s", child.pid)
            child.terminate()
            try:
                child.wait(timeout=8)
            except subprocess.TimeoutExpired:
                child.kill()

    def _on_signal(signum, _frame):
        nonlocal stopping
        stopping = True
        log.info("收到退出信号 %s，停止监督", signum)
        _terminate_child()
        # 退出前清理就绪哨兵：子进程可能已写 ready_<pid>.flag，
        # 残留文件在 pid 复用（极低概率）时会造成下次"瞬时就绪"误判。
        if child is not None:
            try:
                _remove_ready_flag(child.pid)
            except Exception:
                log.debug("launcher: 非致命异常(已静默吞掉)", exc_info=True)
        sys.exit(0)

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    while not stopping:
        # 防疯转：清理时间窗外的重启记录
        now = time.time()
        restart_timestamps = [t for t in restart_timestamps if now - t < RESTART_WINDOW]
        if len(restart_timestamps) >= MAX_RESTARTS_PER_WINDOW:
            log.critical(
                "重启次数过多（%d 次 / %ds），疑似死循环崩溃，停止自动复活。"
                "请检查 logs/ 下的崩溃包。",
                len(restart_timestamps), int(RESTART_WINDOW),
            )
            return 1

        log.info("启动子进程…")
        try:
            child = subprocess.Popen(
                [python, str(MAIN), *sys.argv[1:]],
                cwd=str(HERE),
                # 继承 stdout/stderr，让桌宠日志正常显示
                stdout=None,
                stderr=None,
            )
        except Exception as e:  # 拉起失败（极少）
            log.error("无法启动子进程: %s", e)
            time.sleep(RESTART_DELAY)
            continue

        # ── 等待业务就绪哨兵（logs/ready_<pid>.flag）──
        # 只有收到哨兵后才开始计 uptime。子进程 import/初始化期可能长达 30s+，
        # 此期间崩溃属于“启动失败”，不能当作“健康运行后偶发崩溃”而重置重启计数
        # （否则启动期反复崩溃会无限重启）。超过 READY_TIMEOUT 视为启动超时，kill。
        child_pid = child.pid
        ready_flag = _ready_flag_path(child_pid)
        ready_time: "float | None" = None
        launch_started = time.time()
        while child.poll() is None:
            if ready_flag.exists():
                ready_time = time.time()
                log.info("子进程业务就绪（pid=%s，启动耗时 %.1fs）",
                         child_pid, ready_time - launch_started)
                break
            if time.time() - launch_started > READY_TIMEOUT:
                log.error("子进程 %s 在 %ds 内未就绪，判定启动失败，强制终止",
                          child_pid, int(READY_TIMEOUT))
                child.kill()
                break
            time.sleep(READY_POLL_INTERVAL)

        exit_code = child.wait()
        # 清理就绪哨兵（收到哨兵时已删一次；这里兜底防残留）
        _remove_ready_flag(child_pid)
        if stopping:
            break

        if exit_code == 0:
            log.info("子进程正常退出（退出码 0），监督器结束。")
            return 0

        # 异常退出：记录时间，判断健康度
        if ready_time is not None:
            uptime = time.time() - ready_time
        else:
            uptime = time.time() - launch_started
        restart_timestamps.append(time.time())
        if ready_time is not None and uptime >= HEALTHY_UPTIME:
            # 收到过就绪哨兵且健康运行过一段时间才崩，视为偶发，重置计数
            restart_timestamps = restart_timestamps[-1:]
        elif ready_time is None:
            log.warning("子进程未发出业务就绪哨兵即退出（启动期崩溃），不重置重启计数")

        log.warning(
            "子进程异常退出（退出码 %s，运行 %.1fs）。%s 后自动复活…",
            exit_code, uptime, RESTART_DELAY,
        )
        # 崩溃现场提示：把最新 crash_dump zip 路径打到控制台，用户/排查者可直达
        latest_zip = _latest_crash_dump()
        if latest_zip is not None:
            log.warning(
                "崩溃现场已打包: %s（含线程堆栈/C扩展列表/日志尾部，可直接解压查看）",
                latest_zip,
            )
        time.sleep(RESTART_DELAY)

    return 0


if __name__ == "__main__":
    sys.exit(main())
