# -*- coding: utf-8 -*-
"""config.json 监视器（"改配置不用重启"）单元测试。

不碰真文件、不用 Qt——loader / clock 全注入。
"""
from __future__ import annotations

import json

from core.config_watch import ConfigWatcher, config_signature, load_json_file


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


class _FakeFile:
    """可注入的"文件"：可改内容，也可模拟读不到。"""

    def __init__(self, data=None):
        self.data = data

    def __call__(self, path):
        return None if self.data is None else json.loads(json.dumps(self.data))


def _mk(data, **kw):
    f = _FakeFile(data)
    seen = []
    clock = kw.pop("clock", None) or _Clock()
    w = ConfigWatcher("x.json", on_change=seen.append, loader=f, clock=clock, **kw)
    return w, f, seen, clock


# ── 指纹 ──────────────────────────────────────────────────


def test_signature_is_order_insensitive():
    assert config_signature({"a": 1, "b": 2}) == config_signature({"b": 2, "a": 1})


def test_signature_handles_none_and_weird():
    assert config_signature(None) == config_signature({})
    assert isinstance(config_signature({"x": object()}), str)


def test_load_json_file_returns_none_on_bad_input(tmp_path):
    assert load_json_file(tmp_path / "nope.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("{ not json", encoding="utf-8")
    assert load_json_file(bad) is None
    arr = tmp_path / "arr.json"
    arr.write_text("[1,2]", encoding="utf-8")
    assert load_json_file(arr) is None, "不是 dict 就当没读到"
    ok = tmp_path / "ok.json"
    ok.write_text('{"a": 1}', encoding="utf-8")
    assert load_json_file(ok) == {"a": 1}


# ── 基线 ──────────────────────────────────────────────────


def test_prime_records_baseline_and_does_not_fire():
    """启动瞬间不能误触发——否则一开就白重装一遍。"""
    w, _f, seen, _c = _mk({"a": 1})
    w.prime()
    assert w.poll() is False
    assert seen == []


def test_first_read_without_prime_sets_baseline():
    w, _f, seen, _c = _mk({"a": 1})
    assert w.poll() is False, "首次读到内容 = 记基线，不触发"
    assert seen == []
    assert w.poll() is False


# ── 内容变化 ──────────────────────────────────────────────


def test_content_change_fires():
    w, f, seen, clock = _mk({"a": 1})
    w.prime()
    f.data = {"a": 2}
    clock.advance(10)
    assert w.poll() is True
    assert seen == [{"a": 2}]
    assert w.changes == 1


def test_same_content_does_not_fire():
    """内容没变就不重装（重建闸门会清配额、重建派活会丢结论）。"""
    w, _f, seen, clock = _mk({"a": 1})
    w.prime()
    clock.advance(10)
    assert w.poll() is False
    assert seen == []


def test_nested_change_is_detected():
    w, f, seen, clock = _mk({"game": {"disabled": ["x"]}})
    w.prime()
    f.data = {"game": {"disabled": ["x", "y"]}}
    clock.advance(10)
    assert w.poll() is True


def test_interval_is_respected():
    w, f, seen, clock = _mk({"a": 1}, interval_s=3.0)
    w.prime()
    f.data = {"a": 2}
    clock.advance(1.0)
    assert w.poll() is False, "没到间隔不查"
    assert w.poll() is False
    clock.advance(3.0)
    assert w.poll() is True


# ── 坏输入 ────────────────────────────────────────────────


def test_unreadable_file_is_not_treated_as_empty():
    """读不到 ≠ 配置空了。否则会把功能全卸掉。"""
    w, f, seen, clock = _mk({"a": 1})
    w.prime()
    f.data = None
    clock.advance(10)
    assert w.poll() is False
    assert seen == []


def test_recovers_after_transient_read_failure():
    w, f, seen, clock = _mk({"a": 1})
    w.prime()
    f.data = None
    clock.advance(10)
    w.poll()                                  # 读失败，什么都不做
    f.data = {"a": 3}                          # 文件恢复且内容变了
    clock.advance(10)
    assert w.poll() is True
    assert seen == [{"a": 3}]


def test_callback_exception_does_not_escape():
    """回调炸了也不能把主循环带走；基线已更新，不重复轰炸。"""
    f = _FakeFile({"a": 1})
    clock = _Clock()
    calls = []

    def boom(cfg):
        calls.append(cfg)
        raise RuntimeError("炸")

    w = ConfigWatcher("x.json", on_change=boom, loader=f, clock=clock)
    w.prime()
    f.data = {"a": 2}
    clock.advance(10)
    assert w.poll() is False, "异常被吞，返回 False"
    assert len(calls) == 1


def test_flapping_content_refires_each_time():
    w, f, seen, clock = _mk({"a": 1})
    w.prime()
    for i in range(3):
        f.data = {"a": i + 10}
        clock.advance(10)
        assert w.poll() is True
    assert len(seen) == 3 and w.changes == 3


def test_interval_floor():
    w, _f, _s, _c = _mk({"a": 1}, interval_s=0.0)
    assert w.interval_s >= 0.2, "不能配成 0 把主线程当空转烧"
