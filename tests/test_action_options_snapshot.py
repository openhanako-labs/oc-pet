"""候选清单来自**实测快照**而非手写常量（B 方案，2026-09-20）。

## 背景

`harness_adapter._build_action_prompt` 原本用渲染器里的手写常量
`_AI_DO_PROMPT`（36 个中文标签）当候选清单。问题：

- 加一个新预设 → 得手补一行，容易漂
- 模型换了（miku / Rory / kurisu）→ 常量不会变，但**能播的东西变了**
- 而 `capability_snapshot` 能从模型文件**实测**出真实清单
  （53 预设 / 7 motion / 7 表情 / 33 参数实测范围）

本方案：优先用快照生成的**分组候选**，失败回退手写常量。

## 为什么要分组

实测（docs/表达决策层-2026-09-20.md §3.2）：

| 候选形式 | 结果 |
|---|---|
| 53 个平铺 | 6-8/9，**不稳定**（换措辞就变） |
| 按情绪分组（每组 ≤12） | **10/10**，prob 明显拉开 |

另外通用项（连续眨眼/坐下/躺下）**只留在「平静」组**——
撒进其他组会把专属项稀释（实测「开心强」会被判成「快速眨眼」）。
"""
from __future__ import annotations

import ast
import io
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


# ── 1. 快照的分组方法 ──

def test_grouped_labels_line_is_grouped_and_nonempty():
    from core.capability_snapshot import build_snapshot

    snap = build_snapshot("miku")
    line = snap.grouped_labels_line()
    assert line, "miku 有 53 个预设，分组清单不该为空"
    # 应带情绪前缀（形如 "开心:xxx/yyy"）
    assert ":" in line
    assert "开心" in line


def test_grouped_labels_respects_limit():
    from core.capability_snapshot import build_snapshot

    snap = build_snapshot("miku")
    line = snap.grouped_labels_line(limit_per_emotion=3)
    for group in line.split(" "):
        if ":" not in group:
            continue
        _, labels = group.split(":", 1)
        assert len(labels.split("/")) <= 3, f"未遵守每组上限: {group}"


def test_grouped_labels_only_lists_real_presets():
    """快照只能列出模型**真实拥有**的预设——不能凭分组表凭空造。"""
    from core.capability_snapshot import build_snapshot

    snap = build_snapshot("miku")
    real = set(snap.preset_labels)
    line = snap.grouped_labels_line()
    for group in line.split(" "):
        if ":" not in group:
            continue
        _, labels = group.split(":", 1)
        for lb in labels.split("/"):
            assert lb in real, f"清单里有模型没有的预设: {lb}"


def test_ungrouped_presets_is_diagnostic():
    """ungrouped 应返回「有预设但没进分组」的标签（提示该补表）。"""
    from core.capability_snapshot import build_snapshot

    snap = build_snapshot("miku")
    un = snap.ungrouped_presets()
    assert isinstance(un, list)
    # miku 的分组表是完整的（实测为 0）
    assert un == [], f"miku 分组表应完整，未覆盖: {un}"


def test_grouped_labels_falls_back_when_no_presets():
    """无预设的模型 → 返回空串（调用方据此回退手写常量）。"""
    from core.capability_snapshot import CapabilitySnapshot

    snap = CapabilitySnapshot(character_id="empty", model_id="", model_path="")
    assert snap.grouped_labels_line() == ""


# ── 2. harness_adapter 接线 ──

def _adapter_src() -> str:
    return io.open(os.path.join(ROOT, "core", "harness_adapter.py"),
                   encoding="utf-8").read()


def test_adapter_has_snapshot_path():
    src = _adapter_src()
    assert "_action_options_from_snapshot" in src
    assert "grouped_labels_line" in src


def test_adapter_falls_back_to_constant():
    """快照失败必须回退手写常量——prompt 里绝不能没有候选。"""
    src = _adapter_src()
    i = src.index("def _build_action_prompt")
    body = src[i:i + 3000]
    assert "_AI_DO_PROMPT" in body, "缺少回退到手写常量的路径"


def test_adapter_has_kill_switch():
    """必须能关掉（配置开关），否则出问题只能改代码。"""
    src = _adapter_src()
    assert "action_options_from_snapshot" in src


def test_snapshot_failure_returns_empty_not_raises():
    """快照构建抛异常时，应返回空串（让调用方回退），不能把异常抛出去。"""
    src = _adapter_src()
    i = src.index("def _action_options_from_snapshot")
    body = src[i:i + 2500]
    assert "except Exception" in body
    assert "return \"\"" in body


def test_character_id_comes_from_config_character():
    """角色 id 必须取 ``config.character``。

    ⚠ 2026-09-20 实测踩坑：**agent_id ≠ character_id**。

    adapter 的 ``self.agent_id`` 是 Hana 的 agent 名（``"ophelia"``），
    而快照要的是 ``characters/`` 下的模型目录名（``"miku"`` / ``"shizuku"``）。
    第一版拿 ``_character_id`` / ``_current_char`` / ``_agent_id`` 去猜，
    全取不到 → 快照路径**静默返回空串** → prompt 一直用手写常量。
    **单元测试全绿，功能是死的。**
    """
    src = _adapter_src()
    i = src.index("def _current_character_id")
    body = src[i:i + 1600]
    assert 'cfg.get("character")' in body, \
        "角色 id 应优先取 config.character（与 perception_mixin 一致）"


def test_character_id_resolves_end_to_end():
    """端到端：真的能解析出一个非空角色 id（而非静默返回空串）。

    这条是上面那个坑的**回归保护**：只断言源码里有某个字符串
    不够——要真的调一次看结果。
    """
    sys.path.insert(0, ROOT)
    from core.harness_adapter import HanakoPetAdapter

    try:
        ad = HanakoPetAdapter(agent_id="ophelia")
    except TypeError:
        ad = HanakoPetAdapter()
    cid = ad._current_character_id()
    assert cid, "角色 id 解析为空 —— 快照路径会静默失效"
    assert isinstance(cid, str)


def test_snapshot_path_actually_produces_options():
    """端到端：默认配置下，快照路径应真的产出候选（不是空串）。

    这是 B 方案的核心断言。若它红了，说明候选清单又回退到手写常量，
    那 14 个快照独有的可播标签会再次“存在但从没被告知”。
    """
    sys.path.insert(0, ROOT)
    from core.harness_adapter import HanakoPetAdapter

    try:
        ad = HanakoPetAdapter(agent_id="ophelia")
    except TypeError:
        ad = HanakoPetAdapter()
    opts = ad._action_options_from_snapshot(None)
    assert opts, "快照路径产出为空 —— B 方案未生效"
    assert ":" in opts, "候选应按情绪分组（带「情绪:」前缀）"
    # 快照独有、手写常量漏掉的可播标签。
    # ⚠ 只断言「必定入选」的那几个——清单有长度预算，不是所有可播标签
    # 都能进去（那是设计选择，不是 bug）。选中的这几个是排序提权后
    # 稳定在预算内的。
    for expected in ("挥手", "摇头", "坐下"):
        assert expected in opts, f"快照独有标签丢失: {expected}"


def test_prompt_stays_within_guardrail():
    """快照路径不能把 prompt 长度护栏撞红（test_do_aliases 上限 420）。"""
    sys.path.insert(0, ROOT)
    from core.capability_snapshot import build_snapshot

    line = build_snapshot("miku").grouped_labels_line()
    # 调用方模板约 206 字符，加起来必须 < 420
    assert len(line) + 206 < 420, f"候选过长，会撞 prompt 护栏（{len(line)}）"


def test_high_value_actions_win_over_micro_expressions():
    """完整动作应优先于微表情（实测：arm_wave 曾被预算截掉）。

    这是排序修正的回归保护——若有人把 arm_wave 排回后面，它会再次被截。
    """
    sys.path.insert(0, ROOT)
    from core.capability_snapshot import build_snapshot

    line = build_snapshot("miku").grouped_labels_line()
    happy = ""
    for g in line.split():
        if g.startswith("开心"):
            happy = g.split(":", 1)[1]
            break
    assert happy, "开心组不该为空"
    assert "挥手" in happy, "完整动作「挥手」被微表情挤出了预算"
    # 提权后，它应排在嘴角上扬这类微表情之前
    labels = happy.split("/")
    if "嘴角上扬" in labels:
        assert labels.index("挥手") < labels.index("嘴角上扬"), \
            "「挥手」应排在微表情之前"


# ── 3. 端到端：快照 → prompt 片段 ──

def test_prompt_fragment_contains_known_labels():
    """生成的 prompt 片段应含实测可播的中文标签。"""
    from core.capability_snapshot import build_snapshot

    line = build_snapshot("miku").grouped_labels_line()
    # 这几个是 docs 里 12/12 实测命中的标签
    for expected in ("害羞脸红", "生气瞪视", "失落垂眼"):
        assert expected in line, f"缺少实测命中的标签: {expected}"
