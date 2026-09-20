# -*- coding: utf-8 -*-
"""回归：打包时不得把 Live2D 模型打进产物。

## 背景（2026-09-20）

`oc_pet.spec` 原来写 `datas = [('characters', 'characters')]`，把整个目录
打进去 —— 实测带出 **47.9MB** 模型：

    kurisu   2.5 MB
    miku    34.6 MB
    Rory    10.8 MB   ← 第三方模型

这与 README 的承诺直接矛盾：

> 本项目**不随仓库分发任何 Live2D 模型文件**
> （`characters/*/live2d/` 已在 `.gitignore` 中排除）

且 Rory 是**第三方模型**，分发出去有法律风险。

## 现在的做法

spec 里 `_collect_character_configs()` 按**文件后缀**过滤：
  ✓ 收：pet.json / manifest.json / model.json / profile.json / *.md
  ✗ 不收：.moc3 / .moc.json / *.model3.json / 贴图 / 物理 / 动作 / 备份

本测试**直接加载 spec 里的那份函数**（不复制逻辑），避免两处漂移
—— 上一版验证脚本就是复制了一份，改了 spec 却没同步。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]


def _load_spec_head():
    """exec oc_pet.spec 的前半段（到 Analysis 之前）。

    后半段会调 PyInstaller 的 EXE/COLLECT，测试里不能跑。
    """
    spec_path = _REPO / "oc_pet.spec"
    src = spec_path.read_text(encoding="utf-8")
    cut = src.index("a = Analysis(")
    ns = {"__name__": "oc_pet_spec"}
    exec(compile(src[:cut], str(spec_path), "exec"), ns)  # noqa: S102
    return ns


@pytest.fixture(scope="module")
def spec_ns():
    return _load_spec_head()


# ══════════════════════════════════════════════════════════════
#  1. 核心：模型素材不得被收集
# ══════════════════════════════════════════════════════════════

def test_no_model_files_collected(spec_ns):
    """★ 核心：收集到的文件里不得含任何模型素材/备份。"""
    exts = spec_ns["_MODEL_EXTS"]
    collect = spec_ns["_collect_character_configs"]

    os.chdir(_REPO)
    items = collect("characters")
    assert items, "characters/ 下应有配置文件（至少 miku/pet.json）"

    bad = [src for src, _ in items
           if any(src.lower().endswith(e) for e in exts)]
    assert not bad, f"模型素材混进打包列表: {bad}"


def test_dst_is_directory_not_file(spec_ns):
    """datas 的 dst 必须是**目录**，不能拼上文件名。

    2026-09-20 实测踩坑：把文件名拼进 dst 后产物变成
    `characters/miku/pet.json/pet.json`（多一层目录）。
    """
    collect = spec_ns["_collect_character_configs"]
    os.chdir(_REPO)
    for _, dst in collect("characters"):
        assert not dst.lower().endswith((".json", ".md", ".txt")), (
            f"dst 误拼了文件名（会多一层目录）: {dst}")


def test_model_exts_cover_known_assets(spec_ns):
    """后缀表要覆盖已知的模型文件类型。"""
    exts = spec_ns["_MODEL_EXTS"]
    for must in (".moc3", ".moc.json", ".model3.json", ".model.json",
                 ".physics3.json", ".cdi3.json", ".motion3.json",
                 ".png", ".jpg", ".bak", ".orig"):
        assert must in exts, f"后缀表缺 {must}"


def test_curated_size_is_small(spec_ns):
    """收集总量应远小于模型体积（模型 47.9MB，配置应为 KB 级）。"""
    collect = spec_ns["_collect_character_configs"]
    os.chdir(_REPO)
    items = collect("characters")
    total = sum(os.path.getsize(s) for s, _ in items if os.path.exists(s))
    assert total < 2 * 1024 * 1024, (
        f"收集到 {total/1024/1024:.1f}MB —— 疑似又混进了模型")


# ══════════════════════════════════════════════════════════════
#  2. 该保留的配置不能被误伤
# ══════════════════════════════════════════════════════════════

def test_keeps_character_configs(spec_ns):
    """角色配置（pet.json / profile.json）必须保留。

    `profile.json` 在 `live2d/` 目录下，但它**是配置不是模型**
    —— 随仓库分发（.gitignore 里 `!characters/*/live2d/profile.json`）。
    """
    collect = spec_ns["_collect_character_configs"]
    os.chdir(_REPO)
    kept = {src.replace("/", os.sep) for src, _ in collect("characters")}

    for want in (os.path.join("characters", "miku", "pet.json"),
                 os.path.join("characters", "miku", "live2d", "profile.json")):
        assert want in kept, f"应保留 {want}"


def test_skips_dot_suffixed_model_dirs(spec_ns):
    """形如 `Roxy_V1.8192` / `kurisu.2048` 的模型子目录应整体跳过。"""
    collect = spec_ns["_collect_character_configs"]
    os.chdir(_REPO)
    items = collect("characters")
    for _, dst in items:
        assert not any(
            part.split(".")[-1].isdigit() and len(part.split(".")[-1]) >= 3
            for part in dst.split(os.sep)
        ), f"模型子目录混入: {dst}"


# ══════════════════════════════════════════════════════════════
#  3. 源码级守卫
# ══════════════════════════════════════════════════════════════

def test_spec_does_not_bulk_copy_characters():
    """spec 不得再出现「整目录打包 characters」的**代码**写法。

    ⚠️ 只查代码行，跳过注释——我在 spec 里写了那句旧写法当**反例**，
    直接字符串搜会被自己的注释骗到（同一个坑：注释里的反例当代码）。
    """
    src = (_REPO / "oc_pet.spec").read_text(encoding="utf-8")
    code_lines = [ln for ln in src.splitlines()
                  if not ln.lstrip().startswith("#")]
    code = "\n".join(code_lines)
    assert "('characters', 'characters')" not in code, (
        "又退回整目录打包 —— 会带出 47.9MB 模型")


def test_spec_exe_is_onedir_not_onefile():
    """EXE 不得再拿 a.binaries / a.datas（那是 onefile 写法）。

    2026-09-20 实测：误用导致 exe 262MB（内容重复），修后 23.9MB。
    同样只查代码行（注释里引了旧写法当反例）。
    """
    src = (_REPO / "oc_pet.spec").read_text(encoding="utf-8")
    i = src.index("exe = EXE(")
    j = src.index("coll = COLLECT(")
    block = src[i:j]
    code_lines = [ln for ln in block.splitlines()
                  if not ln.lstrip().startswith("#")]
    code = "\n".join(code_lines)
    assert "exclude_binaries=True" in code, (
        "onedir 模式 EXE 应设 exclude_binaries=True")
    assert "a.binaries" not in code, (
        "EXE 不该拿 a.binaries（那是 onefile 写法，会让 exe 翻倍）")
    assert "a.datas" not in code, "EXE 不该拿 a.datas"
