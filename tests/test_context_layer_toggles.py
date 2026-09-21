# -*- coding: utf-8 -*-
"""语境注入层（氛围 / 生活游标）的**开关链路**单测。

背景（2026-09-21）：这两层原本只能手改 config.json。加进设置面板的「记忆」页时，
顺手把开关做成**热生效**（保存即生效），因此链路变长了一节：

    设置面板勾选 → c["atmosphere"]/["life_cursor"] → config_diff → write_merged
      → ConfigWatcher / 保存回调 → pet._apply_runtime_config
      → HOT_CONFIG_KEYS → appliers → _init_atmosphere_layer / _init_p1_life_cursor

链路越长越容易在某一段静默断掉（勾了没反应，或者更糟：**悄悄把配置写坏**）。
本文件盯住三件事：

1. **装卸表**：两个 key 既在 ``HOT_CONFIG_KEYS`` 里，也在 ``appliers`` 字典里
   （少一处就是"勾了保存，什么也没发生"）。
2. **可重复调用**：两个 init 都会被重复调用，必须幂等（life_cursor 尤其危险：
   不先停旧定时器就是每次保存多留一个 QTimer）。
3. **模板兜底**：两个 key 都得在 config.template.json 里有块——面板能写出去的键，
   模板里必须存在，否则用户配置里会凭空长出没人知道默认值的键。
"""
import copy
import json
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from pet_mixins.emotion_classify_mixin import EmotionClassifyMixin
from pet_mixins.perception_mixin import PerceptionMixin

ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


# ── 1. 装卸表 ────────────────────────────────────────────

def test_hot_config_keys_include_context_layers():
    src = _read("pet.py")
    m = re.search(r"HOT_CONFIG_KEYS\s*=\s*\(([^)]*)\)", src, re.S)
    assert m, "没找到 HOT_CONFIG_KEYS"
    keys = re.findall(r'"([^"]+)"', m.group(1))
    assert "atmosphere" in keys
    assert "life_cursor" in keys


def test_appliers_dict_wires_context_layers():
    src = _read("pet.py")
    assert '"atmosphere": self._init_atmosphere_layer' in src
    assert '"life_cursor": self._init_p1_life_cursor' in src


def test_applier_targets_exist_on_the_mixins():
    """装卸表里点名的两个方法必须真实存在——拼错名字会静默不生效。"""
    assert hasattr(EmotionClassifyMixin, "_init_atmosphere_layer")
    assert hasattr(PerceptionMixin, "_init_p1_life_cursor")


def test_startup_does_not_own_the_context_layers():
    """★ 启动路径不能自己再装一遍。

    否则启动时初始化两次（日志里同一行打两遍），而且两套路径各记各的账。
    这与仓库已有的 ``test_startup_no_longer_calls_inits_separately`` 是同一条纪律：
    **启动也只走 `_apply_runtime_config` 这一条路**。
    """
    assert "self._init_atmosphere_layer()" not in _read("pet_mixins/emotion_classify_mixin.py")
    assert "self._init_p1_life_cursor()" not in _read("pet_mixins/perception_mixin.py")


# ── 2. 可重复调用 ───────────────────────────────────────

def _fake_emotion(config):
    return SimpleNamespace(config=config, _engine=None, _life_cursor=None)


def test_atmosphere_layer_is_standalone_callable():
    """不依赖分类器初始化，只读 self.config。"""
    f = _fake_emotion({"atmosphere": {"enabled": False}})
    EmotionClassifyMixin._init_atmosphere_layer(f)
    assert f._atmosphere is not None
    assert f._atmosphere.enabled is False
    assert f._atmos_input is None


def test_atmosphere_layer_enabled_and_ratio_builds_input():
    f = _fake_emotion({"atmosphere": {"enabled": True, "source": "ratio"}})
    EmotionClassifyMixin._init_atmosphere_layer(f)
    assert f._atmosphere.enabled is True
    assert f._atmos_source == "ratio"
    assert f._atmos_input is not None, "结构族需要滚动分位输入"


def test_atmosphere_layer_rebuild_is_idempotent():
    f = _fake_emotion({"atmosphere": {"enabled": True, "source": "ratio"}})
    EmotionClassifyMixin._init_atmosphere_layer(f)
    first = f._atmosphere
    EmotionClassifyMixin._init_atmosphere_layer(f)
    assert f._atmosphere is not first, "重建应当是换一个新实例，而不是沿用旧状态"
    assert f._atmosphere.enabled is True


def test_atmosphere_layer_turning_off_clears_state():
    f = _fake_emotion({"atmosphere": {"enabled": True, "source": "ratio"}})
    EmotionClassifyMixin._init_atmosphere_layer(f)
    assert f._atmosphere.enabled is True
    f.config = {"atmosphere": {"enabled": False}}
    EmotionClassifyMixin._init_atmosphere_layer(f)
    assert f._atmosphere.enabled is False
    assert f._atmos_input is None, "关掉之后不该还留着输入缓冲在后台攒"


def test_atmosphere_layer_survives_garbage_config(caplog):
    """类型写错时不能抛，但也**不能一声不响**地当成没配——
    文件里有块、行为上没有，是最难查的那种故障。"""
    f = _fake_emotion({"atmosphere": "不是字典"})
    with caplog.at_level("WARNING"):
        EmotionClassifyMixin._init_atmosphere_layer(f)
    assert f._atmosphere is not None, "降级为「关闭」即可，但不该连状态对象都没有"
    assert f._atmosphere.enabled is False
    assert any("config.atmosphere" in r.message for r in caplog.records), \
        "类型不对必须留下 warning"


class _StopTracker:
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


def test_life_cursor_init_stops_previous_timer():
    """★ 重复调用必须先停旧定时器，否则每次保存设置都多一个 QTimer。"""
    tracker = _StopTracker()
    f = SimpleNamespace(config={}, _life_cursor_timer=tracker)
    PerceptionMixin._init_p1_life_cursor(f)
    assert tracker.stopped is True
    assert f._life_cursor is not None
    assert f._life_cursor_timer is None, "未启用时不该新建定时器"


def test_both_inits_report_true_for_the_ledger():
    """★ 返回 None 会被热重载当成失败 → 每次保存都重建一次（静默白干）。"""
    assert EmotionClassifyMixin._init_atmosphere_layer(
        _fake_emotion({"atmosphere": {"enabled": True, "source": "ratio"}})) is True
    assert EmotionClassifyMixin._init_atmosphere_layer(
        _fake_emotion({"atmosphere": {"enabled": False}})) is True
    f = SimpleNamespace(config={}, _life_cursor_timer=None)
    assert PerceptionMixin._init_p1_life_cursor(f) is True


def test_life_cursor_init_survives_broken_old_timer():
    class _Boom:
        def stop(self):
            raise RuntimeError("定时器已经没了")

    f = SimpleNamespace(config={}, _life_cursor_timer=_Boom())
    PerceptionMixin._init_p1_life_cursor(f)          # 不应抛出
    assert f._life_cursor_timer is None


# ── 3. 模板兜底 ─────────────────────────────────────────

@pytest.mark.parametrize("key", ["atmosphere", "life_cursor"])
def test_template_has_block_default_off(key):
    tpl = json.loads(_read("config.template.json"))
    assert key in tpl, f"面板能写出 {key}，模板里就得有它"
    assert tpl[key]["enabled"] is False, "这两层默认必须是关的（开着就是持续花钱）"


def test_template_blocks_keep_source_and_thresholds():
    """氛围层的族与阈值必须留在模板里——没了它们会退化成情绪族默认值。"""
    tpl = json.loads(_read("config.template.json"))
    atmo = tpl["atmosphere"]
    assert atmo.get("source") == "ratio"
    for k in ("theta_hi", "theta_lo", "beta", "half_life_hours"):
        assert k in atmo, f"模板缺 {k}"


def test_settings_panel_exposes_both_switches():
    src = _read("ui/settings_dialog.py")
    assert 'QCheckBox("启用氛围累积层（把最近的气氛攒成慢变量）")' in src
    assert 'QCheckBox("启用生活游标（最近在忙什么 → 【近况】一句）")' in src
    # 收集段：两个键都要被写进待保存的配置里
    assert 'c["atmosphere"] = blk' in src
    assert 'c["life_cursor"] = blk2' in src


# ── 4. 真把面板造出来（不是字符串检查）──────────────────

class _StubPM:
    def list_pets(self):
        return []

    def get_pet(self, *a, **k):
        return None


def _make_dialog(config):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from ui.settings_dialog import SettingsDialog

    QApplication.instance() or QApplication([])
    return SettingsDialog(config=copy.deepcopy(config), pet_manager=_StubPM())


def _ctx_config():
    return {
        "atmosphere": {"enabled": True, "source": "ratio", "theta_hi": 0.5,
                       "theta_lo": 0.4, "beta": 0.1, "half_life_hours": 3.0},
        "life_cursor": {"enabled": False, "window_hours": 6.0,
                        "interval_minutes": 60.0, "min_events": 5,
                        "max_events": 120},
        "dialog": {"agent_id": "ophelia"},
        "agents": [{"id": "miku", "enabled": True,
                    "position": {"x": 0, "y": 0}, "scale": 1.0,
                    "builtin": True}],
        "character": "miku",
    }


def test_panel_switch_reflects_config():
    dlg = _make_dialog(_ctx_config())
    assert dlg.atmo_enabled.isChecked() is True
    assert dlg.life_cursor_enabled.isChecked() is False


def test_panel_toggle_preserves_sibling_keys():
    """★ 开关只能改 `enabled`，**不能把 source / theta / half_life 吃掉**。

    真被吃掉的话不会报错：阈值静默退回情绪族默认值，迟滞带整个落空。
    这是本次改动里唯一一个"写得出来、也错得出来"的地方。
    """
    dlg = _make_dialog(_ctx_config())
    dlg.atmo_enabled.setChecked(False)
    dlg.life_cursor_enabled.setChecked(True)
    dlg._save()                       # 原地改 self._config（写盘在 _commit）
    out = dlg.get_config()
    assert out["atmosphere"]["enabled"] is False
    assert out["life_cursor"]["enabled"] is True
    assert out["atmosphere"]["source"] == "ratio", "兄弟键被吃了"
    assert out["atmosphere"]["theta_hi"] == 0.5
    assert out["atmosphere"]["half_life_hours"] == 3.0
    assert out["life_cursor"]["window_hours"] == 6.0


def test_toggle_shows_up_in_the_write_patch():
    """★ 开关必须进写盘补丁——不然用户勾了保存，等于没勾。

    写盘走 `config.config_diff(基线快照, 面板现状)`，只落"用户真动过的键"。
    这一步断了不会报任何错。
    """
    import config as cfg_mod

    base = _ctx_config()
    dlg = _make_dialog(base)
    dlg.life_cursor_enabled.setChecked(True)
    dlg._save()
    patch = cfg_mod.config_diff(base, dlg.get_config())
    assert "life_cursor" in patch, "开关没进补丁 → 保存等于没点"
    assert patch["life_cursor"].get("enabled") is True
    # 没动过的氛围层不该跟着一起落盘
    assert "atmosphere" not in patch
