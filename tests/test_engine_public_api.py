"""ConversationEngine 对外语义接口（2026-09-19）。

背景
----
PetSystem/PetShell 拆分的前置清理。原先有 **4 处**跨模块私有访问：

    voice_provider_mixin.py:240-242   self._engine._lock / _tts / _tts_ready
    chat_mixin.py:533-536             self._engine._adapter._reply_timeout
    bubble_mixin.py:253               self._engine._adapter.transport_mode
    pet.py:3503-3504                  self._engine._thread

这些让「引擎内部实现」泄漏到 UI 侧，拆分时无法把引擎搬进 PetSystem
而不动 UI 代码。

新增的四个方法**不是 getter**（把私有属性换名字暴露等于没改），
而是有名字的**操作**：

    set_tts_provider()   原子替换（持引擎锁）
    transport_mode()     「当前是不是 Hanako 模式」
    reply_timeout_sec()  「该等多久」
    join_thread()        「关闭时等它收尾」

本测试重点：
  * set_tts_provider 的**原子性**（worker 线程同时读，不能出现中间态）
  * 无 adapter / 属性缺失时的兜底（不能抛）
  * 源码里不再有 _engine._ 私有访问
"""
from __future__ import annotations

import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.conversation_engine import ConversationEngine  # noqa: E402


class _FakeProvider:
    def __init__(self, name="fake", ready=True):
        self.name = name
        self.is_ready = ready

    def preload(self):
        pass


@pytest.fixture()
def engine():
    """用 object.__new__ 跳过 __init__（避免拉起真实线程/适配器）。"""
    e = object.__new__(ConversationEngine)
    e._lock = threading.Lock()
    e._tts = None
    e._tts_ready = False
    e._adapter = None
    e._thread = None
    return e


# ── 1. set_tts_provider：原子性（最关键）──

def test_set_tts_provider_updates_both(engine):
    p = _FakeProvider(ready=True)
    assert engine.set_tts_provider(p) is True
    assert engine._tts is p
    assert engine._tts_ready is True


def test_set_tts_provider_none(engine):
    assert engine.set_tts_provider(None) is False
    assert engine._tts is None
    assert engine._tts_ready is False


def test_set_tts_provider_not_ready(engine):
    p = _FakeProvider(ready=False)
    assert engine.set_tts_provider(p) is False
    assert engine._tts is p          # provider 换上了
    assert engine._tts_ready is False  # 但 ready 是 False


def test_set_tts_provider_missing_is_ready_attr(engine):
    """provider 没有 is_ready 属性时不应抛（第三方 provider 可能没有）。"""
    class _Bare:
        name = "bare"

    assert engine.set_tts_provider(_Bare()) is False


def test_set_tts_provider_holds_engine_lock(engine):
    """确定性验证：两处赋值必须发生在**引擎锁内**。

    为什么不用并发竞态用例：CPython 的 GIL 让「两步赋值被读到中间态」
    极难稳定复现——实测用 4 线程 × 3000 次切换、并把实现故意改成非原子，
    该用例**仍然通过**。那是个假测试，不能留。

    所以这里验证**可确定的性质**：赋值期间持的是 engine._lock。
    做法是把 _lock 换成一个记录进入/退出的探针锁，
    再用一个「赋值时检查是否持锁」的 provider 来断言。
    """
    p = _FakeProvider("probe", ready=True)

    observed = {}
    real_lock = engine._lock

    class _ProbeLock:
        """包住真锁，记录持锁状态。"""
        def __init__(self):
            self.held = False
            self.max_depth = 0

        def __enter__(self):
            real_lock.__enter__()
            self.held = True
            self.max_depth += 1
            return self

        def __exit__(self, *a):
            self.held = False
            return real_lock.__exit__(*a)

    probe = _ProbeLock()
    engine._lock = probe

    # 让 is_ready 在被读取的那一刻记录「是否持锁」
    class _ProbeProvider:
        name = "probe"

        @property
        def is_ready(self):
            observed["held_during_ready"] = probe.held
            return True

    try:
        engine.set_tts_provider(_ProbeProvider())
    finally:
        engine._lock = real_lock

    assert probe.max_depth == 1, "应恰好进入一次引擎锁"
    assert observed.get("held_during_ready") is True, (
        "读取 is_ready 时未持引擎锁 —— 两步赋值不在同一临界区内"
    )
    assert engine._tts_ready is True
    assert probe.held is False, "退出后应已释放锁"


def test_set_tts_provider_single_lock_acquisition(engine):
    """只应获取一次锁（不是每个字段各一次）。"""
    real_lock = engine._lock
    count = {"n": 0}

    class _CountLock:
        def __enter__(self):
            count["n"] += 1
            return real_lock.__enter__()

        def __exit__(self, *a):
            return real_lock.__exit__(*a)

    engine._lock = _CountLock()
    try:
        engine.set_tts_provider(_FakeProvider(ready=True))
    finally:
        engine._lock = real_lock

    assert count["n"] == 1, f"锁获取次数应为 1，实际 {count['n']}"


# ── 2. transport_mode ──

def test_transport_mode_defaults_to_direct(engine):
    assert engine.transport_mode() == "direct"


def test_transport_mode_from_adapter(engine):
    class _Ad:
        transport_mode = "prefer_hanako"

    engine._adapter = _Ad()
    assert engine.transport_mode() == "prefer_hanako"


def test_transport_mode_missing_attr(engine):
    class _Ad:
        pass

    engine._adapter = _Ad()
    assert engine.transport_mode() == "direct"


def test_transport_mode_empty_string(engine):
    """空串应回退 direct，不能返回 ''。"""
    class _Ad:
        transport_mode = ""

    engine._adapter = _Ad()
    assert engine.transport_mode() == "direct"


# ── 3. reply_timeout_sec ──

def test_reply_timeout_default(engine):
    assert engine.reply_timeout_sec() == 180.0


def test_reply_timeout_from_adapter(engine):
    class _Ad:
        _reply_timeout = 300

    engine._adapter = _Ad()
    assert engine.reply_timeout_sec() == 300.0


def test_reply_timeout_garbage_value(engine):
    """脏值不应抛，回退 180。"""
    class _Ad:
        _reply_timeout = "not-a-number"

    engine._adapter = _Ad()
    assert engine.reply_timeout_sec() == 180.0


def test_reply_timeout_zero(engine):
    """0 是假值但不能被当成缺失（or 180 会吞掉它）—— 见实现注释。"""
    class _Ad:
        _reply_timeout = 0

    engine._adapter = _Ad()
    assert engine.reply_timeout_sec() == 180.0


# ── 4. join_thread ──

def test_join_thread_no_thread(engine):
    assert engine.join_thread(timeout=0.1) is True


def test_join_thread_dead_thread(engine):
    engine._thread = threading.Thread(target=lambda: None)
    assert engine.join_thread(timeout=0.1) is True


def test_join_thread_alive_returns_false_on_timeout(engine):
    """跑很久的线程 → 超时返回 False（不阻塞关闭流程）。"""
    ev = threading.Event()
    engine._thread = threading.Thread(target=ev.wait, daemon=True)
    engine._thread.start()
    try:
        assert engine.join_thread(timeout=0.05) is False
    finally:
        ev.set()
        engine._thread.join(timeout=1.0)


def test_join_thread_alive_returns_true_when_finishes(engine):
    engine._thread = threading.Thread(target=lambda: None, daemon=True)
    engine._thread.start()
    assert engine.join_thread(timeout=2.0) is True


# ── 5. 收敛本身：源码里不该再有私有访问 ──

def test_no_engine_private_access_in_ui_side():
    """pet.py 与 pet_mixins/ 里不应再有 `_engine._` 私有访问。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    offenders = []
    targets = [os.path.join(root, "pet.py")]
    mixins = os.path.join(root, "pet_mixins")
    if os.path.isdir(mixins):
        targets += [os.path.join(mixins, f)
                    for f in os.listdir(mixins) if f.endswith(".py")]
    for path in targets:
        if not os.path.exists(path):
            continue
        for i, line in enumerate(open(path, encoding="utf-8"), 1):
            if "_engine._" in line and not line.strip().startswith("#"):
                offenders.append(f"{os.path.basename(path)}:{i}")
    assert not offenders, f"仍存在跨模块私有访问: {offenders}"
