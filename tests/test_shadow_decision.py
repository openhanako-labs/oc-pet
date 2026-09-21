"""影子模式决策记录器测试。

设计原则（与 test_expression_director.py 保持一致）：
- 引擎不可用时全部走兜底路径 —— CI / 无 GPU 环境下本测试必须全绿。
- **零行为改变**：关掉开关时，任何调用都不产生 I/O、不建线程。

覆盖：
1. 记录器基础：enabled / disabled / set_config / jsonl 落盘 / 结果回写 / stats
2. 健壮性：引擎离线、引擎返回畸形、异常不抛出
3. ProactiveScheduler 接线：默认关 → 无调用；config 开 → 记录；关闭后 → 停止
4. 主线程非阻塞性：引擎慢时主线程仍毫秒级返回
"""
from __future__ import annotations

import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.shadow_decision import (  # noqa: E402
    ShadowDecisionRecorder,
    _build_noul_schema,
    _summarize_context,
)
from core.perception.proactive import ProactiveScheduler  # noqa: E402


# ── 引擎测试替身（默认快回离线，避免真实网络探测拖慢测试） ──


def _install_engine_stub(monkeypatch, response=None, available=True,
                        exc=None, delay=0.0):
    """默认给测试装上 FastFail 引擎 stub（避免真实网络探测拖慢测试）。

    补丁位置：`core.expression_director.LocalDecisionClient`
    （_handle_decision 惰性导入，运行时取的是 core.expression_director
      模块里的名字）。

    Args:
        response: decide() 返回值
        available: is_available() 返回值
        exc: 抛出异常（None = 不抛）
        delay: 模拟网络延迟（秒）
    """
    class FastFailClient:
        def __init__(self, base_url="", timeout=0.1, **kwargs):
            self.base_url = base_url
            self.timeout = timeout

        def is_available(self, ttl=30.0):
            if exc is not None:
                raise exc
            if delay:
                time.sleep(delay)
            return available

        def decide(self, context, schema, temperature=0.2):
            if exc is not None:
                raise exc
            if delay:
                time.sleep(delay)
            return response

    # 两个位置都补丁，防御性：_handle_decision 惰性 import 从
    # core.expression_director 取；保险起见 shadow_decision 模块也补
    monkeypatch.setattr(
        "core.expression_director.LocalDecisionClient", FastFailClient,
    )
    import core.shadow_decision as sd_mod
    monkeypatch.setattr(sd_mod, "LocalDecisionClient", FastFailClient,
                        raising=False)


def _wait_for_worker(recorder: "ShadowDecisionRecorder",
                    timeout: float = 5.0) -> bool:
    """等待 worker 线程处理队列；返回是否在超时内完成。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        # 队列空 + 短等一下确认已写完
        if recorder._queue is not None and recorder._queue.empty():
            time.sleep(0.05)
            if recorder._queue is None or recorder._queue.empty():
                return True
        time.sleep(0.02)
    return False


def _read_jsonl(path: str) -> list[dict]:
    """读取 JSONL 文件；文件不存在返回空列表。"""
    if not os.path.exists(path):
        return []
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


# ── 用例 1：基础行为（启用/关闭/开关切换） ──────────────────


class TestRecorderEnabledState:
    def test_disabled_by_default(self):
        r = ShadowDecisionRecorder()
        assert not r.enabled()

    def test_enabled_explicitly(self):
        r = ShadowDecisionRecorder({"enabled": True})
        assert r.enabled()

    def test_disabled_returns_none_and_writes_nothing(self, tmp_path):
        """关掉开关时：不建线程、不入队、不落盘（零 I/O）。"""
        p = str(tmp_path / "x.jsonl")
        r = ShadowDecisionRecorder({"enabled": False, "jsonl_path": p})
        did = r.record_decision(kind="k", context={"scenario": "s"})
        assert did is None
        assert r._queue is None  # 未建队列
        assert r._worker is None  # 未建线程
        assert not os.path.exists(p)
        # 结果也不落盘
        r.record_outcome("x", delivered=True)
        assert not os.path.exists(p)

    def test_set_enabled_toggles(self):
        r = ShadowDecisionRecorder({"enabled": False})
        assert not r.enabled()
        r.set_enabled(True)
        assert r.enabled()
        r.set_enabled(False)
        assert not r.enabled()

    def test_set_config_updates_enabled(self):
        r = ShadowDecisionRecorder({"enabled": False})
        r.set_config({"enabled": True})
        assert r.enabled()
        r.set_config({"enabled": False})
        assert not r.enabled()

    def test_stats_when_disabled(self):
        r = ShadowDecisionRecorder({"enabled": False})
        s = r.stats()
        assert s["enabled"] is False
        assert s["decisions_total"] == 0
        assert s["outcomes_total"] == 0


# ── 用例 2：JSONL 落盘格式 ────────────────────────────────


class TestJsonlFormat:
    def test_decision_record_shape(self, tmp_path, monkeypatch):
        """启用后：一次 record_decision 应产生一条 decision 记录，字段完整。"""
        _install_engine_stub(
            monkeypatch,
            response={"verdict": {"value": "no", "prob": 0.9}},
            available=True,
        )
        p = str(tmp_path / "shadow.jsonl")
        r = ShadowDecisionRecorder({"enabled": True, "jsonl_path": p})
        did = r.record_decision(
            kind="proactive.intent",
            context={
                "scenario": "night_late_work",
                "intent": {"intent": "work", "confidence": 0.85},
                "signals": {"period": "evening", "activity": "typing"},
            },
            fallback_prompt="写了很久，休息一下吧？",
        )
        assert did and len(did) == 12

        assert _wait_for_worker(r), "worker 未在处理队列"
        lines = _read_jsonl(p)
        assert len(lines) == 1
        rec = lines[0]
        for k in ("record_type", "decision_id", "kind", "verdict", "prob",
                  "band", "source", "elapsed_ms", "engine_available",
                  "input_summary", "fallback_prompt", "question"):
            assert k in rec, f"缺字段 {k}"
        assert rec["record_type"] == "decision"
        assert rec["decision_id"] == did
        assert rec["verdict"] == "no"
        assert rec["prob"] == 0.9
        assert rec["engine_available"] is True
        assert rec["source"] == "engine"
        assert "night_late_work" in json.dumps(
            rec["input_summary"], ensure_ascii=False
        )

    def test_outcome_record_shape(self, tmp_path):
        p = str(tmp_path / "shadow.jsonl")
        r = ShadowDecisionRecorder({"enabled": True, "jsonl_path": p})
        r.record_outcome(
            "did_abc", delivered=True, prompt="你好", source="intent",
        )
        assert _wait_for_worker(r)
        lines = _read_jsonl(p)
        assert len(lines) == 1
        rec = lines[0]
        assert rec["record_type"] == "outcome"
        assert rec["decision_id"] == "did_abc"
        assert rec["delivered"] is True
        assert rec["prompt"] == "你好"
        assert rec["source"] == "intent"

    def test_user_replied_outcome(self, tmp_path):
        p = str(tmp_path / "shadow.jsonl")
        r = ShadowDecisionRecorder({"enabled": True, "jsonl_path": p})
        r.record_user_reply("did_xyz", replied=True)
        assert _wait_for_worker(r)
        rec = _read_jsonl(p)[0]
        assert rec["user_replied"] is True
        assert rec["note"] == "user_reply_recorded"

    def test_jsonl_path_dir_gets_daily_suffix(self, tmp_path):
        """路径无 .jsonl 后缀 → 自动追加日期后缀，不覆盖已有文件。"""
        d = str(tmp_path / "shadow_decisions")
        r = ShadowDecisionRecorder({"enabled": True, "jsonl_path": d})
        assert r._jsonl_path.endswith(".jsonl")
        assert "shadow_" in r._jsonl_path


# ── 用例 3：异常与降级（绝不抛异常） ──────────────────────


class TestRobustness:
    def test_engine_offline_produces_record_with_available_false(
        self, tmp_path, monkeypatch,
    ):
        """引擎离线（is_available=False，快返回）→ 写 engine_off 记录。"""
        _install_engine_stub(monkeypatch, response=None, available=False)
        p = str(tmp_path / "shadow.jsonl")
        r = ShadowDecisionRecorder({"enabled": True, "jsonl_path": p})
        did = r.record_decision(kind="k", context={"scenario": "s"})
        assert did
        assert _wait_for_worker(r)
        rec = _read_jsonl(p)[0]
        assert rec["engine_available"] is False
        assert rec["verdict"] is None
        assert rec["source"] == "engine_off"

    def test_engine_returns_none(self, tmp_path, monkeypatch):
        """引擎在线但 decide 返回 None（无有效 verdict）→ 记录但 verdict=None。"""
        _install_engine_stub(monkeypatch, response=None, available=True)
        p = str(tmp_path / "shadow.jsonl")
        r = ShadowDecisionRecorder({"enabled": True, "jsonl_path": p})
        did = r.record_decision(kind="k", context={})
        assert did
        assert _wait_for_worker(r)
        rec = _read_jsonl(p)[0]
        assert rec["verdict"] is None
        assert "未返回" in rec["reason"] or "引擎" in rec["reason"]

    def test_engine_raises_exception(self, tmp_path, monkeypatch):
        """引擎抛异常：写一条 source=engine_err 的记录，不 crash。"""
        _install_engine_stub(monkeypatch, exc=RuntimeError("boom"),
                              available=True)
        p = str(tmp_path / "shadow.jsonl")
        r = ShadowDecisionRecorder({"enabled": True, "jsonl_path": p})
        did = r.record_decision(kind="k", context={})
        assert did  # 主线程立即返回 ID，不抛
        assert _wait_for_worker(r)
        rec = _read_jsonl(p)[0]
        assert rec["source"] == "engine_err"
        assert "boom" in rec["reason"]

    def test_record_decision_never_raises(self, tmp_path):
        """任何异常都不得抛出（硬纪律）。"""
        p = str(tmp_path / "shadow.jsonl")
        r = ShadowDecisionRecorder({"enabled": True, "jsonl_path": p})
        # 传入非法类型，不应崩溃
        try:
            r.record_decision(kind=None, context=None, fallback_prompt=None)  # type: ignore
        except Exception as e:
            pytest.fail(f"record_decision 抛异常: {e!r}")

    def test_record_outcome_never_raises(self, tmp_path):
        # 2026-09-21：必须传显式 jsonl_path。不传时落默认相对目录
        # ./shadow_decisions/，会把占位记录写进**真实**数据文件
        # （实测：跑一次测试该文件 6 行→ 7 行）。conftest 里已加隔离守卫，
        # 这里仍显式传路径，两道一起才有意义。
        p = str(tmp_path / "shadow.jsonl")
        r = ShadowDecisionRecorder({"enabled": True, "jsonl_path": p})
        # 各种边界输入，都不应抛
        r.record_outcome("", delivered=True)
        r.record_outcome(None, delivered=True)  # type: ignore
        r.record_outcome("did", delivered=None, user_replied=None)  # type: ignore

    def test_default_path_never_points_into_repo(self, _isolate_shadow_decisions):
        """守卫：测试期间的默认落盘路径必须在 tmp，不得是仓库目录。

        这是给 2026-09-21 那起「测试往真实 shadow_decisions/ 写脏记录」
        立的具体栏杆——比人盯代码可靠。
        """
        r = ShadowDecisionRecorder()
        assert str(_isolate_shadow_decisions) in r._jsonl_path

    def test_stats_after_engine_failure(self, tmp_path, monkeypatch):
        _install_engine_stub(monkeypatch, exc=RuntimeError("x"),
                              available=True)
        p = str(tmp_path / "shadow.jsonl")
        r = ShadowDecisionRecorder({"enabled": True, "jsonl_path": p})
        r.record_decision(kind="k", context={})
        _wait_for_worker(r)
        s = r.stats()
        assert s["decisions_total"] == 1
        assert s["verdict_hist"] == {}
        assert s["source_hist"].get("engine_err", 0) >= 1


# ── 用例 4：schema 与内部工具 ─────────────────────────────


class TestInternalHelpers:
    def test_build_noul_schema_shape(self):
        schema = _build_noul_schema("是否值得花 LLM 生成？")
        assert "verdict" in schema
        v = schema["verdict"]
        assert v["type"] == "enum"
        assert v["choices"] == ["yes", "no"]
        # 硬约束：选项写在 description 第一行
        first_line = v["description"].split("\n")[0]
        assert "yes" in first_line and "no" in first_line

    def test_build_noul_schema_custom_question(self):
        schema = _build_noul_schema("自定义问题 X")
        assert "自定义问题 X" in schema["verdict"]["description"]

    def test_summarize_context_keeps_key_fields(self):
        ctx = {
            "scenario": "night_late_work",
            "category": "development",
            "signals": {"period": "evening", "activity": "typing"},
            "fallback_prompt": "休息一下",
        }
        s = _summarize_context(ctx)
        assert s["scenario"] == "night_late_work"
        assert "signals" in s
        assert s["signals"].get("period") == "evening"

    def test_summarize_context_truncates_long_text(self):
        ctx = {"fallback_prompt": "x" * 500}
        s = _summarize_context(ctx)
        assert len(s.get("fallback_prompt", "")) <= 120


# ── 用例 5：ProactiveScheduler 集成 ──────────────────────


class TestProactiveIntegration:
    """验证接入的旁路特性：默认关 → 无调用；启用 → 记录；关闭 → 停止。"""

    @pytest.fixture(autouse=True)
    def _stub_engine(self, monkeypatch):
        """默认快回离线引擎（避免测试里网络探测拖慢）。"""
        _install_engine_stub(monkeypatch, available=False)

    def test_default_shadow_off_no_call(self, tmp_path):
        """默认关闭 → record_decision 返回空字符串，无 JSONL 文件。"""
        sched = ProactiveScheduler(on_proactive=lambda t: None)
        sched.load_config({"enabled": True})  # 主开关开、shadow 未配
        r = sched._record_shadow_decision("proactive.intent",
                                          {"scenario": "s"}, "p")
        assert r == ""

    def test_enable_via_load_config(self, tmp_path):
        p = str(tmp_path / "shadow.jsonl")
        sched = ProactiveScheduler(on_proactive=lambda t: None)
        sched.load_config({
            "shadow_decision": {"enabled": True, "jsonl_path": p},
        })
        assert sched._shadow is not None
        assert sched._shadow.enabled()

    def test_full_intent_generation_path_records(self, tmp_path):
        """完整路径：意图命中 → LLM 生成 → 投递 → 应产生 decision + outcome。"""
        p = str(tmp_path / "shadow.jsonl")
        sched = ProactiveScheduler(on_proactive=lambda t: None)
        sched.load_config({
            "enabled": True,
            "shadow_decision": {"enabled": True, "jsonl_path": p},
        })
        did = sched._record_shadow_decision(
            "proactive.intent",
            context={"scenario": "night_late_work"},
            fallback_prompt="休息一下吧？",
        )
        assert did, "启用后 record_decision 应返回非空 ID"
        sched._record_shadow_outcome(did, delivered=True, prompt="生成文本")
        assert _wait_for_worker(sched._shadow), "worker 未处理队列"
        lines = _read_jsonl(p)
        types = {x["record_type"] for x in lines}
        assert types == {"decision", "outcome"}

    def test_disabled_after_config_no_new_records(self, tmp_path):
        """关掉后 → 不新增记录。"""
        p = str(tmp_path / "shadow.jsonl")
        sched = ProactiveScheduler(on_proactive=lambda t: None)
        sched.load_config({
            "shadow_decision": {"enabled": True, "jsonl_path": p},
        })
        did = sched._record_shadow_decision("proactive.intent", {}, "p")
        assert did
        sched._record_shadow_outcome(did, delivered=True)
        assert _wait_for_worker(sched._shadow)
        assert len(_read_jsonl(p)) == 2

        # 关闭
        sched.load_config({"shadow_decision": {"enabled": False}})
        sched._record_shadow_decision("proactive.intent", {}, "p")
        sched._record_shadow_outcome("any", delivered=True)
        time.sleep(0.2)
        assert len(_read_jsonl(p)) == 2  # 不新增

    def test_deliver_accepts_shadow_decision_id(self, tmp_path):
        """_deliver 签名变更向后兼容：老调用（无 shadow_decision_id）不报错。"""
        sched = ProactiveScheduler(on_proactive=lambda t: None)
        delivered = []
        sched.on_proactive = delivered.append
        sched._deliver("hello", source_key="a")
        assert delivered == ["hello"]
        # 新式调用（传 shadow_decision_id）也应工作
        sched._deliver("hello2", source_key="b", shadow_decision_id="")
        assert delivered[-1] == "hello2"


# ── 用例 6：主线程非阻塞性 ────────────────────────────────


class TestMainThreadNonBlocking:
    def test_record_decision_returns_quickly_even_with_slow_engine(
        self, tmp_path, monkeypatch,
    ):
        """引擎慢 200ms 时，主线程的 record_decision 仍应毫秒级返回。"""
        _install_engine_stub(
            monkeypatch,
            response={"verdict": {"value": "yes", "prob": 0.9}},
            available=True,
            delay=0.2,
        )
        p = str(tmp_path / "shadow.jsonl")
        r = ShadowDecisionRecorder({"enabled": True, "jsonl_path": p})
        t0 = time.perf_counter()
        did = r.record_decision(kind="k", context={})
        elapsed = time.perf_counter() - t0
        assert did
        assert elapsed < 0.05, f"主线程被阻塞了 {elapsed:.3f}s（应 <50ms）"


class _FakeGenerator:
    """最小生成器替身：只提供 _start_generation 需要的接口。"""

    def is_available(self) -> bool:
        return True

    def set_callbacks(self, on_generated=None, on_fallback=None):
        self.on_generated = on_generated
        self.on_fallback = on_fallback

    def generate(self, context, fallback_prompt):  # pragma: no cover - 只有异步侧用
        return None


class TestShadowWiringRegression:
    """回归：2026-09-21 修复的两处接线 bug。

    两处都会让 outcome 永远带 **空 decision_id**，被 record_outcome 开头的
    ``if not decision_id: return`` 静默丢弃，于是攒下一堆 decision 却零 outcome，
    测量目的整个作废。

    1. ``_try_intent`` 先设 ``_shadow_decision_id``，随后 ``_start_generation``
       把它清空（现在改为由调用方传入，不再内部清空）。
    2. ``_on_generation_result`` 成功路径传的是已清空的
       ``self._shadow_decision_id``（现在传局部变量）。

    注意：旧用例 ``TestProactiveIntegration.test_full_intent_generation_path_records``
    名为「完整路径」，实际是手动调 ``_record_shadow_decision`` /
    ``_record_shadow_outcome``，绕过了 ``_start_generation`` /
    ``_on_generation_result``，所以这两个 bug 都没被它拦住。
    """

    @pytest.fixture(autouse=True)
    def _stub_engine(self, monkeypatch):
        _install_engine_stub(
            monkeypatch,
            response={"verdict": {"value": "yes", "prob": 0.9}},
            available=True,
        )

    def _scheduler(self, tmp_path):
        p = str(tmp_path / "shadow.jsonl")
        sched = ProactiveScheduler(on_proactive=lambda t: None)
        sched.load_config({
            "enabled": True,
            "shadow_decision": {"enabled": True, "jsonl_path": p},
        })
        sched.set_generator(_FakeGenerator(), llm_generation=True)
        return sched, p

    def test_start_generation_keeps_passed_id(self, tmp_path):
        """回归 1：_start_generation 不得清空调用方传入的 decision_id。"""
        sched, _ = self._scheduler(tmp_path)
        assert sched._start_generation({}, "fb", "s", shadow_decision_id="DID-1") is True
        assert sched._shadow_decision_id == "DID-1", \
            "传入的 decision_id 被 _start_generation 抹掉了"

    def test_generation_success_path_links_outcome_to_decision(self, tmp_path):
        """回归 2（主路径）：LLM 成功生成 → outcome 必须带上同一个 decision_id。"""
        sched, p = self._scheduler(tmp_path)
        did = sched._record_shadow_decision(
            "proactive.intent", {"scenario": "late_night_work"}, "休息一下吧？")
        assert did, "启用后应返回非空 decision_id"
        assert sched._start_generation(
            {"scenario": "late_night_work"}, "休息一下吧？", "late_night_work",
            shadow_decision_id=did,
        ) is True
        sched._on_generation_result("夜深了，歇一会儿？")  # 模拟异步回调（主线程）
        assert _wait_for_worker(sched._shadow)
        outs = [x for x in _read_jsonl(p) if x["record_type"] == "outcome"]
        assert outs, "成功路径没有写出任何 outcome"
        assert outs[-1]["decision_id"] == did, (
            f"outcome 未关联到 decision：期望 {did!r}，"
            f"实得 {[o.get('decision_id') for o in outs]!r}"
        )

    def test_fallback_path_links_outcome_to_decision(self, tmp_path):
        """回归 2（回退路径）：生成失败 → outcome 同样要带上 decision_id。"""
        sched, p = self._scheduler(tmp_path)
        did = sched._record_shadow_decision("proactive.intent", {"scenario": "x"}, "fb")
        sched._start_generation({}, "fb", "x", shadow_decision_id=did)
        sched._on_generation_fallback("模板兜底")
        assert _wait_for_worker(sched._shadow)
        outs = [x for x in _read_jsonl(p) if x["record_type"] == "outcome"]
        assert outs and outs[-1]["decision_id"] == did


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "--tb=short"]))
