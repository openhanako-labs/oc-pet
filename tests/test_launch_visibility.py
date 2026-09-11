"""「桌宠为什么没起来」必须让用户看得见。

## 为什么要有这个测试

2026-09-11 实测：桌宠静默不启动，而**界面上一个字都没有**。
原因是 `launch_all` 的早退分支只写日志——而桌宠没起来时**没有窗口**，
没有窗口就没有托盘，于是没有任何通道能告诉用户。

（我当时是翻日志才知道的，为此让用户重启了三次桌宠。）

## 锁住什么

凡是「一个窗口都不会建起来」的路径，都必须调 `notify_user`：

  1. characters/ 里一个角色目录都没有
  2. 没有启用的 agent（全 disabled）
  3. 启用的 agent 全都缺模型资源 → 本次被跳过
  4. 第一个桌宠窗口就创建失败（launch_window 抛异常）

另外：`notify_user` 本身在无 Qt / 无托盘时必须安静返回，不能反过来把启动搞崩。
"""
from __future__ import annotations

import pytest

from pet_manager import PetManager


@pytest.fixture
def notified(monkeypatch):
    """拦截通知，记录 (title, body)。"""
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(PetManager, "notify_user",
                        lambda self, t, b: calls.append((t, b)))
    return calls


def _manager(monkeypatch, agents, *, has_characters=True, has_sprites=True):
    """构造一个不碰 Hanako WS / 不建窗口的 PetManager。"""
    m = PetManager.__new__(PetManager)
    m._windows = {}
    m._config = {"agents": agents}
    m._config_mtime = 0
    m._launch_error_logged = set()
    m._own_tray = None
    m._own_tray_menu = None
    m._bridge = None
    m._bridge_enabled = False
    m._ws_client = None
    m._session_manager = None

    monkeypatch.setattr(PetManager, "_reload_config", lambda self: False)
    monkeypatch.setattr(PetManager, "_has_any_characters",
                        lambda self: has_characters)
    monkeypatch.setattr(PetManager, "_has_sprites",
                        lambda self, aid: has_sprites)
    return m


# ══════════════════════════════════════════════════════════════
#  1. 没有角色目录
# ══════════════════════════════════════════════════════════════

def test_notifies_when_no_character_dir(monkeypatch, notified):
    m = _manager(monkeypatch, [], has_characters=False)

    m.launch_all()

    assert len(notified) == 1, "一个窗口都没建起来就必须提示"
    title, body = notified[0]
    assert "未启动" in title
    assert "角色" in body


# ══════════════════════════════════════════════════════════════
#  2. 没有启用的 agent
# ══════════════════════════════════════════════════════════════

def test_notifies_when_nothing_enabled(monkeypatch, notified):
    m = _manager(monkeypatch, [
        {"id": "miku", "enabled": False},
        {"id": "rory", "enabled": False},
    ])

    m.launch_all()

    assert len(notified) == 1
    _, body = notified[0]
    assert "启用" in body


def test_notifies_when_all_enabled_are_skipped(monkeypatch, notified):
    """本次的实测场景：miku 已启用但资源不可用 → 0 个窗口。"""
    m = _manager(monkeypatch, [{"id": "miku", "enabled": True}],
                 has_sprites=False)

    m.launch_all()

    assert len(notified) == 1
    _, body = notified[0]
    assert "miku" in body, "得说清是哪个角色缺资源，否则用户无从下手"


def test_skippedIds_are_never_written_back(monkeypatch, notified):
    """提示的同时不得把「暂时不可用」固化成「永久关闭」。"""
    m = _manager(monkeypatch, [{"id": "miku", "enabled": True}],
                 has_sprites=False)

    m.launch_all()

    # _validate_enabled_agents 只改内存视图，_save_config 不该被调用
    assert m._config["agents"][0]["enabled"] is False, "本次内存里应被跳过"


# ══════════════════════════════════════════════════════════════
#  3. 窗口创建失败
# ══════════════════════════════════════════════════════════════

def test_notifies_when_window_creation_fails(monkeypatch, notified):
    m = _manager(monkeypatch, [{"id": "miku", "enabled": True}])
    monkeypatch.setattr(PetManager, "get_sprite_dir",
                        lambda self, aid: None)

    import builtins

    real_import = builtins.__import__

    def _fail_pet(name, *a, **kw):
        if name == "pet":
            raise RuntimeError("GPU 初始化失败")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _fail_pet)

    m.launch_all()

    assert len(notified) == 1
    title, body = notified[0]
    assert "失败" in title
    assert "GPU" in body


def test_failed_agent_notifies_only_once(monkeypatch, notified):
    """同一 agent 反复失败不该刷屏（用户会被气泡烦死）。"""
    m = _manager(monkeypatch, [{"id": "miku", "enabled": True}])
    monkeypatch.setattr(PetManager, "get_sprite_dir", lambda self, aid: None)

    import builtins

    real_import = builtins.__import__

    def _fail_pet(name, *a, **kw):
        if name == "pet":
            raise RuntimeError("boom")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _fail_pet)

    m.launch_all()
    m.launch_all()
    m.launch_all()

    assert len(notified) == 1


# ══════════════════════════════════════════════════════════════
#  4. notify_user 自身必须安静失败
# ══════════════════════════════════════════════════════════════

def test_notify_user_is_silent_without_tray(monkeypatch):
    """无托盘可用时只写日志——提示是 UX 兜底，不能反过来炸掉启动。"""
    m = PetManager.__new__(PetManager)
    m._windows = {}
    m._own_tray = None
    m._own_tray_menu = None

    monkeypatch.setattr(PetManager, "tray_icon", lambda self: None)
    m.notify_user("t", "b")  # 不抛异常即通过


def test_notify_user_swallows_tray_errors(monkeypatch):
    class _Boom:
        def supportsMessages(self):
            raise RuntimeError("tray 炸了")

    m = PetManager.__new__(PetManager)
    m._windows = {}
    m._own_tray = None
    m._own_tray_menu = None

    monkeypatch.setattr(PetManager, "tray_icon", lambda self: _Boom())
    m.notify_user("t", "b")  # 不抛异常即通过


def test_one_window_without_tray_does_not_break_launch(monkeypatch, notified):
    """有窗口但托盘不可用时，其余逻辑照常。"""
    m = _manager(monkeypatch, [
        {"id": "miku", "enabled": True},
        {"id": "rory", "enabled": False},
    ])
    monkeypatch.setattr(PetManager, "launch_window", lambda self, aid: None)

    m.launch_all()

    assert notified == []
