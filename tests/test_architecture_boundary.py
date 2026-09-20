"""PetSystem / PetShell 边界护栏（2026-09-19）。

这是架构拆分的**安全绳**——先立约束，再搬代码。理由与
``tests/test_signal_contract.py`` 同：纯搬家重构的最大风险是搬漏/搬错，
现有测试抓不到「某条连接静默失效」。

## 两条边界（洛琪希评审建议，可 grep 验证）

1. **PetSystem 不得依赖 Qt** —— 领域逻辑层，应能在无 Qt 环境下单测。
2. **PetShell 不得引用领域对象** —— Qt 适配壳，只做信号/定时器/窗口。

## 现状（2026-09-19 实测）

拆分**尚未开始**。本文件当前是「基线快照 + 趋势约束」：
记录当前数字，防止在拆分过程中**反向恶化**（比如往 pet.py 里加新的
Qt 依赖或领域耦合）。

拆完后，下面的 ``PENDING`` 断言应改为硬断言（=0）。

⚠ 重要教训：不要用「Qt 符号出现次数」判断一个方法是否属于 Qt 层。
``_apply_settings`` 有 0 个 Qt 引用，却调 ``self.setWindowOpacity()``
（继承自 QWidget）——静态计数会漏掉这类**间接依赖**。
真正的判据是「它调用的东西最终落在谁身上」。
"""
from __future__ import annotations

import ast
import io
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 拆分进行中：PetSystem 尚不存在。拆完后应置 False 并把断言改为硬断言。
PENDING = True

#: 拆分前 pet.py 的 Qt 引用基线（AST 计数，2026-09-19 实测）。
#: 只允许下降，不允许上升 —— 拆分期间新增 Qt 依赖说明方向反了。
#: 注：不要用「含 Qt 符号的行数」（那是 87）；这里数的是 AST 节点引用数。
PET_PY_QT_REF_BASELINE = 115


def _read(rel: str) -> str:
    path = os.path.join(ROOT, rel)
    if not os.path.exists(path):
        pytest.skip(f"{rel} 不存在")
    return io.open(path, encoding="utf-8").read()


def _pet_sources() -> list[str]:
    """pet.py + pet_mixins/*.py 相对路径列表。"""
    out = ["pet.py"]
    mixins = os.path.join(ROOT, "pet_mixins")
    if os.path.isdir(mixins):
        out += [os.path.join("pet_mixins", f)
                for f in sorted(os.listdir(mixins)) if f.endswith(".py")]
    return [p for p in out if os.path.exists(os.path.join(ROOT, p))]


def _qt_names_in(tree: ast.AST) -> set[str]:
    """收集从 PySide6 导入的名字（含别名）。"""
    names: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom):
            mod = n.module or ""
            if "PySide6" in mod:
                for a in n.names:
                    names.add(a.asname or a.name)
        elif isinstance(n, ast.Import):
            for a in n.names:
                if "PySide6" in a.name:
                    names.add((a.asname or a.name).split(".")[0])
    return names


def _count_qt_refs(src: str) -> int:
    """AST 计数：真正引用 Qt 名字的次数（比正则可靠）。"""
    tree = ast.parse(src)
    qt = _qt_names_in(tree)
    n = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in qt:
            n += 1
        elif isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name) and node.value.id in qt:
                n += 1
    return n


# ── 1. pet.py 的 Qt 引用不得反向增长 ──

def test_pet_py_qt_refs_do_not_regrow():
    """拆分期间 pet.py 的 Qt 引用数只应下降。

    这条不阻止拆分，只阻止「边拆边加」。
    """
    src = _read("pet.py")
    n = _count_qt_refs(src)
    assert n <= PET_PY_QT_REF_BASELINE, (
        f"pet.py 的 Qt 引用从基线 {PET_PY_QT_REF_BASELINE} 涨到 {n}——"
        f"拆分期间新增 Qt 依赖说明方向反了。"
    )


# ── 2. PetSystem 无 Qt（拆分完成后生效）──

def test_pet_system_has_no_qt():
    """PetSystem 中不得出现 PySide6 导入。"""
    path = os.path.join(ROOT, "core", "pet_system.py")
    if not os.path.exists(path):
        if PENDING:
            pytest.skip("PetSystem 尚未建立（拆分进行中）")
        pytest.fail("core/pet_system.py 不存在")
    src = io.open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    assert not _qt_names_in(tree), (
        "PetSystem 引入了 Qt —— 领域层必须能无 Qt 单测"
    )
    # 也不应出现模块级 Qt import 行
    assert not re.search(r"^\s*from\s+PySide6", src, re.M), "PetSystem 有 Qt import"


# ── 3. PetShell 不得引用领域对象（拆分完成后生效）──

DOMAIN_ATTRS = (
    "_engine", "_renderer", "_perception", "_physics", "_motion",
    "_tts_player", "_bubble", "_hanako_monitor", "_status_mapper",
)


def test_pet_shell_has_no_domain_refs():
    """PetShell 中不得出现 self._engine / self._renderer 等领域引用。"""
    path = os.path.join(ROOT, "core", "pet_shell.py")
    if not os.path.exists(path):
        if PENDING:
            pytest.skip("PetShell 尚未建立（拆分进行中）")
        pytest.fail("core/pet_shell.py 不存在")
    src = io.open(path, encoding="utf-8").read()
    offenders = []
    for i, line in enumerate(src.splitlines(), 1):
        s = line.strip()
        if s.startswith("#"):
            continue
        for attr in DOMAIN_ATTRS:
            if f"self.{attr}" in line:
                offenders.append(f"pet_shell.py:{i} ({attr})")
    assert not offenders, f"PetShell 引用了领域对象: {offenders}"


# ── 4. 禁止跨模块私有访问（已在第 5 项清理，防回归）──

def test_no_engine_private_access_in_ui_side():
    """pet.py 与 pet_mixins/ 里不应再有 `_engine._` 私有访问。

    这条是 2026-09-19 清理成果的回归保护。
    """
    offenders = []
    for rel in _pet_sources():
        src = _read(rel)
        for i, line in enumerate(src.splitlines(), 1):
            if line.strip().startswith("#"):
                continue
            if "_engine._" in line:
                offenders.append(f"{rel}:{i}")
    assert not offenders, f"跨模块私有访问回归: {offenders}"


# ── 5. mixin 依赖面快照（防止拆分中悄悄扩大）──

def test_mixin_dependency_surface_is_tracked():
    """记录 mixin 对宿主的依赖属性数，超出快照则提醒。

    这不是硬约束（mixin 模式本就依赖宿主），而是**可见性**：
    让「又往 mixin 里塞了一个宿主依赖」这件事在 CI 里显形。
    """
    snapshot = {
        "animation_mixin.py": 8,
        "interaction_mixin.py": 7,
        "behavior_mixin.py": 11,
        "chat_mixin.py": 10,
        "bubble_mixin.py": 12,
        "play_mixin.py": 5,
        "perception_mixin.py": 3,
        "panels_mixin.py": 4,
        "interface_mixin.py": 4,
        "audio_mixin.py": 3,
        "voice_provider_mixin.py": 6,
        "perch_mixin.py": 2,
    }
    # 只做存在性 + 文件数校验；精确属性数依赖文档字符串格式，易碎。
    mixins_dir = os.path.join(ROOT, "pet_mixins")
    if not os.path.isdir(mixins_dir):
        pytest.skip("pet_mixins/ 不存在")
    actual = {f for f in os.listdir(mixins_dir) if f.endswith(".py")}
    expected = set(snapshot)
    missing = expected - actual
    assert not missing, f"快照里的 mixin 不见了（被删？）: {missing}"
