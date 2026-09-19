# -*- coding: utf-8 -*-
"""A2A 派活（策略 + 编排）单元测试。

通道注入，所以不碰 Hana、不花 token、不建真会话。
"""
from __future__ import annotations

from core.a2a import (
    DEFAULT_MAX_PER_DAY,
    DEFAULT_MAX_PER_HOUR,
    MAX_TASK_CHARS,
    Delegator,
    build_from_config,
)


class _Clock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class _Session:
    def __init__(self, agent: str, n: int = 1):
        self.session_id = f"s{n}-{agent}"
        self.session_path = f"/sessions/s{n}-{agent}.jsonl"


def _mk(**cfg):
    """返回 (delegator, 记录用的 list)。"""
    calls = []
    clock = cfg.pop("_clock", None) or _Clock()
    agent_cfg = {"enabled": True, "allowed_agents": ["kurisu"]}
    agent_cfg.update(cfg)

    def create_session(agent_id):
        calls.append(("create", agent_id))
        return _Session(agent_id, len(calls))

    def send(session, text, timeout):
        calls.append(("send", session.session_id, text, timeout))
        return f"[{session.session_id}] 收到：{text}"

    d = Delegator(create_session, send, agent_cfg, now=clock)
    return d, calls, clock


# ── 默认：关着 ────────────────────────────────────────────


def test_default_is_disabled():
    d = Delegator(lambda a: _Session(a), lambda s, t, to: "x", {})
    assert d.enabled is False
    ok, why = d.check("kurisu")
    assert ok is False and "未启用" in why


def test_default_allowed_agents_is_empty():
    """默认一个都不许——绝不自动挑 agent。"""
    d = Delegator(lambda a: _Session(a), lambda s, t, to: "x",
                  {"enabled": True})
    assert d.allowed_agents == []
    ok, why = d.check("kurisu")
    assert ok is False and "白名单" in why


def test_empty_agent_is_rejected():
    d, _, _ = _mk()
    ok, why = d.check("")
    assert ok is False and "没指定 agent" in why


def test_agent_not_in_allowlist_is_rejected():
    d, _, _ = _mk()
    ok, why = d.check("alice")
    assert ok is False and "不在白名单" in why


# ── 正常派活 ──────────────────────────────────────────────


def test_delegate_creates_new_session_and_sends():
    d, calls, _ = _mk()
    r = d.delegate("帮我查一下泰拉瑞亚 1.4.5 的改动", "kurisu")
    assert r.ok is True
    assert r.agent_id == "kurisu"
    assert r.session_id and r.session_path
    assert "泰拉瑞亚" in r.reply
    kinds = [c[0] for c in calls]
    assert kinds == ["create", "send"], "必须先建会话再发话"


def test_delegate_never_reuses_user_sessions():
    """护栏 4：每次都新建，绝不往用户已有会话里插话。"""
    d, calls, _ = _mk()
    d.delegate("第一件", "kurisu")
    d.delegate("第二件", "kurisu")
    creates = [c for c in calls if c[0] == "create"]
    assert len(creates) == 2, "两次派活必须建两个会话"
    sends = [c for c in calls if c[0] == "send"]
    assert sends[0][1] != sends[1][1], "两次不能落在同一个会话"


def test_reply_is_returned_verbatim():
    d, _, _ = _mk()
    r = d.delegate("写个标题", "kurisu")
    assert r.reply.startswith("[s1-kurisu]")


def test_elapsed_is_measured():
    d, _, clock = _mk()

    def send(session, text, timeout):
        clock.advance(7.5)
        return "ok"

    d._send = send
    r = d.delegate("任务", "kurisu")
    assert r.ok and r.elapsed == 7.5


def test_timeout_is_passed_through():
    d, calls, _ = _mk()
    d.delegate("任务", "kurisu")
    send = [c for c in calls if c[0] == "send"][0]
    assert send[3] == 180.0


# ── 失败绝不伪装成功 ──────────────────────────────────────


def test_send_failure_returns_not_ok():
    d, _, _ = _mk()

    def boom(session, text, timeout):
        raise RuntimeError("Hana 掉线了")

    d._send = boom
    r = d.delegate("任务", "kurisu")
    assert r.ok is False
    assert "Hana 掉线了" in r.error
    assert r.reply == ""


def test_create_session_failure_is_caught():
    d, _, _ = _mk()

    def boom(agent):
        raise RuntimeError("建会话失败")

    d._create_session = boom
    r = d.delegate("任务", "kurisu")
    assert r.ok is False and "建会话失败" in r.error


def test_session_without_stable_id_is_rejected():
    """拿不到稳定标识就不能往下走——否则回复丢了都不知道丢在哪。"""
    d, _, _ = _mk()
    d._create_session = lambda agent: type("X", (), {})()
    r = d.delegate("任务", "kurisu")
    assert r.ok is False and "稳定标识" in r.error


def test_delegate_never_raises():
    """派活在对话主路径上，任何异常都不能冒出去。"""
    d, _, _ = _mk()
    d._send = lambda *a: (_ for _ in ()).throw(ValueError("炸"))
    r = d.delegate("任务", "kurisu")
    assert isinstance(r.ok, bool) and r.ok is False


# ── 输入校验 ──────────────────────────────────────────────


def test_empty_task_is_rejected():
    d, calls, _ = _mk()
    r = d.delegate("   ", "kurisu")
    assert r.ok is False and "任务为空" in r.error
    assert calls == [], "空任务不该产生任何调用"


def test_overlong_task_is_rejected():
    d, calls, _ = _mk()
    r = d.delegate("啊" * (MAX_TASK_CHARS + 1), "kurisu")
    assert r.ok is False and "过长" in r.error
    assert calls == []


def test_task_at_limit_is_accepted():
    d, _, _ = _mk()
    assert d.delegate("啊" * MAX_TASK_CHARS, "kurisu").ok is True


# ── 预算 ──────────────────────────────────────────────────


def test_hourly_budget_blocks():
    d, _, clock = _mk(max_per_hour=2, max_per_day=99)
    assert d.delegate("一", "kurisu").ok
    assert d.delegate("二", "kurisu").ok
    r = d.delegate("三", "kurisu")
    assert r.ok is False and "每小时上限" in r.error


def test_hourly_window_slides():
    d, _, clock = _mk(max_per_hour=1, max_per_day=99)
    d.delegate("一", "kurisu")
    assert d.delegate("二", "kurisu").ok is False
    clock.advance(3601)
    assert d.delegate("三", "kurisu").ok is True


def test_daily_budget_blocks_even_after_hour_slides():
    d, _, clock = _mk(max_per_hour=1, max_per_day=2)
    d.delegate("一", "kurisu")
    clock.advance(3601)
    d.delegate("二", "kurisu")
    clock.advance(3601)
    r = d.delegate("三", "kurisu")
    assert r.ok is False and "每日上限" in r.error


def test_failed_delegation_does_not_spend_budget():
    d, _, _ = _mk(max_per_hour=1, max_per_day=1)
    d._send = lambda s, t, to: (_ for _ in ()).throw(RuntimeError("x"))
    assert d.delegate("一", "kurisu").ok is False
    d._send = lambda s, t, to: "ok"
    assert d.delegate("二", "kurisu").ok is True, "失败的派活不该吃掉配额"


def test_defaults_are_conservative():
    assert DEFAULT_MAX_PER_HOUR <= 10 and DEFAULT_MAX_PER_DAY <= 50


def test_bad_budget_value_falls_back():
    d = Delegator(lambda a: _Session(a), lambda s, t, to: "x",
                  {"enabled": True, "max_per_hour": "abc"})
    assert d.stats()["max_per_hour"] == DEFAULT_MAX_PER_HOUR


# ── 结果队列 & 回调 ───────────────────────────────────────


def test_results_queue_keeps_unsaid_results():
    """结果不自动念——先落队列，由上层决定要不要说。"""
    d, _, _ = _mk()
    d.delegate("任务", "kurisu")
    assert len(d.results) == 1 and d.results[0].ok


def test_results_queue_is_bounded():
    d, _, _ = _mk(max_per_hour=999, max_per_day=999)
    d._send = lambda s, t, to: "x"
    for i in range(80):
        d.delegate(f"任务{i}", "kurisu")
    assert len(d.results) <= 50


def test_on_result_only_fires_on_success():
    seen = []
    calls = []

    def create(agent):
        calls.append(agent)
        return _Session(agent)

    d = Delegator(create, lambda s, t, to: "ok",
                  {"enabled": True, "allowed_agents": ["kurisu"]},
                  on_result=seen.append)
    d.delegate("成功", "kurisu")
    d._send = lambda s, t, to: (_ for _ in ()).throw(RuntimeError("炸"))
    d.delegate("失败", "kurisu")
    assert len(seen) == 1 and seen[0].ok


def test_stats_shape():
    d, _, _ = _mk()
    d.delegate("任务", "kurisu")
    s = d.stats()
    assert s["enabled"] is True
    assert s["allowed_agents"] == ["kurisu"]
    assert s["used_last_hour"] == 1 and s["used_last_day"] == 1


# ── 配置装配 ──────────────────────────────────────────────


def test_build_from_config_defaults_to_disabled():
    d = build_from_config({}, lambda a: _Session(a), lambda s, t, to: "x")
    assert d.enabled is False


def test_build_from_config_reads_block():
    d = build_from_config(
        {"a2a": {"enabled": True, "allowed_agents": ["alice", "kurisu"],
                 "max_per_hour": 3}},
        lambda a: _Session(a), lambda s, t, to: "x",
    )
    assert d.enabled is True
    assert d.allowed_agents == ["alice", "kurisu"]
    assert d.stats()["max_per_hour"] == 3


def test_build_from_config_accepts_single_agent_string():
    d = build_from_config({"a2a": {"enabled": True, "allowed_agents": "kurisu"}},
                          lambda a: _Session(a), lambda s, t, to: "x")
    assert d.allowed_agents == ["kurisu"]
