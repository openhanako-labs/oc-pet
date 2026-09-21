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
    #
    # 2026-09-21：补上 `isinstance(cur, _ConfigWriteSpy)` 这一支。
    # 原实现只替换"绑的是原函数"的模块，而模块若在**某个用例内部**首次导入，
    # 它绑到的是那个用例的 spy；用例结束后 monkeypatch 会把 config.save_config
    # 还原成原函数，却不会碰 `模块.save_config`（那个属性当初不是它设的）——
    # 于是模块手里捏着一个**上一个用例的旧 spy**。后果：
    #   · 某个用例断言自己拿到的 spy.calls → 永远为空（写跑到了旧 spy 里）
    #   · 该文件的断言“模块里绑的应是当前 spy”随机失败，取决于文件顺序
    # 把旧 spy 也归入“需替换”即可，与导入时机/用例顺序无关。
    if original is not None:
        for mod in list(sys.modules.values()):
            if mod is None or mod is config_mod:
                continue
            cur = getattr(mod, "save_config", None)
            if cur is original or isinstance(cur, _ConfigWriteSpy):
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


@pytest.fixture(autouse=True)
def _isolate_shadow_decisions(monkeypatch, tmp_path):
    """禁止任何测试写入仓库真实的 `shadow_decisions/` 目录。

    ## 为什么（2026-09-21 实测）

    `tests/test_shadow_decision.py` 里 `ShadowDecisionRecorder({"enabled": True})`
    **不传 `jsonl_path`** → 落到默认相对目录 `./shadow_decisions/`，于是
    `record_outcome("did", ...)` 往**真实数据文件**追加了一条 `decision_id="did"`
    的占位记录。

    实测复现：跑一次 `pytest tests/test_shadow_decision.py`，
    `shadow_decisions/shadow_YYYY-MM-DD.jsonl` 从 6 行变 7 行。

    ## 为什么严重

    这份 JSONL 是影子模式攒数据用的（分析目标：「引擎判『不值得』的那些，
    事后看真的没必要吗」）。测试灌进去的占位记录永远 join 不上真实 decision，
    只会污染统计。

    ## 同 2808c8a 的教训

    仓库刚为 `config.json` 修过一模一样的事故（测试改写真实产物），
    那里用的是「源头替换 + 会话级对账」两道守卫；这里给新产物补上第一道。

    Args:
        monkeypatch: pytest 夹具
        tmp_path: 每个用例独立的临时目录
    """
    import core.shadow_decision as shadow_mod

    fake_dir = tmp_path / "shadow_decisions"
    monkeypatch.setattr(shadow_mod, "DEFAULT_JSONL_DIR", str(fake_dir),
                        raising=False)
    return fake_dir


@pytest.fixture(scope="session", autouse=True)
def _shadow_decisions_snapshot_guard():
    """会话级兜底：整个会话期间 workspace 的 `shadow_decisions/` 不得被改写。

    为什么还要这一层：`_isolate_shadow_decisions` 是**函数级**守卫，只盖住
    通过默认路径落盘的实例；若某处直接构造了带真实路径的记录器、或用了
    后台线程跨测试边界写盘（`_AsyncConfigSaver` 就是这么漏的），函数级
    守卫拦不住。所以再加一道与文件对账的兜底：会话结束若发现被改 → 恢复
    原内容，并让本次会话报错。
    """
    root = pathlib.Path(__file__).resolve().parent.parent / "shadow_decisions"
    before: dict[pathlib.Path, bytes] = {}
    if root.is_dir():
        for p in root.rglob("*"):
            try:
                if p.is_file():
                    before[p] = p.read_bytes()
            except OSError:
                continue

    yield

    if not root.is_dir():
        return
    added: list[str] = []
    changed: list[str] = []
    for p in root.rglob("*"):
        try:
            if not p.is_file():
                continue
            data = p.read_bytes()
        except OSError:
            continue
        old = before.get(p)
        if old is None:
            added.append(p.name)
        elif old != data:
            changed.append(p.name)
    if not added and not changed:
        return

    # 新增的删掉；被追加的截回原长度（只做能确保无损的恢复）
    for name in added:
        try:
            (root / name).unlink()
        except OSError:
            pass
    for name in changed:
        p = root / name
        try:
            p.write_bytes(before[p])
        except OSError:
            pass
    raise AssertionError(
        f"测试改写了 workspace 的 shadow_decisions/（新增 {added}，改动 {changed}）"
        "——说明有写盘路径绕过了 conftest 的 _isolate_shadow_decisions 守卫。"
        "请给对应测试传显式 jsonl_path，或检查是否又出现新的落盘入口。"
    )


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


# ══════════════════════════════════════════════════════════════
#  会话级守卫：pin 一律不得落到真实 ~/.hanako/pets/
# ══════════════════════════════════════════════════════════════


@pytest.fixture(scope="session", autouse=True)
def _block_real_pin_writes(tmp_path_factory):
    """会话级：把 pin 落盘路径钉到临时目录，收尾再核对真实文件没被动过。

    2026-09-21。`_validate_pin_async()` 会起一个后台线程去核对「会话是否仍存在」，
    核对失败就 `_save_pinned_sessions()`。这个线程**没人 join**，于是常常在
    测试结束、monkeypatch 撤销**之后**才落盘：

      - 写进真实的 `~/.hanako/pets/session_ophelia.json`（实测 189B → 73B）；
      - 更阴的是落进**下一个测试**当时正被 monkeypatch 的路径，把人家刚写好的
        pin 冲掉 —— 全量回归里偶发的 `assert None == 'sess_ROUNDTRIP'` 就是它。

    定位手段（留着以后用）：临时挂个 `-p` 插件包住 `_save_pinned_sessions`，
    打印目标路径 + 调用栈，一眼能看到
    `threading.py <- harness_adapter.py:342` 在写真实路径。

    上面那个 config 守卫是「改了就报错」；这里改成「**先拦住**、再对账」：
    pin 丢了会让桌宠下次对话重开会话，拦住比报警值钱。
    """
    import core.harness_adapter as ha

    class _Probe:
        agent_id = "ophelia"

    real = None
    try:
        real = ha.HanakoPetAdapter._pinned_path(_Probe())
    except Exception:  # noqa: BLE001  拿不到就算了，下面按 None 跳过对账
        real = None
    before = None
    try:
        if real is not None and real.is_file():
            before = real.read_bytes()
    except OSError:
        before = None

    sandbox = tmp_path_factory.mktemp("pin-sandbox")
    orig = ha.HanakoPetAdapter.__dict__.get("_pinned_path")
    ha.HanakoPetAdapter._pinned_path = (
        lambda self=None: sandbox / f"session_{getattr(self, 'agent_id', 'x')}.json")
    yield sandbox

    # 收尾顺序很重要：**先等孤儿线程写完**（重定向还在，它们写进沙箱），
    # 再撤重定向、再对账。反过来的话，线程会在属性还原之后才落盘 →
    # 又写回真实文件（第一版就是这么漏的：前 189B → 后 72B）。
    import threading

    for t in threading.enumerate():
        if t.name == "pin-validate" and t.is_alive():
            t.join(timeout=15)

    if orig is not None:
        ha.HanakoPetAdapter._pinned_path = orig

    if real is None:
        return
    try:
        now = real.read_bytes() if real.is_file() else None
    except OSError:
        return
    if now == before:
        return

    note = "未恢复"
    try:
        if before is not None:
            real.write_bytes(before)
            note = "已自动恢复原内容"
    except OSError as e:
        note = f"恢复失败：{e}"
    raise AssertionError(
        f"测试改写了真实 pin（{real}，{note}）——说明有落盘路径绕过了 "
        "_block_real_pin_writes 守卫。最可能是后台线程/定时器跨了测试边界"
        "（`_validate_pin_async` 的 pin-validate 线程就是历史元凶）。"
    )
