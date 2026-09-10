#!/usr/bin/env python3
"""OC 桌面宠物 - 月曦夜 & 奥菲莉娅

多桌宠模式：每个 Hanako agent 可以独立运行一个桌宠窗口。
"""
import sys
import os
import logging
import faulthandler

# 抓 C++ 层崩溃栈（segfault / access violation）
faulthandler.enable(file=open('crash_trace.txt', 'w', encoding='utf-8'))

# ── 崩溃现场自动收集（打包 crash_trace + 日志尾部 + C扩展列表 + 线程快照）──
# 必须在重模块 import / 任何可能崩溃的操作之前安装，才能覆盖全程。
from core.crash_collector import install as _install_crash_collector
_install_crash_collector()


def _setup_file_logging():
    """将完整日志同时写入 logs/oc_pet.log（UTF-8 滚动），便于回看。

    控制台照常输出；文件日志让完整运行记录可留存，出问题能直接翻文件。
    日志级别从 config.json 读取（默认 INFO），支持 DEBUG/INFO/WARN。
    """
    try:
        from logging.handlers import RotatingFileHandler
        log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, "oc_pet.log")
        fh = RotatingFileHandler(
            log_path, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        # 日志级别：从 config.json 读取，默认 INFO
        log_level = logging.INFO
        try:
            import json
            config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
            if os.path.exists(config_path):
                with open(config_path, encoding="utf-8") as f:
                    config = json.load(f)
                level_str = str(config.get("log_level", "INFO").upper())
                if level_str == "DEBUG":
                    log_level = logging.DEBUG
                elif level_str == "WARN" or level_str == "WARNING":
                    log_level = logging.WARNING
        except Exception:
            logger.debug("main: 非致命异常(已静默吞掉)", exc_info=True)
        fh.setLevel(log_level)
        fh.setFormatter(logging.Formatter(
            '[%(asctime)s] [%(levelname)s] %(name)s: %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S',
        ))
        logging.getLogger().addHandler(fh)
        # 设置根日志级别
        logging.getLogger().setLevel(log_level)
        logging.getLogger(__name__).info("日志文件：%s（级别：%s）", log_path, logging.getLevelName(log_level))
    except Exception as _e:  # 日志文件不可用绝不影响主程序
        logging.getLogger(__name__).warning("无法初始化日志文件：%s", _e)

# ── 导入追踪探针（默认追踪 C 扩展重型依赖）──
# 用法：OC_TRACE_IMPORTS=funasr,wetext python main.py 覆盖默认追踪列表；
#       OC_TRACE_IMPORTS=（空）可整体关闭。
# 默认关闭（生产启动不被刷屏拖慢）：探针在每次 import 目标模块时打印完整
# 调用栈，默认开启会让 faster_whisper/ctranslate2/live2d 的几十个子模块导入
# 全部打印堆栈，显著拖慢启动（2026-08-20 实测日志刷屏 + 启动 35s+）。
# 定位 0x8001010d（COM 错误与"在哪个线程初始化 C 扩展"强相关）时，
# 显式设置 OC_TRACE_IMPORTS=faster_whisper,ctranslate2,live2d 再启动即可。
_trace = os.environ.get("OC_TRACE_IMPORTS")
if _trace is None:
    _trace = ""
if _trace:
    import traceback as _tb
    import threading as _th
    _trace_mods = {m.strip() for m in _trace.split(",") if m.strip()}
    class _ImportTracer:
        def find_spec(self, name, path, target=None):
            if name.split(".")[0] in _trace_mods:
                _cur = _th.current_thread()
                sys.stderr.write(
                    f"\n[OC_TRACE] import triggered: {name} "
                    f"(thread={_cur.name}, ident={_cur.ident})\n"
                    f"[OC_TRACE] tracing: {sorted(_trace_mods)}\n"
                )
                _tb.print_stack(file=sys.stderr)
                sys.stderr.flush()
            return None
    sys.meta_path.insert(0, _ImportTracer())
    sys.stderr.write(f"[OC_TRACE] 导入追踪探针已开启，追踪: {sorted(_trace_mods)}\n")
    sys.stderr.flush()

# ── 沙盒模式快捷开关 ──
if "--sandbox" in sys.argv:
    # 移除参数，委托给 sandbox_runner
    sys.argv.remove("--sandbox")
    from sandbox_runner import apply_patches, run_interactive
    logging.basicConfig(level=logging.INFO, format='[%(levelname)s] %(name)s: %(message)s')
    _setup_file_logging()
    apply_patches()
    run_interactive()
    sys.exit(0)

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QFont



# ── 主题系统（必须在 QApplication 创建后、其他 UI 之前） ──
from ui.theme import init_default

# ── 日志 ──
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
_setup_file_logging()

# ── 全局未捕获异常钩子 ──
# 桌宠闪退后自动重启却无 Python 堆栈，难定位根因。这里把任何未捕获异常
# 同时写进 logs/oc_pet.log（faulthandler 只抓 C 层崩溃，抓不到 Python 异常），
# 下次再崩直接翻日志即可看到完整 traceback，不必再靠猜。
def _install_excepthook():
    import traceback as _tb
    _root = logging.getLogger()
    # 链式：取当前已安装的钩子（crash_collector.install() 已先运行，这里拿到的是
    # crash_collector 的收集钩子；若它未安装则为默认 sys.__excepthook__）。
    # 不能直接调 sys.__excepthook__——那会跳过 crash_collector，导致 Python 异常
    # 退出时崩溃现场不再自动打包。
    _prev = getattr(sys, "excepthook", sys.__excepthook__)
    def _hook(etype, exc, tb):
        try:
            _root.critical("未捕获异常导致进程即将退出:\n%s",
                           "".join(_tb.format_exception(etype, exc, tb)))
        except Exception:
            logger.debug("main: 非致命异常(已静默吞掉)", exc_info=True)
        # 先调前一个钩子（crash_collector 收集 + 默认钩子保证 stderr/退出码），
        # 两个钩子都执行，崩溃现场仍能自动打包。
        try:
            _prev(etype, exc, tb)
        except Exception:
            logger.debug("main: 非致命异常(已静默吞掉)", exc_info=True)
    sys.excepthook = _hook
_install_excepthook()

# Add project root to path
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from pet_manager import PetManager
logger = logging.getLogger(__name__)



def main():
    # 检查沙盒标志
    app = QApplication(sys.argv)
    app.setApplicationName("OC Desktop Pet")
    app.setQuitOnLastWindowClosed(False)

    # 初始化主题系统（在所有 UI 创建前）
    theme_mgr = init_default(app)
    theme_mgr.apply_initial()
    logging.info("初始主题：%s", theme_mgr.current)

    # 根据 config 加载 theme_mode（auto/light/dark）
    from config import load_config, save_config
    cfg = load_config()
    theme_mode = cfg.get("theme_mode", "auto")
    if theme_mode in ("auto", "light", "dark"):
        theme_mgr.set_mode(theme_mode)
        logging.info("从 config 加载主题模式：%s", theme_mode)

    # Global font
    font = QFont("Microsoft YaHei UI", 10)
    app.setFont(font)

    # 清除旧的 response.json，避免启动时播放上次的回复
    try:
        from paths import RESPONSE_FILE
        if RESPONSE_FILE.exists():
            RESPONSE_FILE.unlink()
            logging.info("Cleared old response.json")
    except Exception:
        logger.debug("main: 非致命异常(已静默吞掉)", exc_info=True)

    manager = PetManager()

    # 如果 config 里没有 agents 列表（首次运行），自动添加
    if not manager.agents:
        from pathlib import Path

        # 1. 优先用月薪喵
        yuexinmiao = Path(__file__).parent / "characters" / "yuexinmiao"
        if yuexinmiao.exists():
            manager._config.setdefault("agents", []).append({
                "id": "yuexinmiao",
                "enabled": True,
                "position": {"x": -1, "y": -1},
                "scale": 1.0,
                "builtin": True,
            })
            manager._save_config()
        else:
            # 2. 扫描 Hanako agents
            discovered = manager.discover_agents()
            for agent in discovered:
                if agent["id"] == "yuexinmiao":
                    manager.add_agent("yuexinmiao")
                    break
            if not manager.agents:
                for agent in discovered:
                    if agent.get("has_sprites"):
                        manager.add_agent(agent["id"])
                        break

    manager.launch_all()

    # ── 业务就绪哨兵：通知 launcher 子进程“业务已就绪”。──
    # launcher 据此区分“启动期崩溃”（import/初始化期可能 30s+，此期崩溃不算
    # 健康运行）与“健康运行后偶发崩溃”，避免启动期崩溃被误判为健康而无限重启。
    try:
        from pathlib import Path
        import time as _time
        _logs_dir = Path(__file__).resolve().parent / "logs"
        _logs_dir.mkdir(exist_ok=True)
        _ready_flag = _logs_dir / f"ready_{os.getpid()}.flag"
        _ready_flag.write_text(_time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
        logging.getLogger(__name__).info("业务就绪哨兵已写入: %s", _ready_flag)
    except Exception as _e:
        logging.getLogger(__name__).warning("写入业务就绪哨兵失败（不影响运行）: %s", _e)

    # ── 主线程存活心跳（供 launcher 看门狗检测 GUI 卡死）──
    # Qt 计时器跑在主线程事件循环；主线程一旦卡死（如 Live2D WebView stall），
    # 计时器不再触发 → 心跳文件停止更新 → launcher 判定 hang 并强杀子进程触发自动复活。
    # 必须在 app.exec() 之前创建并 start；父对象设为 app 防止被 Python GC 误回收。
    try:
        from PySide6.QtCore import QTimer as _QTimer
        import time as _hbtime
        from pathlib import Path as _HbPath
        _hb_path = _HbPath(__file__).resolve().parent / "logs" / f"heartbeat_{os.getpid()}.txt"
        _hb_timer = _QTimer(app)
        _hb_timer.setInterval(5000)

        def _write_heartbeat():
            try:
                _hb_path.write_text(_hbtime.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
            except Exception:
                pass

        _hb_timer.timeout.connect(_write_heartbeat)
        _hb_timer.start()
        _write_heartbeat()
        logging.getLogger(__name__).info("主线程心跳已启动: %s", _hb_path)
    except Exception as _hb_e:
        logging.getLogger(__name__).warning("主线程心跳启动失败（不影响运行）: %s", _hb_e)

    rc = app.exec()
    # 退出前 flush 防抖写盘，避免丢失最后一次位置保存
    try:
        from config import async_config_saver
        async_config_saver.shutdown()
    except Exception:
        logger.debug("main: 非致命异常(已静默吞掉)", exc_info=True)
    sys.exit(rc)


if __name__ == "__main__":
    main()
