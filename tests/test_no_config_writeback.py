"""回归锁定：运行时启发式不得写回用户配置

2026-09-11 实测（本次会话真实踩到）：

    config 载入:  miku=false, shizuku=true      ← shizuku 目录不存在
    shizuku 无 sprites → 禁用
    Config updated: disabled agents without sprites   ← 写回 config.json
    结果: 两个 agent 都是 false → 0 enabled agents → 桌宠静默不启动

    用户下次启动看到的只是「没有启用的 agent」，桌宠什么也不显示，
    而根因（一次目录暂时不可用）已经被写进配置文件，永久生效。

这正是最初评审里标出的「启动启发式会静默回写用户配置」。

不变量：
    运行时启发式只能影响**本次运行**，不得修改用户的持久配置。
    模型暂时不可用 ≠ 永久关闭。
"""
from __future__ import annotations

import pathlib

import pytest

from pet_manager import PetManager


def _bare_manager(agents, has_sprites=False):
    """构造只有必要字段的 PetManager（不跑 __init__）。"""
    pm = PetManager.__new__(PetManager)
    pm._config = {"agents": [dict(a) for a in agents]}
    pm._save_config = lambda: pytest.fail("运行时启发式不得写盘！")
    pm._has_sprites = lambda _aid: has_sprites
    return pm


# ══════════════════════════════════════════════════════════════
#  核心：不落盘
# ══════════════════════════════════════════════════════════════

def test_validate_does_not_persist():
    """无资源的 agent 被跳过，但绝不调用 _save_config。"""
    pm = _bare_manager(
        [{"id": "ghost", "enabled": True}, {"id": "real", "enabled": True}],
        has_sprites=False,
    )

    skipped = pm._validate_enabled_agents()

    assert sorted(skipped) == ["ghost", "real"]
    # 内存视图被改（本次不启动它们）
    assert all(a["enabled"] is False for a in pm._config["agents"])


def test_healthy_agent_is_not_touched():
    pm = _bare_manager([{"id": "miku", "enabled": True}], has_sprites=True)

    skipped = pm._validate_enabled_agents()

    assert skipped == []
    assert pm._config["agents"][0]["enabled"] is True


def test_already_disabled_stays_untouched():
    """本来就禁用的不参与判定（也不算「被跳过」）。"""
    pm = _bare_manager([{"id": "off", "enabled": False}], has_sprites=False)

    skipped = pm._validate_enabled_agents()

    assert skipped == []


def test_missing_id_is_skipped_safely():
    pm = _bare_manager([{"enabled": True}], has_sprites=False)

    assert pm._validate_enabled_agents() == []


# ══════════════════════════════════════════════════════════════
#  源码级守卫：防回退
# ══════════════════════════════════════════════════════════════

def test_source_has_no_persist_log():
    """'Config updated: disabled agents without sprites' 只在那段写盘逻辑里出现。

    它消失 = 写盘逻辑已移除。若有人重新加回，这条会红。
    """
    src = (pathlib.Path(__file__).resolve().parent.parent
           / "pet_manager.py").read_text(encoding="utf-8")
    assert "Config updated: disabled agents without sprites" not in src


def test_validation_result_is_not_saved_in_launch_all():
    """launch_all 里不得因「跳过无资源 agent」而写盘。"""
    src = (pathlib.Path(__file__).resolve().parent.parent
           / "pet_manager.py").read_text(encoding="utf-8")

    idx = src.index("_validate_enabled_agents()")
    window = src[idx:idx + 400]
    assert "_save_config()" not in window, (
        "launch_all 不得把「本次无资源」写回用户配置——"
        "模型暂时不可用不该变成永久关闭"
    )
