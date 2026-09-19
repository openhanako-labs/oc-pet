# -*- coding: utf-8 -*-
"""A2A 对话接线测试。

**重点不是"函数返回了什么"，而是"这句话真的会触发吗"**——
所以关键用例全部走一遍真实的 ``CapabilityRouter.route()``，
而不是直接调 callable（直接调会掩盖"装上了但不触发"这种错）。
"""
from __future__ import annotations

import time

import pytest

from core.a2a import Delegator
from core.a2a_capability import (
    AGENT_ALIASES,
    build_capabilities,
    build_patterns,
    extract_task,
    label_of,
    register_a2a,
    resolve_agent,
    unregister_a2a,
)
from core.capability_registry import (
    Capability,
    CapabilityRouter,
    register_capability,
    unregister_capability,
)


class _Session:
    def __init__(self, agent: str):
        self.session_id = f"sess-{agent}"
        self.session_path = f"/sessions/{agent}.jsonl"


def _delegator(allowed=("kurisu",), reply="这是红莉栖的结论。"):
    def create(agent_id):
        return _Session(agent_id)

    def send(session, text, timeout):
        return reply

    return Delegator(create, send, {"enabled": True, "allowed_agents": list(allowed)})


@pytest.fixture
def registered():
    """注册后一定卸载——EXTERNAL_CAPABILITIES 是模块级全局，漏了会污染别的测试。"""
    made = []

    def _go(delegator):
        made.extend(register_a2a(delegator))
        return delegator

    yield _go
    unregister_a2a()


def _wait(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


# ── 认名字 ────────────────────────────────────────────────


def test_label_of_known_and_unknown():
    assert label_of("kurisu") == "牧濑红莉栖"
    assert label_of("nobody") == "nobody"
    assert label_of("") == "那位"


def test_resolve_agent_finds_chinese_alias():
    assert resolve_agent("把这件事交给红莉栖") == ("kurisu", "红莉栖")


def test_resolve_agent_prefers_longest_alias():
    """"牧濑红莉栖"要压过"红莉栖"，否则显示名/裁剪都用短的。"""
    got = resolve_agent("拜托牧濑红莉栖看一下")
    assert got == ("kurisu", "牧濑红莉栖")


def test_resolve_agent_returns_none_without_name():
    """绝不猜：没有名字就是没有。"""
    assert resolve_agent("把这件事交给某个人") is None
    assert resolve_agent("") is None


def test_resolve_agent_respects_pool():
    assert resolve_agent("交给艾莉丝", ["kurisu"]) is None
    assert resolve_agent("交给艾莉丝", ["alice"]) == ("alice", "艾莉丝")


def test_resolve_agent_is_case_insensitive_for_latin():
    assert resolve_agent("delegate to KURISU") == ("kurisu", "kurisu")


# ── 取任务 ────────────────────────────────────────────────


def test_extract_task_strips_name_and_punctuation():
    assert extract_task("交给红莉栖：帮我查一下泰拉瑞亚的改动", "红莉栖") == "帮我查一下泰拉瑞亚的改动"


def test_extract_task_keeps_full_text_when_remainder_too_short():
    """余下太短就不剥——宁可多给一句上下文，也不要把任务切坏。"""
    src = "交给红莉栖总结一下"
    assert extract_task(src, "红莉栖") == src


def test_extract_task_without_alias_is_identity():
    assert extract_task("随便什么", "不存在") == "随便什么"


# ── 关键词 ────────────────────────────────────────────────


def test_patterns_all_contain_agent_name():
    """关键词**必须自带助手名**——否则"把这句话交给我"会被截胡。"""
    for agent in AGENT_ALIASES:
        for p in build_patterns(agent):
            assert any(a.lower() in p.lower() for a in AGENT_ALIASES[agent]), p


# ── 真的会触发吗（走真实路由） ────────────────────────────


def test_router_fires_on_natural_phrase(registered):
    """核心用例：中间夹着中文字的"交给红莉栖"能命中（默认碰撞保护会拦下它）。"""
    d = registered(_delegator())
    r = CapabilityRouter().route("把这件事交给红莉栖总结一下")
    assert r is not None, "没命中——allow_embedded 没生效？"
    assert r.capability == "delegate_task"
    assert "牧濑红莉栖" in r.text
    assert _wait(lambda: len(d.results) == 1), "后台线程没派出去"


def test_router_does_not_intercept_without_agent_name(registered):
    """没有助手名的"交给"必须放过给 LLM——否则会吞掉正常对话。"""
    registered(_delegator())
    assert CapabilityRouter().route("把这句话交给我") is None
    assert CapabilityRouter().route("这件事交给你了") is None


def test_router_fires_when_text_starts_with_verb(registered):
    registered(_delegator())
    r = CapabilityRouter().route("交给红莉栖：查一下泰拉瑞亚 1.4.5")
    assert r is not None and r.capability == "delegate_task"
    assert _wait(lambda: True)


def test_guard_still_applies_to_normal_capabilities():
    """加性字段不能改变原有行为：没声明 allow_embedded 的能力照旧被拦。"""
    cap = Capability(name="_tmp_guard", patterns=["交给"],
                     handler="callable", callable=lambda t: None)
    register_capability(cap)
    try:
        assert CapabilityRouter().route("把这件事交给某人") is None
    finally:
        unregister_capability("_tmp_guard")


# ── 回复内容 ──────────────────────────────────────────────


def test_delegate_reply_is_immediate(registered):
    """派活要立刻回话（不能等三分钟）——回复里不能有结论。"""
    registered(_delegator())
    r = CapabilityRouter().route("交给红莉栖看看这个")
    assert "已经交给" in r.text and "有结果我叫你" in r.text
    assert "结论" not in r.text


def test_rejected_agent_explains_why(registered):
    """白名单为空 → 关键词仍生成（按全部已知助手），并给出明确原因。"""
    d = _delegator(allowed=())
    registered(d)
    r = CapabilityRouter().route("交给艾莉丝看一下")
    assert r is not None, "白名单为空时也该能匹配到，好告诉他为什么不行"
    assert "发不过去" in r.text and "白名单" in r.text
    assert d.results == [], "被拒的派活不该真的发出去"


def test_disabled_delegator_reports_not_enabled(registered):
    d = Delegator(lambda a: _Session(a), lambda s, t, to: "x",
                  {"enabled": False, "allowed_agents": ["kurisu"]})
    # enabled=False 时我们不注册；这里直接构建能力以验证拒因文案
    caps = build_capabilities(d)
    task_cap = [c for c in caps if c.name == "delegate_task"][0]
    out = task_cap.callable("交给红莉栖看看")
    assert "未启用" in out.text


# ── 门铃的另一半：问结果 ──────────────────────────────────


def test_result_capability_reads_unread_once(registered):
    d = registered(_delegator())
    CapabilityRouter().route("交给红莉栖看一眼")
    assert _wait(lambda: d.unread_count == 1)

    r = CapabilityRouter().route("红莉栖那边有结果了吗")
    assert r is not None and r.capability == "delegation_result"
    assert "这是红莉栖的结论。" in r.text

    # 已读——再问就没有了
    r2 = CapabilityRouter().route("红莉栖那边有结果了吗")
    assert "还没有新结果" in r2.text


def test_result_capability_reports_failure_honestly(registered):
    """会话建起来了但没等到回话 → 说"还没回话"，**不能说"没交出去"**。"""
    def boom(session, text, timeout):
        raise RuntimeError("Hana 掉线")

    d = Delegator(lambda a: _Session(a), boom,
                  {"enabled": True, "allowed_agents": ["kurisu"]})
    registered(d)
    CapabilityRouter().route("交给红莉栖查一下")
    assert _wait(lambda: d.unread_count == 1)

    r = CapabilityRouter().route("红莉栖那边有结果了吗")
    assert "还没回话" in r.text and "Hana 掉线" in r.text
    assert "没交出去" not in r.text


def test_result_capability_says_not_delivered_when_create_failed(registered):
    """连会话都没建起来，才说"没交出去"。"""
    d = Delegator(lambda a: (_ for _ in ()).throw(RuntimeError("Hana 掉线")),
                  lambda s, t, to: "x",
                  {"enabled": True, "allowed_agents": ["kurisu"]})
    registered(d)
    CapabilityRouter().route("交给红莉栖查一下")
    assert _wait(lambda: d.unread_count == 1)

    r = CapabilityRouter().route("红莉栖那边有结果了吗")
    assert "没交出去" in r.text
    assert "还没回话" not in r.text


def test_bell_does_not_declare_failure_when_delivered():
    """源码护栏：宠物端门铃必须区分"没交出去"与"还没回话"。"""
    from pathlib import Path
    src = Path(__file__).resolve().parents[1].joinpath("pet.py").read_text(encoding="utf-8")
    start = src.index("def _on_a2a_result")
    body = src[start:start + 1400]
    assert 'getattr(result, "delivered", False)' in body
    assert "没交出去" in body and "还没回话" in body


def test_result_capability_when_nothing_pending(registered):
    registered(_delegator())
    r = CapabilityRouter().route("派活的结果呢")
    assert r is not None and "还没有新结果" in r.text


# ── 卸载 ──────────────────────────────────────────────────


def test_unregister_removes_both(registered):
    registered(_delegator())
    assert CapabilityRouter().route("交给红莉栖看看") is not None
    unregister_a2a()
    assert CapabilityRouter().route("交给红莉栖看看") is None
    assert CapabilityRouter().route("那边有结果了吗") is None
