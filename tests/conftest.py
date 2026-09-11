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
    saver = getattr(config_mod, "async_config_saver", None)
    if saver is not None:
        for name in ("submit", "save", "flush", "shutdown"):
            if hasattr(saver, name):
                monkeypatch.setattr(saver, name, lambda *a, **k: None, raising=False)

    return spy
