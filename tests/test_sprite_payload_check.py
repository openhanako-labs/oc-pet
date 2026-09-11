"""回归锁定：资源存在性判断不得只看目录存在

2026-09-11 实测（跑全量测试后发现的连带问题）：

    characters/sample_live2d/live2d/ 是个**空目录**，
    但 `_has_sprites` 只做 `path.exists()` → 判为 True
    → 被当作有效角色启用 → 启动时无模型可加载。

    （时序上它是被测试写坏 config 时一并带出来的：
     测试把 sample_live2d 写成 enabled=True，而它恰好是空目录，
     于是暴露了「存在 ≠ 有内容」这个判据缺陷。）

不变量：
    **有资源 = 目录存在且有内容。** 空目录不算。
"""
from __future__ import annotations

import pytest

from pet_manager import PetManager


def _pm():
    return PetManager.__new__(PetManager)


# ══════════════════════════════════════════════════════════════
#  _dir_has_payload
# ══════════════════════════════════════════════════════════════

def test_empty_dir_has_no_payload(tmp_path):
    d = tmp_path / "empty"
    d.mkdir()

    assert PetManager._dir_has_payload(d) is False


def test_dir_with_file_has_payload(tmp_path):
    d = tmp_path / "full"
    d.mkdir()
    (d / "model.moc3").write_bytes(b"x")

    assert PetManager._dir_has_payload(d) is True


def test_dir_with_only_nested_empty_dir_counts_as_has_entries(tmp_path):
    """只含空子目录 → 算「有直接项」（`any(iterdir())` 的语义）。

    不深递归是有意为之：模型目录只多一层会很深，而真正要防的是
    「`live2d/` 完全空」这种（实测的真事故）。
    """
    d = tmp_path / "nested"
    (d / "sub").mkdir(parents=True)

    assert PetManager._dir_has_payload(d) is True


def test_nonexistent_dir_has_no_payload(tmp_path):
    assert PetManager._dir_has_payload(tmp_path / "nope") is False


# ══════════════════════════════════════════════════════════════
#  _has_sprites 端到端（用临时 characters 目录）
# ══════════════════════════════════════════════════════════════

@pytest.fixture
def fake_chars(tmp_path, monkeypatch):
    """把 CHARACTERS_DIR 指到临时目录，避免依赖真实 characters/。"""
    import pet_manager as pm_mod

    chars = tmp_path / "characters"
    chars.mkdir()
    monkeypatch.setattr(pm_mod, "CHARACTERS_DIR", chars, raising=False)
    return chars


def test_character_with_empty_live2d_dir_is_invalid(fake_chars):
    """复现实测：live2d/ 空目录 → 不算有资源。"""
    d = fake_chars / "sample_live2d" / "live2d"
    d.mkdir(parents=True)

    assert _pm()._has_sprites("sample_live2d") is False


def test_character_with_real_live2d_dir_is_valid(fake_chars):
    d = fake_chars / "miku" / "live2d"
    d.mkdir(parents=True)
    (d / "miku.moc3").write_bytes(b"x")

    assert _pm()._has_sprites("miku") is True


def test_character_with_pet_json_only_is_accepted(fake_chars):
    """只含 pet.json → 判为有效（**有意**：pet.json 可引用外部资源）。

    这条不是 bug——实现注释写明“可能配置了外部资源”。
    本次事故的假阳是**空 `live2d/` 目录**，不是 pet.json。
    """
    d = fake_chars / "ghost"
    d.mkdir()
    (d / "pet.json").write_text("{}", encoding="utf-8")

    assert _pm()._has_sprites("ghost") is True


def test_missing_character_dir_is_invalid(fake_chars):
    assert _pm()._has_sprites("nonexistent") is False


# ══════════════════════════════════════════════════════════════
#  真实仓库自检：只有一个角色资源完整
# ══════════════════════════════════════════════════════════════

def test_real_characters_dir_has_no_empty_model_dirs():
    """真实 characters/ 下不得存在「空 live2d/ 目录却被当有效」的角色。

    这是本次事故的直接证据：sample_live2d/live2d 是空目录。
    本测试把它钉住——若将来又出现空目录角色，说明有人误建。
    """
    import pet_manager as pm_mod

    chars = getattr(pm_mod, "CHARACTERS_DIR", None)
    if chars is None or not chars.is_dir():
        pytest.skip("characters/ 不存在")

    pm = _pm()
    empty_but_enabled = []
    for p in chars.iterdir():
        if not p.is_dir():
            continue
        l2d = p / "live2d"
        if l2d.is_dir() and not any(l2d.iterdir()) and pm._has_sprites(p.name):
            empty_but_enabled.append(p.name)

    assert not empty_but_enabled, (
        f"这些角色的 live2d/ 是空目录却被判为有资源: {empty_but_enabled}"
    )
