"""接线测试：日志流加固（防止「写日志」冻死整个应用）

覆盖 2026-09-10 卡死事故的修复：
  HanaAgent → cmd /c start_pet.bat → launcher.py → main.py
  cmd 的 stderr 是无人读取的管道 → 写满后 StreamHandler.emit 的 stream.write 阻塞
  → logging 全局锁被持有 → 所有线程排队 → 应用冻死（连看门狗也卡在 log.critical）

设计：全部通过 `stderr=` 显式传流、并用独立 logger 隔离，
不替换 sys.stderr、不触碰 root handler —— 避免干扰 pytest 自身的输出捕获。
"""
from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

import pytest

from log_setup import drop_blocking_stderr_handler, stream_may_block_forever


# ══════════════════════════════════════════════════════════════
#  流类型判定
# ══════════════════════════════════════════════════════════════

def test_pipe_is_detected_as_blockable():
    """真实管道：没人读就会填满并阻塞。这正是事故现场。"""
    r, w = os.pipe()
    try:
        with os.fdopen(w, "w") as pipe_out:
            assert stream_may_block_forever(pipe_out) is True
    finally:
        os.close(r)


def test_regular_file_is_not_blockable(tmp_path):
    """普通文件不会因「没人读」而填满。"""
    with open(tmp_path / "x.log", "w", encoding="utf-8") as fh:
        assert stream_may_block_forever(fh) is False


def test_no_fileno_is_conservatively_blockable():
    """拿不到 fileno 的内存流无法判断 → 保守当作会阻塞（摘掉比冻死强）。"""
    import io

    assert stream_may_block_forever(io.StringIO()) is True


def test_none_stream_is_blockable():
    assert stream_may_block_forever(None) is True


# ══════════════════════════════════════════════════════════════
#  handler 摘除（独立 logger，不碰 root）
# ══════════════════════════════════════════════════════════════

@pytest.fixture
def test_logger():
    lg = logging.getLogger("ocpet.test.log_setup")
    lg.handlers.clear()
    lg.propagate = False
    yield lg
    lg.handlers.clear()


@pytest.fixture
def pipe_pair():
    """返回 (读端 fd, 写端)."""
    r, w = os.pipe()
    yield r, os.fdopen(w, "w")
    os.close(r)


def test_no_durable_handler_means_no_removal(test_logger, pipe_pair):
    """只有 stderr handler、无文件 handler 时不得摘——否则日志彻底丢失。"""
    _, pipe = pipe_pair
    test_logger.addHandler(logging.StreamHandler(pipe))

    assert drop_blocking_stderr_handler(test_logger, stderr=pipe) == 0
    assert len(test_logger.handlers) == 1


def test_pipe_stderr_handler_is_removed(test_logger, pipe_pair, tmp_path):
    from logging.handlers import RotatingFileHandler

    _, pipe = pipe_pair
    fh = RotatingFileHandler(tmp_path / "a.log", encoding="utf-8")
    test_logger.addHandler(logging.StreamHandler(pipe))
    test_logger.addHandler(fh)

    assert drop_blocking_stderr_handler(test_logger, stderr=pipe) == 1

    assert any(isinstance(h, RotatingFileHandler) for h in test_logger.handlers)
    assert not any(
        type(h) is logging.StreamHandler and getattr(h, "stream", None) is pipe
        for h in test_logger.handlers
    )


def test_file_stderr_handler_is_kept(test_logger, tmp_path):
    """stderr 指向普通文件时不该摘（重定向是用户的正当用法）。"""
    from logging.handlers import RotatingFileHandler

    with open(tmp_path / "err.log", "w", encoding="utf-8") as err_fh:
        test_logger.addHandler(logging.StreamHandler(err_fh))
        test_logger.addHandler(RotatingFileHandler(tmp_path / "a.log", encoding="utf-8"))

        assert drop_blocking_stderr_handler(test_logger, stderr=err_fh) == 0
        assert len(test_logger.handlers) == 2


def test_removal_is_reported_to_the_surviving_handler(test_logger, pipe_pair, tmp_path):
    """摘掉控制台输出必须留痕——否则又是一次无声降级。"""
    from logging.handlers import RotatingFileHandler

    _, pipe = pipe_pair
    log_path = tmp_path / "a.log"
    fh = RotatingFileHandler(log_path, encoding="utf-8")
    test_logger.addHandler(logging.StreamHandler(pipe))
    test_logger.addHandler(fh)

    drop_blocking_stderr_handler(test_logger, stderr=pipe)
    test_logger.warning("should land in file")
    fh.flush()

    content = log_path.read_text(encoding="utf-8")
    assert "已摘除" in content, "摘除动作必须留下可见记录"
    assert "should land in file" in content, "后续日志应正常落文件"


def test_nothing_written_to_pipe_after_removal(test_logger, pipe_pair, tmp_path):
    """核心断言：摘掉之后，写日志不再碰那个会阻塞的管道。"""
    from logging.handlers import RotatingFileHandler

    r, pipe = pipe_pair
    fh = RotatingFileHandler(tmp_path / "a.log", encoding="utf-8")
    test_logger.addHandler(logging.StreamHandler(pipe))
    test_logger.addHandler(fh)

    drop_blocking_stderr_handler(test_logger, stderr=pipe)

    for i in range(50):
        test_logger.warning("msg %d", i)
    fh.flush()

    # 管道里必须一条都没有
    os.set_blocking(r, False)
    try:
        leaked = os.read(r, 65536)
    except (BlockingIOError, OSError):
        leaked = b""
    assert leaked == b"", f"管道仍被写入：{leaked[:120]!r}"


# ══════════════════════════════════════════════════════════════
#  端到端：管道写满也不冻（事故的可执行复现）
# ══════════════════════════════════════════════════════════════

def test_app_keeps_running_when_pipe_is_full(test_logger, tmp_path):
    """复现事故：管道写满。

    不加固时，写日志的线程永久阻塞；加固后，写日志完全不碰管道，应用照常跑。
    """
    from logging.handlers import RotatingFileHandler

    r, w = os.pipe()
    pipe = os.fdopen(w, "w")
    try:
        fh = RotatingFileHandler(tmp_path / "a.log", encoding="utf-8")
        test_logger.addHandler(logging.StreamHandler(pipe))
        test_logger.addHandler(fh)

        # 先把管道灌满，制造「无人读 → 写就阻塞」的现场
        os.set_blocking(w, False)
        filler = b"x" * 8192
        while True:
            try:
                os.write(w, filler)
            except (BlockingIOError, OSError):
                break
        os.set_blocking(w, True)

        drop_blocking_stderr_handler(test_logger, stderr=pipe)

        # 另一个线程持续写日志；加固前这里会永久挂住
        done = threading.Event()

        def _writer():
            for i in range(300):
                test_logger.warning("hazard %d", i)
            fh.flush()
            done.set()

        t = threading.Thread(target=_writer, daemon=True)
        t.start()
        t.join(timeout=10.0)

        assert done.is_set(), "管道写满时应用仍应能正常写日志（不阻塞）"
    finally:
        os.close(r)
        pipe.close()


# ══════════════════════════════════════════════════════════════
#  看门狗：先杀后记（源码级不变量）
# ══════════════════════════════════════════════════════════════

def test_watchdog_kills_before_logging():
    """看门狗的职责是恢复，不能被日志挡住。

    事故里 log.critical 先于 child.kill()，写日志阻塞 → 永远杀不掉子进程
    → 「自动复活」失效，进程卡死 9 分钟无人管。这条锁住顺序。
    """
    src = (Path(__file__).resolve().parent.parent / "launcher.py").read_text(encoding="utf-8")
    start = src.index("def _watchdog_wait(")
    end = src.index("\ndef ", start + 10)
    body = src[start:end]

    kill_at = body.index("child.kill()")
    crit_at = body.index("log.critical(")

    assert kill_at < crit_at, "必须先 kill 再写日志——否则日志阻塞会让看门狗失效"


def test_launcher_has_file_log_handler():
    """launcher 不得只依赖 stderr：事故中它自己就卡在写 stderr 上。"""
    src = (Path(__file__).resolve().parent.parent / "launcher.py").read_text(encoding="utf-8")
    assert "RotatingFileHandler" in src, "launcher 应有文件日志，不依赖 stderr"
    assert "launcher.log" in src


def test_main_hardens_logging_after_file_setup():
    """main.py 必须在文件日志就位后才摘 stderr handler（顺序不能反）。"""
    src = (Path(__file__).resolve().parent.parent / "main.py").read_text(encoding="utf-8")
    assert "drop_blocking_stderr_handler" in src
    idx_setup = src.index("_setup_file_logging()")
    idx_harden = src.index("drop_blocking_stderr_handler")
    assert idx_setup < idx_harden, "必须先挂文件 handler，再摘 stderr，否则日志会丢"
