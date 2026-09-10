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

HERE = Path(__file__).resolve().parent

# ── 日志（2026-09-10 加固）──
# 原来只靠 basicConfig 的 stderr StreamHandler。实测事故：HanaAgent → cmd /c →
# launcher → main.py，cmd 的 stderr 是**无人读取的管道**，写满后 launcher 自己
# 也卡在 log.critical 上，看门狗无法复活子进程。现在改成：
#   1) 写 logs/launcher.log（文件不会因“没人读”而填满）
#   2) 文件 handler 就位后，摘掉可能阻塞的 stderr handler
# 注意顺序：必须先保证文件 handler 存在，摘 stderr 才不会把日志弄丢。
_LOG_FMT = "[%(asctime)s] [LAUNCHER] %(levelname)s: %(message)s"
_LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"

logging.basicConfig(level=logging.INFO, format=_LOG_FMT, datefmt=_LOG_DATEFMT)

try:
    from logging.handlers import RotatingFileHandler
    (_log_dir := HERE / "logs").mkdir(parents=True, exist_ok=True)
    _fh = RotatingFileHandler(
        _log_dir / "launcher.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8",
    )
    _fh.setFormatter(logging.Formatter(_LOG_FMT, datefmt=_LOG_DATEFMT))
    logging.getLogger().addHandler(_fh)
except Exception:
    pass  # 文件日志不可用不影响监督（下面也不会摘 stderr，因为无 durable handler）

try:
    from log_setup import drop_blocking_stderr_handler
    drop_blocking_stderr_handler()
except Exception:
    pass  # 加固失败不影响监督循环

log = logging.getLogger("launcher")

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
# 真卡死是永久性的，超时放宽不影响检测效果，但能避免误杀健康进程。
# 实测启动期主线程忙（模型 fit/绘图）会延迟 Qt 计时器约 25s，故阈值取 90s 留足余量。
HEARTBEAT_TIMEOUT = 90.0      # 心跳超过此秒数未更新 → 判定主线程卡死（GUI hang）
HEARTBEAT_POLL_INTERVAL = 3.0 # 看门狗轮询心跳间隔（秒）；需远小于 HEARTBEAT_TIMEOUT


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


def _heartbeat_path(pid: int) -> Path:
    """子进程主线程心跳文件路径：logs/heartbeat_<pid>.txt（由 main.py 周期更新）。"""
    return HERE / "logs" / f"heartbeat_{pid}.txt"


def _remove_heartbeat(pid: int) -> None:
    """删除心跳文件（子进程退出后兜底清理，防残留）。"""
    try:
        p = _heartbeat_path(pid)
        if p.exists():
            p.unlink()
    except Exception:
        log.debug("launcher: 非致命异常(已静默吞掉)", exc_info=True)


def _watchdog_wait(child, child_pid: int, ready_time: "float | None") -> bool:
    """主线程存活看门狗：阻塞到子进程退出，返回是否因「判定卡死」而强杀。

    为什么需要：launcher 原本只在子进程**异常退出**时自动复活；但 GUI 主线程卡死
    （如 Live2D WebView stall）时进程并不退出，看门狗永远等不到退出事件 —— 桌宠
    就永久假死在桌面上，只能人工干预。本函数补齐「卡死」这一半。

    原理：子进程 main.py 用主线程 Qt 计时器每 ~5s 更新心跳文件；主线程一旦冻结，
    计时器不再触发、心跳停止更新。此处若超过 HEARTBEAT_TIMEOUT 未更新即判定卡死。

    防误杀三道保险：
      1) 仅在子进程曾发出业务就绪哨兵（ready_time 非 None）后启用 —— 启动期导入
         重型依赖/模型 fit 可能很久，且那时心跳尚未开始；
      2) 心跳文件尚不存在时（子进程还没跑到心跳初始化）不计时，只重置基准；
      3) HEARTBEAT_TIMEOUT 取 90s 而非 45s —— 实测启动期主线程忙会延迟 Qt 计时器
         约 25s，阈值太小会把"正在忙"误判成"已卡死"。真卡死是永久性的，放宽无损。

    抽成独立函数是为了可测：真实起一个「写完心跳就挂起」的子进程即可验证。
    """
    last_beat = time.time()
    while child.poll() is None:
        if ready_time is not None:
            try:
                hb = _heartbeat_path(child_pid)
                if hb.exists():
                    mtime = hb.stat().st_mtime
                    if mtime > last_beat:
                        last_beat = mtime
                    elif time.time() - last_beat > HEARTBEAT_TIMEOUT:
                        # 先杀后记（2026-09-10）：看门狗的职责是恢复，不能被日志挡住。
                        # 实测事故（HanaAgent → cmd /c → launcher → main.py）：stderr 是
                        # 无人读取的管道，写满后原来那句 log.critical 永久阻塞 → 永远走不到
                        # child.kill() → “自动复活”失效，进程卡死 9 分钟无人管。
                        child.kill()
                        log.critical(
                            "子进程主线程疑似卡死（心跳停滞 %ds，最后心跳 %s），"
                            "已强制终止以触发自动复活",
                            int(time.time() - last_beat),
                            time.strftime("%H:%M:%S", time.localtime(last_beat)),
                        )
                        return True
                else:
                    # 心跳文件尚未创建：不计时，避免把"还没开始心跳"误判成"心跳停了"
                    last_beat = time.time()
            except Exception:
                log.debug("launcher: 非致命异常(已静默吞掉)", exc_info=True)
        time.sleep(HEARTBEAT_POLL_INTERVAL)
    return False


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


# 启动自检只打印一次（每次 launcher 进程），避免自动复活时刷屏
_startup_check_reported = [False]


def _report_startup_env() -> None:
    """启动失败时追加环境自检。

    2026-09-10 接线 core/startup_check.py（此前零调用方）。
    crash_dump 回答「怎么挂的」，自检回答「缺什么」——互补，不重复。
    任何失败都不影响监督循环（诊断不能把监督器搞挂）。
    """
    try:
        from core.startup_check import run_startup_check
        report = run_startup_check()
    except Exception as e:
        log.warning("启动自检不可用（跳过）: %s", e)
        return
    try:
        report.print()
        if not report.all_ok:
            failed = [r.name for r in report.results if not r.ok]
            log.warning("启动环境缺失项: %s", "、".join(failed))
    except Exception as e:
        log.warning("启动自检报告输出失败: %s", e)


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
                _remove_heartbeat(child.pid)
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

        # ── 主线程存活看门狗：检测 GUI 卡死（进程未退出但事件循环冻结）──
        # 子进程 main.py 每 ~5s 更新一次心跳文件（logs/heartbeat_<pid>.txt）；
        # 若超过 HEARTBEAT_TIMEOUT 未更新，判定主线程卡死（如 Live2D WebView stall），
        # 强杀子进程以触发下方的自动复活。仅在该子进程曾发出业务就绪哨兵后启用，
        # 避免误杀启动期的慢加载（导入重型依赖可能 30s+）。
        _watchdog_wait(child, child_pid, ready_time)
        exit_code = child.wait()
        # 清理就绪哨兵与心跳文件（收到哨兵/心跳时已删一次；这里兜底防残留）
        _remove_ready_flag(child_pid)
        _remove_heartbeat(child_pid)
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
        # 启动期崩溃时追加环境自检（父子同一环境，等价）。
        # 仅在未发出就绪哨兵（=启动失败）时跑，且每次 launcher 进程只跑一次。
        if ready_time is None and not _startup_check_reported[0]:
            _startup_check_reported[0] = True
            _report_startup_env()
        time.sleep(RESTART_DELAY)

    return 0


if __name__ == "__main__":
    sys.exit(main())
