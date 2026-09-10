"""接线测试：Live2D 帧步骤的错误可见性（_frame_step）

覆盖 2026-09-10 修：_frame_update 原为 10 个独立 try/except + logger.debug。
生产日志级别 INFO 下不可见 → 每秒最多 600 次静默失败。
修后：失败记一次 warning + 进可查询的降级状态；恢复时再报一次。
"""
from __future__ import annotations

import logging

import pytest

from avatar.live2d_renderer import Live2DRenderer


@pytest.fixture
def renderer():
    """不加载模型的最小 renderer 实例（只测 _frame_step 机制）。"""
    return Live2DRenderer.__new__(Live2DRenderer)


def test_success_returns_value_and_no_degradation(renderer):
    assert renderer._frame_step("ok", lambda x: x * 2, 21) == 42
    assert renderer.frame_degraded is False
    assert renderer.frame_failed_steps() == []


def test_failure_is_recorded_and_visible(renderer, caplog):
    def boom():
        raise RuntimeError("模型参数缺失")

    with caplog.at_level(logging.WARNING, logger="avatar.live2d_renderer"):
        assert renderer._frame_step("UpdateMouth", boom) is None

    assert renderer.frame_degraded is True
    assert renderer.frame_failed_steps() == ["UpdateMouth"]

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, "失败必须可见（原本是 debug，INFO 下不可见）"
    assert "UpdateMouth" in warnings[0].getMessage()


def test_repeated_failure_logs_only_once(renderer, caplog):
    """60fps 下不能逐帧刷屏。"""
    def boom():
        raise RuntimeError("x")

    with caplog.at_level(logging.WARNING, logger="avatar.live2d_renderer"):
        for _ in range(50):
            renderer._frame_step("UpdateExpression", boom)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, f"重复失败应只报一次，实际 {len(warnings)} 次"
    assert renderer.frame_failed_steps() == ["UpdateExpression"]


def test_recovery_is_reported_and_clears_state(renderer, caplog):
    def boom():
        raise RuntimeError("临时失败")

    renderer._frame_step("UpdateBreath", boom)
    assert renderer.frame_degraded is True

    with caplog.at_level(logging.INFO, logger="avatar.live2d_renderer"):
        assert renderer._frame_step("UpdateBreath", lambda: "ok") == "ok"

    assert renderer.frame_failed_steps() == []
    assert renderer.frame_degraded is False, "全部恢复后降级状态应清除"
    assert any("已恢复" in r.getMessage() for r in caplog.records)


def test_one_step_failing_does_not_stop_others(renderer):
    """单个步骤失败不得拖垮其余步骤——原实现的 try 隔离要保住。"""
    calls = []

    def boom():
        raise RuntimeError("boom")

    renderer._frame_step("Bad", boom)
    renderer._frame_step("Good1", lambda: calls.append("a"))
    renderer._frame_step("Good2", lambda: calls.append("b"))

    assert calls == ["a", "b"]
    assert renderer.frame_failed_steps() == ["Bad"]


def test_degraded_state_queryable_without_any_failure(renderer):
    """未初始化过时也可安全查询（新增属性不得抛）。"""
    assert renderer.frame_degraded is False
    assert renderer.frame_failed_steps() == []
