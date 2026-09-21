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


# ══════════════════════════════════════════════════════════════
#  反向守卫：测试不得写真实的 config.json
# ══════════════════════════════════════════════════════════════

def test_conftest_guard_blocks_real_config_write():
    """2026-09-11 事故：测试把用户的 config.json 写成了别的角色。

    `ui/settings_dialog.py` 有两种导入方式：
      :29   模块级 `from config import save_config`
      :1202 函数内 `from config import save_config`
    两者都会绕过 `monkeypatch.setattr(sd, "save_config", ...)`。
    tests/conftest.py 的 autouse 守卫在**源头 + 已加载模块**双覆盖。

    本测试功能性验证：调用后被记录，且不留盘痕迹。
    """
    import config as config_mod

    assert type(config_mod.save_config).__name__ == "_ConfigWriteSpy", (
        f"conftest 守卫未生效，当前是 {type(config_mod.save_config).__name__}"
    )

    config_mod.save_config({"agents": [{"id": "__guard_probe__", "enabled": True}]})

    assert config_mod.save_config.calls, "守卫应记录到这次调用"
    assert config_mod.save_config.last["agents"][0]["id"] == "__guard_probe__"


def test_conftest_guard_also_covers_module_level_imports():
    """模块级导入（如 settings_dialog）拿到的也应是 spy，不是原函数。"""
    import config as config_mod

    from ui import settings_dialog as sd

    if not hasattr(sd, "save_config"):
        pytest.skip("settings_dialog 未导入 save_config")

    assert sd.save_config is config_mod.save_config, (
        "settings_dialog 里绑的 save_config 未被守卫替换——"
        "它会在测试中写真实 config.json"
    )


def test_real_config_file_is_untouched_by_this_test():
    """确认本测试不会改动 workspace 下的 config.json。"""
    import json
    from pathlib import Path

    cfg = Path(__file__).resolve().parent.parent / "config.json"
    if not cfg.exists():
        pytest.skip("无 config.json")

    before = tuple((a["id"], a["enabled"])
                   for a in json.loads(cfg.read_text(encoding="utf-8")).get("agents", []))

    # 模拟一次「切换角色包」的写入调用
    import config as config_mod
    config_mod.save_config({"agents": [{"id": "__probe__", "enabled": True}]})

    after = tuple((a["id"], a["enabled"])
                  for a in json.loads(cfg.read_text(encoding="utf-8")).get("agents", []))
    assert before == after, "测试写到了真实 config.json"


# ══════════════════════════════════════════════════════════
#  2026-09-21 事故回归：防抖线程跨测试边界落盘
# ══════════════════════════════════════════════════════════

def test_async_saver_schedule_is_blocked_by_guard():
    """守卫必须拦掉 ``async_config_saver.schedule``。

    原 bug：守卫只拦了 submit/save/flush/shutdown，**漏了 schedule**，而
    ``settings_dialog._switch_pet`` 走的正是 schedule()。它会启动一个 150ms
    防抖线程，线程醒来时测试往往已结束、monkeypatch 已撤销，于是用**原函数**
    save_config 把用户的 config.json 写了（合并式写：45 个键还在，只有
    agents/character/character_package 被覆盖，症状像"自己改过"）。

    为什么旧自检没抓到：``test_conftest_guard_blocks_real_config_write`` 查的是
    "调用 config.save_config 会不会落盘"，而这条路径的落盘发生在**测试边界之外**、
    用的是**原函数**，与 spy 无关。所以这里改查修复点本身：schedule 不得生效。
    """
    import config as config_mod

    saver = config_mod.async_config_saver
    before_thread = saver._thread

    saver.schedule({"agents": [{"id": "__race_probe__", "enabled": True}]})

    assert saver._pending is None, (
        "守卫未拦下 schedule：_pending 已被写入，防抖线程将会在测试边界外落盘"
    )
    assert saver._thread is before_thread, "守卫未拦下 schedule：防抖线程被创建了"


def test_schedule_then_wait_does_not_touch_config_json():
    """复现原事故条件：调度一次 + 等过防抖窗口 → 文件必须原封不动。

    防抖窗口是 150ms（``config._AsyncConfigSaver`` 默认），这里等 4 倍。
    """
    import time
    from pathlib import Path

    import config as config_mod

    cfg = Path(__file__).resolve().parent.parent / "config.json"
    if not cfg.exists():
        pytest.skip("无 config.json")
    before = cfg.read_bytes()

    config_mod.async_config_saver.schedule({
        "agents": [{"id": "__race_probe__", "enabled": True}],
        "character": "__race_probe__",
    })

    time.sleep(0.6)  # 4× debounce：线程若存在，一定已经醒来写过盘

    assert cfg.read_bytes() == before, (
        "async_config_saver 在等待窗口后写了真实 config.json"
    )


# ══════════════════════════════════════════════════════════
#  2026-09-21 事故回归：写盘方只能提交自己拥有的键
# ══════════════════════════════════════════════════════════

def test_pet_manager_submits_only_agents_slice():
    """PetManager 只能交 `agents` 切片，不得交整份快照。

    原 bug：`update_agent_cfg` 把 `self._config`（**启动时读到的整份快照**）
    交给共享防抖器，而防抖器的旧语义是"最后提交者胜"——一次拖拽就把用户在
    设置面板里改过的开关整体盖回启动值（重启后开关复原）。
    """
    src = (pathlib.Path(__file__).resolve().parent.parent
           / "pet_manager.py").read_text(encoding="utf-8")

    assert 'schedule({"agents":' in src, "PetManager 应只提交 agents 切片"
    assert "schedule(self._config)" not in src, (
        "PetManager 又把整份快照交给防抖器了——拖拽会盖掉用户刚改的设置"
    )


def test_no_full_config_snapshot_is_scheduled():
    """全仓扫描：任何地方都不得把整份配置快照交给防抖写盘器。

    共享防抖器的合并是**顶层补丁**语义，交整份快照等于"我拥有所有键"，
    会把别的写入者（设置面板、另一个桌宠实例、手改 config.json）的改动盖掉。
    需要写哪个键就只交哪个键。
    """
    root = pathlib.Path(__file__).resolve().parent.parent
    offenders: list[str] = []
    for rel in ("pet.py", "pet_manager.py", "main.py"):
        for src_path in [root / rel]:
            if src_path.is_file():
                _scan_snapshot_schedules(src_path, offenders)
    for sub in ("pet_mixins", "ui", "core"):
        for src_path in (root / sub).glob("*.py"):
            _scan_snapshot_schedules(src_path, offenders)

    assert not offenders, (
        "这些地方把整份 config 快照交给了防抖写盘器：" + ", ".join(offenders)
    )


def _scan_snapshot_schedules(src_path: pathlib.Path, offenders: list[str]) -> None:
    text = src_path.read_text(encoding="utf-8")
    for pattern in ("async_config_saver.schedule(self.config)",
                    "async_config_saver.schedule(self._config)"):
        if pattern in text:
            offenders.append(str(src_path.name))
