"""接线测试：backup_service CLI + startup_check 配置角色检查

覆盖 2026-09-10 接线：
- backup_service 此前零调用方，本次补 CLI 入口（list/backup/verify/restore/delete）
- startup_check 此前零调用方，本次接进 launcher 启动失败路径；
  并补 check_configured_character（原自检抓不到「配置指向的角色不存在」）
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core import startup_check as sc
from core.backup_service import BackupService, _cmd_list, _cmd_backup, _cmd_verify, _cmd_restore, main


# ══════════════════════════════════════════════════════════════
#  backup_service CLI
# ══════════════════════════════════════════════════════════════

@pytest.fixture
def svc(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "memory.json").write_text('{"k": 1}', encoding="utf-8")
    (data / "sub").mkdir()
    (data / "sub" / "deep.txt").write_text("deep", encoding="utf-8")
    return BackupService(data_dir=data, backup_dir=tmp_path / "backups")


def test_backup_then_list(svc, capsys):
    assert _cmd_backup(svc, "unit") == 0
    out = capsys.readouterr().out
    assert "完成" in out

    assert _cmd_list(svc) == 0
    out = capsys.readouterr().out
    assert "unit" in out


def test_list_without_backups_is_helpful(svc, capsys):
    assert _cmd_list(svc) == 0
    out = capsys.readouterr().out
    assert "还没有任何备份" in out
    assert "backup" in out


def test_backup_missing_source_returns_error(tmp_path, capsys):
    svc = BackupService(
        data_dir=tmp_path / "does-not-exist",
        backup_dir=tmp_path / "backups",
    )
    assert _cmd_backup(svc, "") == 1


def test_verify_roundtrip(svc, capsys):
    _cmd_backup(svc, "v")
    capsys.readouterr()
    latest = svc.get_latest_backup()
    assert latest is not None

    assert _cmd_verify(svc, latest["backup_id"]) == 0
    assert "校验通过" in capsys.readouterr().out


def test_verify_unknown_ref(svc, capsys):
    assert _cmd_verify(svc, "nope") == 1
    assert "找不到备份" in capsys.readouterr().out


# ── restore 是不可逆操作，必须显式确认 ─────────────────────────

def test_restore_requires_explicit_confirmation(svc, capsys):
    _cmd_backup(svc, "r")
    capsys.readouterr()
    bid = svc.get_latest_backup()["backup_id"]

    rc = _cmd_restore(svc, bid, confirmed=False)

    out = capsys.readouterr().out
    assert rc == 2, "未确认时必须拒绝执行"
    assert "不可逆" in out
    assert "--yes" in out


def test_restore_confirmed_succeeds(svc):
    _cmd_backup(svc, "r2")
    bid = svc.get_latest_backup()["backup_id"]

    assert _cmd_restore(svc, bid, confirmed=True) == 0


def test_restore_unknown_ref(svc, capsys):
    assert _cmd_restore(svc, "nope", confirmed=True) == 1
    assert "找不到备份" in capsys.readouterr().out


def test_backup_restore_true_roundtrip(tmp_path):
    """完整往返：备份 → 校验 → 改坏 → 恢复 → 原样。

    这是修 backup_service 三个 bug 的守门测试：
      1) manifest 从未写入 → verify 永远失败
      2) 归档名带 src_dir.name 前缀，manifest 存裸 rel_path → 对不上
      3) restore 把文件解到 target/<源目录名>/，不是原位
    只有真往返能同时卡住这三条。
    """
    data = tmp_path / "data"
    (data / "sub").mkdir(parents=True)
    original = {
        "memory.json": '{"remember": "美式咖啡"}',
        "sub/notes.txt": "重要笔记",
    }
    for rel, body in original.items():
        (data / rel).write_text(body, encoding="utf-8")

    svc = BackupService(data_dir=data, backup_dir=tmp_path / "backups")
    assert _cmd_backup(svc, "roundtrip") == 0
    bid = svc.get_latest_backup()["backup_id"]

    # 1) 备份可校验（原实现此步永远失败，因为没写 manifest）
    assert _cmd_verify(svc, bid) == 0

    # 2) 篡改数据
    (data / "memory.json").write_text("被改坏了", encoding="utf-8")
    (data / "sub" / "notes.txt").unlink()

    # 3) 恢复
    assert _cmd_restore(svc, bid, confirmed=True) == 0

    # 4) 原样回来了，且没有多出一层嵌套目录
    for rel, body in original.items():
        assert (data / rel).read_text(encoding="utf-8") == body, f"{rel} 未还原到原位"
    assert not (data / "data").exists(), "不得多出一层源目录名嵌套"
    # 5) 清单不得解压进数据目录
    assert not (data / "_manifest.json").exists(), "清单是元数据，不应落到数据目录"


# ── CLI 参数层 ─────────────────────────────────────────────────

def test_main_without_command_lists(monkeypatch, svc, capsys):
    monkeypatch.setattr("core.backup_service.get_backup_service", lambda: svc)
    assert main([]) == 0
    assert "备份" in capsys.readouterr().out


def test_main_restore_without_yes_refuses(monkeypatch, svc, capsys):
    monkeypatch.setattr("core.backup_service.get_backup_service", lambda: svc)
    _cmd_backup(svc, "cli")
    capsys.readouterr()
    bid = svc.get_latest_backup()["backup_id"]

    assert main(["restore", bid]) == 2
    assert "--yes" in capsys.readouterr().out


# ══════════════════════════════════════════════════════════════
#  startup_check.check_configured_character
# ══════════════════════════════════════════════════════════════

def _fake_root(tmp_path, config, packs):
    root = tmp_path / "proj"
    (root / "characters").mkdir(parents=True)
    for name in packs:
        d = root / "characters" / name
        d.mkdir()
        (d / "pet.json").write_text("{}", encoding="utf-8")
    if config is not None:
        (root / "config.json").write_text(
            json.dumps(config, ensure_ascii=False), encoding="utf-8"
        )
    return root


def test_missing_configured_character_is_detected(tmp_path, monkeypatch):
    """复现真实故障：config 指向 shizuku，但只有 miku。"""
    root = _fake_root(
        tmp_path,
        {"character": "shizuku", "character_package": "shizuku"},
        packs=["miku"],
    )
    monkeypatch.setattr(sc, "_project_root", lambda: root)

    result = sc.check_configured_character()

    assert result.ok is False
    assert "shizuku" in result.message
    assert "miku" in result.message, "应提示可用的角色"
    assert result.fix


def test_present_configured_character_passes(tmp_path, monkeypatch):
    root = _fake_root(tmp_path, {"character": "miku"}, packs=["miku"])
    monkeypatch.setattr(sc, "_project_root", lambda: root)

    result = sc.check_configured_character()

    assert result.ok is True


def test_no_config_is_not_a_failure(tmp_path, monkeypatch):
    """无 config.json = 首次启动，由引导选角色，不算错。"""
    root = _fake_root(tmp_path, None, packs=["miku"])
    monkeypatch.setattr(sc, "_project_root", lambda: root)

    assert sc.check_configured_character().ok is True


def test_broken_config_json_reports_parse_error(tmp_path, monkeypatch):
    root = _fake_root(tmp_path, None, packs=["miku"])
    (root / "config.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(sc, "_project_root", lambda: root)

    result = sc.check_configured_character()

    assert result.ok is False
    assert "解析失败" in result.message


def test_report_includes_configured_character_check():
    report = sc.run_startup_check()
    names = [r.name for r in report.results]
    assert "配置的角色存在？" in names
