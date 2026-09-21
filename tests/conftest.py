"""测试全局守卫。

## 为什么需要（2026-09-11 真实事故）

`ui/settings_dialog.py:1202` 在 `_apply_package_selection` **函数体内**做局部导入：

    from config import save_config
    save_config(self._config)

而 `:29` 又做了**模块级**导入：

    from config import load_config, save_config

两种方式都会绕过测试里常见的
`monkeypatch.setattr(sd, "save_config", ...)` —— 前者从 config 重新取名字，
后者早在 import 时就把**原函数对象**绑进了本模块命名空间。

后果（时间戳全部对上）：每次跑全量测试都会把用户的 `config.json` 改成
「某个角色包启用 + 其余禁用」，于是桌宠下次启动
「0 enabled agents，桌面宠物不会显示」。

曾误判为「有野实例写回配置」；真凶是测试自己写盘。

## 守卫策略（双覆盖）

1. **源头**：替换 `config.save_config` —— 之后才 import 的模块会拿到替换版
2. **已加载模块**：扫描 `sys.modules`，把那些此前已按名字绑定原函数的模块
   一并替换 —— 覆盖 `settings_dialog` 这类模块级导入

两者缺一不可，因为导入时机不同。

测试若需要验证「确实写了盘」，自行 monkeypatch 一个记录器覆盖本守卫即可。
"""
from __future__ import annotations

import pathlib
import sys

import pytest


class _ConfigWriteSpy:
    """记录所有写盘调用，但不落盘。"""

    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, cfg):
        import copy
        self.calls.append(copy.deepcopy(cfg))

    def __bool__(self):
        return bool(self.calls)

    @property
    def last(self):
        return self.calls[-1] if self.calls else None


@pytest.fixture(autouse=True)
def _block_real_config_writes(monkeypatch):
    """禁止任何测试写真实的 config.json（覆盖局部导入与模块级导入两种路径）。"""
    import config as config_mod

    original = getattr(config_mod, "save_config", None)
    spy = _ConfigWriteSpy()
    monkeypatch.setattr(config_mod, "save_config", spy, raising=False)

    # 扫描已加载模块：把此前绑定了原函数的，一并换成 spy
    if original is not None:
        for mod in list(sys.modules.values()):
            if mod is None or mod is config_mod:
                continue
            if getattr(mod, "save_config", None) is original:
                monkeypatch.setattr(mod, "save_config", spy, raising=False)

    # 异步保存器（窗口位置等高频写入路径）
    #
    # 2026-09-21：这里原先只拦 submit/save/flush/shutdown，**漏了 schedule** ——
    # 而 settings_dialog._switch_pet 走的正是 schedule()。schedule 会启动一个
    # 防抖线程（150ms 后调模块级 save_config），线程醒来时测试往往已经结束、
    # monkeypatch 已撤销，于是用**原函数**把用户的 config.json 写了。
    # 症状极隐蔽：落不落盘取决于时序运气，跑十次可能只中一次（本文件的自检
    # 用例查的是 config.save_config 是否被替换，根本查不出跨测试边界的落盘）。
    # 因此 schedule 必须一并拦掉（不让线程被创建），再用 _run 做双保险。
    saver = getattr(config_mod, "async_config_saver", None)
    if saver is not None:
        for name in ("schedule", "submit", "save", "flush", "shutdown"):
            if hasattr(saver, name):
                monkeypatch.setattr(saver, name, lambda *a, **k: None, raising=False)
        if hasattr(saver, "_run"):
            monkeypatch.setattr(saver, "_run", lambda *a, **k: None, raising=False)

    return spy


@pytest.fixture(scope="session", autouse=True)
def _workspace_config_snapshot_guard():
    """会话级兜底：整个会话期间 workspace 的 config.json 不得被改写。

    为什么还要这一层（2026-09-21）：`_block_real_config_writes` 是**函数级**
    守卫，测试一结束就撤销；而 `_AsyncConfigSaver` 的防抖线程是**跨测试边界**
    醒来的——它能正好落在这条缝里用原函数落盘。这类“守卫看起来在、实际有洞”
    的问题靠逐个堵入口防不住，所以再加一道与文件对账的兜底：
    会话结束若发现 config.json 被改 → 恢复原内容，并让本次会话报错。

    CI 上没有 config.json（被 .gitignore 排除）→ 自动跳过。
    """
    cfg_path = pathlib.Path(__file__).resolve().parent.parent / "config.json"
    original: bytes | None = None
    try:
        if cfg_path.is_file():
            original = cfg_path.read_bytes()
    except OSError:
        original = None

    yield

    if original is None:
        return
    try:
        current = cfg_path.read_bytes()
    except OSError:
        return
    if current == original:
        return

    note = "未恢复"
    try:
        cfg_path.write_bytes(original)
        note = "已自动恢复原内容"
    except OSError as e:
        note = f"恢复失败：{e}"
    raise AssertionError(
        f"测试改写了 workspace 的 config.json（{note}）——说明有写盘路径绕过了 "
        "conftest 的 _block_real_config_writes 守卫。请检查是否又出现新的落盘入口"
        "（尤其是会跨测试边界的后台线程/定时器）。"
    )
