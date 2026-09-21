"""回归锁定：配置写盘的**补丁语义**（2026-09-21）

## 三个真实事故（全部实测复现过）

1. **开关不持久**：PetManager 每次拖拽都把"启动时读到的整份快照"交给共享防抖器，
   而防抖器的旧语义是"最后提交者胜"——用户刚在设置面板关掉的开关被整体盖回
   启动值，退出时再落一次盘，重启后开关又是开着的。

2. **清不掉字段**：旧 `_deep_merge` 有一条"空值不覆盖"保护，导致**任何字段一旦
   有值就再也清不掉**。实测：清空麦克风设备 / MCP token / Skyrim dll 全都不生效。

3. **pop 等于没写**：合并式写盘分不清"没提到这个键"和"要把这个键删掉"，
   代码里 `cfg["screen"].pop("interval_min")` 表达不了删除——取消勾选"随机截屏
   间隔"后旧范围一直留在配置里。

## 现在的不变量

- **写盘**：只影响补丁里显式提到的键；提到空值就是清空；提到 ``DELETE`` 就是删除。
- **加载**：磁盘说了算，只补"磁盘里缺失"的键（空串是合法值，不再当成"未提供"）。
- **防抖器**：多次 ``schedule`` 的补丁**累积**（深合并），不是后者替换前者。
- **读失败**：目标文件存在但读不出来 → 放弃本次写入（绝不把整份配置写成只剩补丁）。

测试直接用 ``write_merged`` / ``_merge_over`` / ``config_diff`` 这些纯函数 + 临时
文件，绕开 conftest 对 ``config.save_config`` 的写盘守卫。
"""
from __future__ import annotations

import json

import pytest

import config as C


# ══════════════════════════════════════════════════════════════
#  合并语义：空值 / 删除 / 无关键
# ══════════════════════════════════════════════════════════════

def test_empty_value_clears_field():
    """清空字段必须生效（旧实现的"空值不覆盖"让这一步静默失败）。"""
    disk = {"asr": {"device": "mic-1", "provider": "whisper_local"}}
    merged = C._merge_over(disk, {"asr": {"device": ""}})

    assert merged["asr"]["device"] == ""
    assert merged["asr"]["provider"] == "whisper_local", "没提到的键必须原样保留"


def test_delete_sentinel_removes_key():
    """`DELETE` 是"显式删除"的唯一表达方式（dict.pop 表达不出来）。"""
    disk = {"screen": {"enabled": True, "interval_min": 60, "interval_max": 180}}
    merged = C._merge_over(disk, {"screen": {"interval_min": C.DELETE, "interval_max": C.DELETE}})

    assert merged["screen"] == {"enabled": True}


def test_delete_is_not_truthy():
    """哨兵不能被当成真值——否则 `if cfg.get(...)` 这类判断会走错分支。"""
    assert not C.DELETE
    assert repr(C.DELETE) == "<DELETE>"


def test_unmentioned_keys_are_untouched():
    """补丁只影响自己提到的键（这是"各写各的切片"能成立的前提）。"""
    disk = {"shortcuts": {"enabled": False}, "sfx": {"enabled": False}, "scale": 1.0}
    merged = C._merge_over(disk, {"scale": 1.5})

    assert merged["shortcuts"]["enabled"] is False
    assert merged["sfx"]["enabled"] is False
    assert merged["scale"] == 1.5


# ══════════════════════════════════════════════════════════════
#  Bug 1 原始场景：拖拽不得盖掉设置面板刚写的开关
# ══════════════════════════════════════════════════════════════

def test_drag_does_not_revert_toggles(tmp_path):
    """PetManager 只提交 `agents` 切片 → 用户改过的开关原样留在磁盘上。"""
    cfg_path = tmp_path / "config.json"
    disk = {
        "shortcuts": {"enabled": False},
        "screen": {"enabled": False},
        "sfx": {"enabled": False},
        "agents": [{"id": "miku", "enabled": True, "position": {"x": 100, "y": 100}}],
    }
    cfg_path.write_text(json.dumps(disk, ensure_ascii=False), encoding="utf-8")

    # update_agent_cfg 提交的就是这一片
    C.write_merged(str(cfg_path), {"agents": [{"id": "miku", "enabled": True, "position": {"x": 300, "y": 400}}]})

    after = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert after["shortcuts"]["enabled"] is False
    assert after["screen"]["enabled"] is False
    assert after["sfx"]["enabled"] is False
    assert after["agents"][0]["position"] == {"x": 300, "y": 400}


# ══════════════════════════════════════════════════════════════
#  防抖写盘器：补丁累积，而不是"最后提交者胜"
# ══════════════════════════════════════════════════════════════

def test_saver_accumulates_patches():
    """三个来源各交自己那一片 → 一份补丁里全都在（顺序无关）。"""
    saver = C._AsyncConfigSaver(debounce_ms=60000)  # 别真的起线程写盘
    saver.schedule({"scale": 1.2})
    saver.schedule({"agents": [{"id": "miku"}]})
    saver.schedule({"window": {"x": 7, "y": 8}})

    assert saver._pending == {
        "scale": 1.2,
        "agents": [{"id": "miku"}],
        "window": {"x": 7, "y": 8},
    }
    saver._stop = True


def test_saver_later_value_wins_within_same_key():
    """同一个键被提交两次 → 后提交的生效（累积是深合并，不是取并集）。"""
    saver = C._AsyncConfigSaver(debounce_ms=60000)
    saver.schedule({"scale": 1.0})
    saver.schedule({"scale": 1.4})

    assert saver._pending == {"scale": 1.4}
    saver._stop = True


# ══════════════════════════════════════════════════════════════
#  读失败保护：绝不把整份配置写成只剩补丁
# ══════════════════════════════════════════════════════════════

def test_write_aborts_when_disk_unreadable(tmp_path):
    """文件存在但读不出来 → 放弃写入，原文件一个字节都不动。

    合并式写盘最危险的失败模式是"读失败当空文件"——那会把用户的整份配置
    写成只剩补丁里那几个键。
    """
    cfg_path = tmp_path / "config.json"
    broken = "{ 这不是 json"
    cfg_path.write_text(broken, encoding="utf-8")

    with pytest.raises(RuntimeError):
        C.write_merged(str(cfg_path), {"scale": 2.0})

    assert cfg_path.read_text(encoding="utf-8") == broken


def test_write_creates_file_when_absent(tmp_path):
    """首次启动（文件不存在）要能正常落盘——这是引导路径，不能被保护挡住。"""
    cfg_path = tmp_path / "config.json"

    C.write_merged(str(cfg_path), {"character": "miku"})

    assert json.loads(cfg_path.read_text(encoding="utf-8")) == {"character": "miku"}


# ══════════════════════════════════════════════════════════════
#  config_diff：只挑"用户真动过的键"
# ══════════════════════════════════════════════════════════════

def _base() -> dict:
    return {"a": 1, "n": {"x": 1, "y": 2}, "gone": 5, "lst": [1, 2]}


def test_diff_no_change_returns_none():
    assert C.config_diff(_base(), _base()) is None


def test_diff_only_includes_changed_leaf():
    after = {"a": 1, "n": {"x": 9, "y": 2}, "gone": 5, "lst": [1, 2]}
    assert C.config_diff(_base(), after) == {"n": {"x": 9}}


def test_diff_removed_key_becomes_delete():
    after = {"a": 1, "n": {"x": 1, "y": 2}, "lst": [1, 2]}
    assert C.config_diff(_base(), after) == {"gone": C.DELETE}


def test_diff_list_and_new_subtree():
    after = {"a": 1, "n": {"x": 1, "y": 2}, "gone": 5, "lst": [1, 3], "new": {"k": 1}}
    assert C.config_diff(_base(), after) == {"lst": [1, 3], "new": {"k": 1}}


def test_diff_none_value_is_a_real_change():
    """None 是合法值，不能被当成"没变化"（内部用 _NoChange 标记区分）。"""
    assert C.config_diff({"k": "v"}, {"k": None}) == {"k": None}


def test_diff_then_merge_round_trip():
    """diff → _merge_over 必须能还原 after（删除用 DELETE 表达）。"""
    after = {"a": 2, "n": {"x": 1, "y": 9}, "lst": [1, 2]}
    patch = C.config_diff(_base(), after)

    assert C._merge_over(_base(), patch) == after


# ══════════════════════════════════════════════════════════════
#  load_config：磁盘说了算 / 默认值深拷贝
# ══════════════════════════════════════════════════════════════

def test_load_honours_disk_empty_string(monkeypatch, tmp_path):
    """磁盘上的空串是"用户清空了"，不能被默认值吃掉。"""
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"tts": {"edge_voice": ""}}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(C, "CONFIG_PATH", str(cfg_path))

    cfg = C.load_config()

    assert cfg["tts"]["edge_voice"] == ""
    assert cfg["tts"]["volume"] == C.DEFAULT_CONFIG["tts"]["volume"], "磁盘缺失的键仍要补默认"


def test_load_fills_missing_keys_from_defaults(monkeypatch, tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(C, "CONFIG_PATH", str(cfg_path))

    cfg = C.load_config()

    assert cfg["presence"]["interval_minutes"] == C.DEFAULT_CONFIG["presence"]["interval_minutes"]


def test_load_returns_isolated_defaults(monkeypatch, tmp_path):
    """默认值必须深拷贝：改一处嵌套默认值不得污染模块级 DEFAULT_CONFIG。"""
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(C, "CONFIG_PATH", str(cfg_path))

    cfg = C.load_config()
    cfg["proactive"]["rules"][0]["weight"] = 999

    assert C.DEFAULT_CONFIG["proactive"]["rules"][0]["weight"] != 999


def test_load_falls_back_on_broken_file(monkeypatch, tmp_path):
    """解析失败回退默认（而不是抛异常把启动带崩）。"""
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text("{ 半截", encoding="utf-8")
    monkeypatch.setattr(C, "CONFIG_PATH", str(cfg_path))

    cfg = C.load_config()

    assert cfg["scale"] == C.DEFAULT_CONFIG["scale"]
