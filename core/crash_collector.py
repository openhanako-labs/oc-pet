"""崩溃现场自动收集器。

目标：桌宠崩溃（尤其是 0x8001010d 这类 C 层 COM 错误，Python traceback 抓不到）
时，自动把"破案线索"打包成一个 zip，方便事后定位根因，而不必再靠猜。

收集内容：
    - crash_trace.txt        （faulthandler 抓的 C 层栈，若已生成）
    - oc_pet.log 尾部 2000 行（最近运行上下文）
    - sys.modules 中所有 C 扩展（.pyd/.so）列表 —— 用来锁定"谁偷偷初始化了 COM"
    - 当前存活线程快照（线程名 + 堆栈）
    - 环境信息（Python 版本、解释器路径、启动参数）

触发时机：
    - atexit（正常/异常退出都会跑，但 segfault 不一定能跑）
    - 增强的 sys.excepthook（Python 异常退出必跑）
    - faulthandler 的 dump 回调（C 层崩溃时，在写栈之后调一次）

注意：本模块只做"收集"，不负责重启——重启由 launcher.py 父进程负责。
"""
from __future__ import annotations

import os
import sys
import time
import atexit
import traceback
import threading
import logging
from pathlib import Path

log = logging.getLogger(__name__)

_COLLECTED = False

# 本进程启动时刻（模块导入时间）。用于区分 crash_trace.txt 是「上次崩溃的
# 遗留」还是「本次运行期刚写下的」——比固定秒数阈值可靠。
_PROCESS_START = time.time()


class _PydImportTracker:
    """记录每个 C 扩展（.pyd/.so）首次被哪个线程 import。

    通过 meta_path finder 在导入发生时打点（只记录、不拦截、永远返回 None），
    崩溃包 c_extensions.txt 里可标注"谁在哪个线程拉起了 C 扩展"——
    0x8001010d 这类 COM 错误常与"在错误 apartment 的线程里初始化 COM"有关，
    知道导入线程才能定位是哪个初始化路径闯的祸。
    """

    def __init__(self) -> None:
        # module_name -> (线程名, 线程 ident)
        self.imported_by: dict[str, tuple[str, int | None]] = {}

    def find_spec(self, fullname, path=None, target=None):
        try:
            if fullname not in self.imported_by:
                import importlib.machinery as _mach
                spec = _mach.PathFinder.find_spec(fullname, path)
                if spec is not None and spec.origin and (
                    spec.origin.endswith(".pyd") or spec.origin.endswith(".so")
                ):
                    cur = threading.current_thread()
                    self.imported_by[fullname] = (cur.name, cur.ident)
        except Exception:
            log.debug("crash_collector: 非致命异常(已静默吞掉)", exc_info=True)
        return None  # 永不拦截，交给正常导入机制

    @classmethod
    def install(cls) -> "_PydImportTracker":
        """插入 meta_path 首位（须在重型依赖 import 之前调用）。"""
        tracker = cls()
        sys.meta_path.insert(0, tracker)
        return tracker


_PYD_TRACKER = _PydImportTracker.install()


def _project_root() -> Path:
    # main.py 在仓库根；本文件在 core/ 下
    return Path(__file__).resolve().parent.parent


def _collect_once(reason: str) -> str | None:
    """执行一次收集，返回生成的 zip 路径或 None。幂等（只跑一次）。"""
    global _COLLECTED
    if _COLLECTED:
        return None
    _COLLECTED = True

    root = _project_root()
    logs_dir = root / "logs"
    logs_dir.mkdir(exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    zip_path = logs_dir / f"crash_dump_{ts}.zip"

    try:
        import zipfile
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            # 1) crash_trace.txt
            ct = root / "crash_trace.txt"
            if ct.exists():
                zf.write(ct, "crash_trace.txt")

            # 2) oc_pet.log 尾部
            logfile = logs_dir / "oc_pet.log"
            if logfile.exists():
                try:
                    with open(logfile, "r", encoding="utf-8", errors="replace") as f:
                        lines = f.readlines()
                    tail = "".join(lines[-2000:])
                    zf.writestr("oc_pet.log.tail.txt", tail)
                except Exception as e:
                    zf.writestr("oc_pet.log.tail.txt", f"(读取失败: {e})")

            # 3) C 扩展列表（锁定 COM 嫌疑）+ 每个 .pyd 首次被哪个线程 import
            c_exts = []
            for name, mod in list(sys.modules.items()):
                try:
                    fn = getattr(mod, "__file__", None)
                    if fn and (fn.endswith(".pyd") or fn.endswith(".so") or ".pyd" in fn):
                        imp = _PYD_TRACKER.imported_by.get(name)
                        if imp is not None:
                            imp_note = f"\t(首次 import 线程: {imp[0]}, ident={imp[1]})"
                        else:
                            imp_note = "\t(首次 import 线程: 未知/模块启动前已加载)"
                        c_exts.append(f"{name}\t{fn}{imp_note}")
                except Exception:
                    log.debug("crash_collector: 非致命异常(已静默吞掉)", exc_info=True)
            # 也列出可疑的 COM 相关顶层模块是否曾被导入
            com_suspects = [m for m in sys.modules
                            if m.split(".")[0] in
                            ("pythoncom", "win32com", "pywin", "comtypes", "ctypes")]
            c_exts.append("")
            c_exts.append("=== COM 相关模块是否曾被导入 ===")
            c_exts.append("\n".join(com_suspects) if com_suspects else "(无)")
            zf.writestr("c_extensions.txt", "\n".join(c_exts))

            # 4) 线程快照（含每线程堆栈：0x8001010d 这类 C 层 COM 错误常与
            #    某个线程在错误 apartment 里初始化 COM 有关，只有栈才能定位）
            frames = {}
            try:
                frames = sys._current_frames()
            except Exception:
                log.debug("crash_collector: 非致命异常(已静默吞掉)", exc_info=True)
            threads = []
            for t in threading.enumerate():
                stack_lines: list[str] = []
                if t.ident in frames:
                    try:
                        stack_lines = traceback.format_stack(frames[t.ident])
                    except Exception:
                        stack_lines = ["    (无法获取堆栈)"]
                header = f"[thread] {t.name} (daemon={t.daemon}, alive={t.is_alive()}, ident={t.ident})"
                body = "".join(stack_lines).rstrip("\n") if stack_lines else "    (无活动帧)"
                threads.append(f"{header}\n{body}")
            zf.writestr("threads.txt", "\n\n".join(threads))

            # 5) 环境信息
            env = [
                f"reason={reason}",
                f"time={ts}",
                f"python={sys.version}",
                f"executable={sys.executable}",
                f"argv={sys.argv}",
                f"platform={sys.platform}",
            ]
            zf.writestr("env.txt", "\n".join(env))

        log.warning("崩溃现场已打包: %s", zip_path)
        return str(zip_path)
    except Exception as e:
        try:
            log.error("崩溃收集失败: %s\n%s", e, traceback.format_exc())
        except Exception:
            log.debug("crash_collector: 非致命异常(已静默吞掉)", exc_info=True)
        return None


def install(reason_default: str = "unknown") -> None:
    """在 main.py 早期调用，安装所有钩子。"""
    # atexit：正常/Python 异常退出都会触发（segfault 不一定）
    try:
        atexit.register(lambda: _collect_once("atexit"))
    except Exception:
        log.debug("crash_collector: 非致命异常(已静默吞掉)", exc_info=True)

    # 增强 excepthook：Python 未捕获异常时必跑
    _orig = sys.excepthook
    def _hook(etype, exc, tb):
        try:
            _collect_once(f"python_exc:{etype.__name__}")
        except Exception:
            log.debug("crash_collector: 非致命异常(已静默吞掉)", exc_info=True)
        _orig(etype, exc, tb)
    try:
        sys.excepthook = _hook
    except Exception:
        log.debug("crash_collector: 非致命异常(已静默吞掉)", exc_info=True)

    # 注：faulthandler 的 C 层 dump 时机无法注入 Python 回调，但它会把
    # crash_trace.txt 写到磁盘；C 层崩溃时 atexit 不一定跑，因此我们在
    # install() 末尾显式检查"上次遗留的 crash_trace.txt"并补打包（见下）。
    try:
        _collect_stale_crash()
    except Exception:
        log.debug("crash_collector: 非致命异常(已静默吞掉)", exc_info=True)


def _collect_stale_crash() -> str | None:
    """启动时检查上次崩溃遗留的 crash_trace.txt（C 层崩溃 atexit 未跑的情况），
    若有则补打包，避免线索丢失。"""
    root = _project_root()
    ct = root / "crash_trace.txt"
    if not ct.exists():
        return None
    # 仅当文件早于本进程启动时刻，才视为上次崩溃的遗留。
    # 2026-09-21：原先用「年龄 < 30s 视为当前运行期，跳过」的魔法数，与
    # launcher 的 RESTART_DELAY=3s 直接打架——自动复活路径下每次重启都被判
    # "太新"，于是崩溃线索系统性丢失（日志里最新现场永远是几天前的旧包）。
    # 换成与进程启动时刻比较，语义精确，也不再依赖重启延迟这个易变常量。
    try:
        if ct.stat().st_mtime >= _PROCESS_START:
            return None
    except Exception:
        log.debug("crash_collector: 非致命异常(已静默吞掉)", exc_info=True)
    return _collect_once("stale_crash_trace_on_startup")
