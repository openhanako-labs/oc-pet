# -*- coding: utf-8 -*-
"""需求④A 回归：模型包与人设包解耦。

导入纯 Live2D 模型 zip(无 manifest)时,oc-pet 不应再伪造 identity.md /
awareness.md 占位人设——外观包与人格包被强行绑死是旧病灶。人设一律由
对应 Hanako agent 提供。REQUIRED_IDENTITY_FILES 只保留 model.json。
"""
import os
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from core.character_package import CharacterPackageManager, REQUIRED_IDENTITY_FILES


def test_required_identity_files_only_model_json():
    assert REQUIRED_IDENTITY_FILES == ["model.json"]


def test_live2d_import_creates_no_fake_persona(tmp_path):
    """导入纯 Live2D zip → 只生成 model.json + manifest.json,不伪造人设文件。"""
    src = tmp_path / "src"
    src.mkdir()
    (src / "Roxy.model3.json").write_text("{}", encoding="utf-8")
    zpath = tmp_path / "Roxy.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.write(src / "Roxy.model3.json", "Roxy.model3.json")

    installed = tmp_path / "installed"
    mgr = CharacterPackageManager(install_dir=installed)
    target = mgr.install_package(str(zpath), overwrite=True)

    files = set(os.listdir(target))
    assert "identity.md" not in files
    assert "awareness.md" not in files
    assert "model.json" in files
    assert "manifest.json" in files
