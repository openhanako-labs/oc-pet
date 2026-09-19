# -*- coding: utf-8 -*-
"""全局闸门的**接线**守卫（O1-P2）。

本文件明确自己的边界：它**不测行为**（行为在 `test_llm_gate.py` 的 23 例里），
只盯着"三条真实调用路径有没有真的挂到闸门上"。

为什么需要这种"看源码"的测试：闸门是**可选增强**——接线被谁顺手删掉，
程序照样跑、测试照样绿，只有下一次 429 才知道。本项目已有同类先例
（`test_do_aliases.py` 断言 prompt 与白名单一致性）。
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _src(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


# ── 屏幕视觉 ──────────────────────────────────────────────


def test_vision_path_acquires_gate():
    s = _src("core/perception/screen.py")
    assert 'gate.acquire("vision")' in s, "屏幕视觉没有过闸门"
    assert "gate.notify_429" in s, "视觉 429 没有通知全局闸门"
    assert "gate.notify_ok()" in s, "视觉成功没有复位闸门计数"


def test_vision_path_always_releases():
    """槽必须归还——成功/失败/异常三条路都要（否则闸门被永久占住）。"""
    s = _src("core/perception/screen.py")
    assert "gate.release()" in s
    # 归还出现在 finally 里，且出现两次（视觉 + 语义增强）
    assert s.count("finally:\n            # 并发槽只盖住这次视觉调用") == 1
    assert s.count("gate.release()") >= 2


def test_enrich_path_acquires_gate():
    s = _src("core/perception/screen.py")
    assert 'gate.acquire("enrich")' in s, "语义增强没有过闸门"


def test_gate_skip_is_logged_not_silent():
    """被闸门跳过要留痕，否则"为什么没增强"会变成新的谜。"""
    s = _src("core/perception/screen.py")
    assert "屏幕感知被全局闸门跳过" in s


# ── adapter（429 的发现端）─────────────────────────────────


def test_adapter_reports_429_to_gate():
    s = _src("core/harness_adapter.py")
    assert s.count("notify_429") >= 2, "重试路径与最终错误路径都应上报 429"
    assert "notify_ok()" in s, "成功路径应复位闸门"


# ── 启动接线 ──────────────────────────────────────────────


def test_pet_configures_gate_at_startup():
    s = _src("pet.py")
    assert "configure_gate(" in s, "启动时没有按配置建闸门"
    assert "self._init_llm_gate()" in s, "闸门初始化没有被调用"


def test_config_templates_carry_gate_block():
    """两份配置模板都要带 llm_gate 块，否则用户不知道有这个开关。"""
    import json

    for name in ("config.template.json",):
        cfg = json.loads((ROOT / name).read_text(encoding="utf-8"))
        assert "llm_gate" in cfg, f"{name} 缺 llm_gate 块"
        assert "budgets" in cfg["llm_gate"]
