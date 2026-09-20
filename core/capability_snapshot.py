"""能力快照 —— 从模型文件动态生成「这个模型能做什么」的目录。

## 它治什么病

`live2d_renderer.py` 里有一段实测注释：把 53 个预设名 + 13 个 motion 名（共 66 个）
挤成一行塞进 prompt，结果 `[do:]` **全历史 0 次使用**——模型没法从
`angry_glare/arm_wave/blink3/...` 这种裸名字里选。

根因不是模型笨，是**候选没带语义、没分类、没边界**。任何「让 AI 选」的设计
都缺一份**带描述的候选清单**——这份清单就是本模块。

## 它产出什么

    snap = build_snapshot("miku")
    snap.motions      # [Motion(name, file, duration, loop, params_touched), ...]
    snap.expressions  # [Expression(name, file, params), ...]
    snap.presets      # [Preset(name, steps, params), ...]  ← 53 个参数序列预设
    snap.param_ranges # {param_id: (lo, hi)}  ← 从 motion/expression 曲线实测

## 参数范围从哪来（重要）

`.cdi3.json` **只有 Id / GroupId / Name，没有 min/max**（实测 141 参数全无范围）。
所以范围必须从**模型自己的 motion3.json / exp3.json 曲线**里量——
那是建模师实际用过的取值范围，比猜的准。

## 用法

    from core.capability_snapshot import build_snapshot
    snap = build_snapshot("miku")
    print(snap.summary_line())     # 一行概况，进日志
    print(snap.describe())         # 多行描述，给人看 / 给 LLM 看
"""
from __future__ import annotations

import glob
import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

# 版权/水印类表情，不进候选（会被误播成作者 LOGO）
_IGNORED_EXPRESSION_KEYWORDS = ("水印", "watermark", "版权", "author", "credit", "logo")


# ── 预设 → 中文语义标签 ────────────────────────────────────
#
# 为什么需要这层：预设名是英文下划线风格（blush_shy / eye_roll），实测把它们
# 直接当候选给本地引擎，分辨力明显不足——「困」被判成 giggle、「平静」被判成
# giggle，都是因为模型抓不住这些裸名字的语义。
#
# 换成中文语义标签后（RTX 3060 / Qwen2.5-1.5B-RLCD，53 个候选、temp=0.2）：
#   开心强→灿烂笑容 害羞强→害羞脸红 失落中→失落垂眼 思考轻→思考
#   惊讶强→惊讶吸气 困强→犯困 生气中→生气瞪视
# 8/9 命中，耗时 ~370ms。**候选必须带人话**，这是实测结论。
#
# 未收录的预设回退到预设名本身（仍可用，只是分辨力弱一些）。
PRESET_LABELS: dict[str, str] = {
    "blink3": "连续眨眼",
    "wink": "俏皮眨眼",
    "blink_slow": "缓慢眨眼",
    "blush": "脸红",
    "blush_shy": "害羞脸红",
    "blush_deny": "红着脸摇头否认",
    "gaze_shift": "眼神游移",
    "gaze_up": "抬头往上看",
    "gaze_down": "低头往下看",
    "gaze_side": "侧目",
    "sneak_peek": "偷瞄",
    "smile_soft": "温柔微笑",
    "smile_bright": "灿烂笑容",
    "pout": "撅嘴",
    "angry_glare": "生气瞪视",
    "sad_droop": "失落垂眼",
    "surprise_gasp": "惊讶吸气",
    "think_look": "思考",
    "doubt": "疑惑歪头",
    "sigh": "叹气",
    "yawn": "打哈欠",
    "excited": "兴奋",
    "grin": "咧嘴大笑",
    "shy": "羞怯",
    "proud": "得意",
    "sleepy": "犯困",
    "nod": "点头",
    "head_shake": "摇头",
    "head_tilt": "歪头",
    "giggle": "咯咯笑",
    "sneeze": "打喷嚏",
    "stretch_yawn": "伸懒腰打哈欠",
    "stretch": "伸展",
    "dance": "摇摆起舞",
    "sit": "坐下",
    "lie": "躺下",
    "blink_quick": "快速眨眼",
    "eye_twinkle": "眼睛发亮",
    "brow_raise": "挑眉",
    "lip_pucker": "嘟嘴",
    "head_bob": "点头晃动",
    "head_roll": "转脖子",
    "head_tilt_left": "向左歪头",
    "head_tilt_right": "向右歪头",
    "body_sway": "身体轻晃",
    "shoulder_shrug": "耸肩",
    "arm_wave": "挥手",
    "mouth_smile": "嘴角上扬",
    "mouth_pursed": "抿嘴",
    "tongue_out": "吐舌头",
    "eye_roll": "翻白眼",
    "eye_wide": "瞪大眼睛",
    "eye_narrow": "眯起眼睛",
}

# 中文标签 → 预设名（反向查表，自动构建）
LABEL_TO_PRESET: dict[str, str] = {v: k for k, v in PRESET_LABELS.items()}


# ── 预设 → 适用情绪标签 ────────────────────────────────────
#
# **两阶段决策的关键**。实测（RTX 3060 / Qwen2.5-1.5B-RLCD）：
#
# | 候选规模 | 结果 |
# |---|---|
# | 53 个全给 | 6-8/9 命中，且**极不稳定**——“惊讶”时而判成“打哈欠” |
# | 按情绪分组后（每组 6-9 个） | **10/10 命中**，且 prob 拉开差距 |
#
# 原因：候选集越小，每个候选的语义空间越干净。53 选 1 时模型会被
# “打哈欠/低头”这类泛用项吸走；缩到情绪相关的一组，它就能专注分辨细节。
#
# 所以流程是：**主 LLM 定情绪 → 本表筛出候选 → 引擎在小组里挑细节动作**。
# 情绪理解交给主 LLM（它本来就在读对话），引擎只做“同情绪内的精细选择”。
#
# ⚠️ 建表原则（实测踩过）：**只放该情绪特有的、有表现力的动作**。
# 第一版把 blink3/blink_quick/sit 这类通用项也撒进了开心组，结果
# “开心强”被判成“快速眨眼”（因为通用项数量多、语义平淡，把专属项稀释了）。
# 通用眨眼/坐卧类只留在「平静」组。
PRESET_EMOTIONS: dict[str, tuple[str, ...]] = {
    # ── 开心 ──（表现力排序：笑 > 完整动作 > 眼部 > 头部/身体）
    #
    # 2026-09-20 排序修正：`arm_wave`（挥手）与 `dance`（摇摆起舞）从原位提到
    # 前面。理由：分组清单有长度预算（每组只列前 N 个），而**完整动作比微表情
    # 表现力更强、也更适合桌宠**。实测踩坑：原排序下 arm_wave 排第 12 位，
    # 在预算内**被截掉**——它是可播的，却从未进过 prompt。
    "smile_bright": ("开心",),
    "grin": ("开心",),
    "giggle": ("开心",),
    "excited": ("开心",),
    "arm_wave": ("开心",),          # ← 提权：完整动作
    "dance": ("开心",),             # ← 提权：完整动作
    "eye_twinkle": ("开心",),
    "mouth_smile": ("开心",),
    "proud": ("开心",),
    "wink": ("开心", "害羞"),
    "brow_raise": ("开心", "惊讶"),
    "tongue_out": ("开心", "害羞"),
    "head_bob": ("开心",),
    # ── 害羞 ──
    "blush_shy": ("害羞",),
    "blush_deny": ("害羞",),
    "shy": ("害羞",),
    "blush": ("害羞",),
    "sneak_peek": ("害羞",),
    "lip_pucker": ("害羞",),
    "head_tilt_left": ("害羞",),
    "head_tilt_right": ("害羞",),
    "gaze_shift": ("害羞",),
    "pout": ("害羞",),
    # ── 惊讶 ──
    "surprise_gasp": ("惊讶",),
    "eye_wide": ("惊讶",),
    "sneeze": ("惊讶",),
    "body_sway": ("惊讶",),
    # ── 思考 ──
    "think_look": ("思考",),
    "gaze_up": ("思考",),
    "sigh": ("思考", "失落"),
    "mouth_pursed": ("思考",),
    # ── 疑惑 ──
    "doubt": ("疑惑",),
    "head_tilt": ("疑惑",),
    "gaze_side": ("疑惑",),
    "shoulder_shrug": ("疑惑",),
    "head_shake": ("疑惑",),
    # ── 生气 ──
    "angry_glare": ("生气",),
    "eye_roll": ("生气",),
    "eye_narrow": ("生气",),
    # ── 失落 ──
    "sad_droop": ("失落",),
    "gaze_down": ("失落",),
    "blink_slow": ("失落", "困"),
    # ── 困 ──
    "sleepy": ("困",),
    "yawn": ("困",),
    "stretch_yawn": ("困",),
    "stretch": ("困",),
    "lie": ("困",),
    # ── 平静（通用项只在这里）──
    "smile_soft": ("平静",),
    "blink3": ("平静",),
    "blink_quick": ("平静",),
    "nod": ("平静",),
    "sit": ("平静",),
    "head_roll": ("平静",),
}

# 引擎单字段上限（超过就要拆多字段或分批）
MAX_CHOICES_PER_FIELD = 255

# 每个情绪最多给引擎多少个候选（太多会稀释分辨力，见 PRESET_EMOTIONS 注释）
MAX_CHOICES_PER_EMOTION = 12


def presets_for_emotion(snapshot: "CapabilitySnapshot", emotion: str,
                        limit: int = MAX_CHOICES_PER_EMOTION) -> list["Preset"]:
    """筛出适用于某情绪的预设（按标签匹配；无匹配则返回全部）。

    Args:
        snapshot: 能力快照。
        emotion: 中文情绪词，如 ``"开心"``。
        limit: 最多返回多少个。

    Returns:
        预设列表（保序，最多 ``limit`` 个）。**永远不返回空**——
        该情绪没有专属预设时，回退到全部预设，保证调用方总有候选可用。
    """
    hit = [p for p in snapshot.presets if emotion in PRESET_EMOTIONS.get(p.name, ())]
    if not hit:
        logger.debug("capability_snapshot: 情绪 %r 无专属预设，回退到全部", emotion)
        hit = list(snapshot.presets)
    return hit[:limit]


def preset_label(name: str) -> str:
    """预设名 → 中文语义标签（未收录则返回原名）。"""
    return PRESET_LABELS.get(name, name)


# ── 数据模型 ──────────────────────────────────────────────


@dataclass(frozen=True)
class Motion:
    """一个 motion 动作文件。"""

    name: str              # 文件名去扩展名，如 "waving"
    file: str              # 相对模型目录的路径
    duration: float = 0.0  # 秒（来自 Meta.Duration）
    loop: bool = False
    fade_in: float = 0.3
    fade_out: float = 0.3
    params: tuple[str, ...] = ()   # 该动作驱动的参数 id

    @property
    def is_idle(self) -> bool:
        return self.loop or self.name.lower() in ("idle", "待机")


@dataclass(frozen=True)
class Expression:
    """一个 .exp3.json 表情文件。"""

    name: str              # model3.json 里的 Name（可能含中文）
    file: str
    params: tuple[str, ...] = ()


@dataclass(frozen=True)
class Preset:
    """一个参数序列预设（来自 avatar/emote_presets.py）。

    Live2D 用参数驱动（set/clear/blink 步骤），不依赖 motion 文件——
    所以**即使模型没有 motion 文件，预设仍然可用**（Rory 就是这种情况）。
    """

    name: str
    steps: tuple[dict, ...] = ()
    params: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        """中文语义标签（给决策引擎当候选用；见 :data:`PRESET_LABELS`）。"""
        return preset_label(self.name)

    @property
    def duration(self) -> float:
        total = 0.0
        for s in self.steps:
            if s.get("type") == "blink":
                total += float(s.get("interval", 0.6)) * int(s.get("times", 1))
            else:
                total += float(s.get("duration", 0.0))
        return round(total, 2)


@dataclass
class CapabilitySnapshot:
    """一个角色的完整能力目录。"""

    character_id: str
    model_id: str = ""
    model_path: str = ""
    motions: list[Motion] = field(default_factory=list)
    expressions: list[Expression] = field(default_factory=list)
    presets: list[Preset] = field(default_factory=list)
    param_ranges: dict[str, tuple[float, float]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    # ── 查询 ──────────────────────────────────────────────

    @property
    def motion_names(self) -> list[str]:
        return [m.name for m in self.motions]

    @property
    def expression_names(self) -> list[str]:
        return [e.name for e in self.expressions]

    @property
    def preset_names(self) -> list[str]:
        return [p.name for p in self.presets]

    @property
    def preset_labels(self) -> list[str]:
        """预设的中文语义标签列表（决策候选用）。"""
        return [p.label for p in self.presets]

    def preset_by_label(self, label: str) -> Optional["Preset"]:
        """按中文标签找预设（引擎返回的就是标签）。"""
        for p in self.presets:
            if p.label == label or p.name == label:
                return p
        return None

    @property
    def all_action_names(self) -> list[str]:
        """全部可播动作名（预设优先，motion 补充，去重保序）。"""
        seen: dict[str, None] = {}
        for n in self.preset_names + self.motion_names:
            seen.setdefault(n, None)
        return list(seen)

    def has_motions(self) -> bool:
        return bool(self.motions)

    def param_range(self, param_id: str, default: tuple[float, float] = (-1.0, 1.0)) -> tuple[float, float]:
        return self.param_ranges.get(param_id, default)

    # ── 展示 ──────────────────────────────────────────────

    def summary_line(self) -> str:
        return (
            f"[capability] {self.character_id}: "
            f"{len(self.presets)} presets / {len(self.motions)} motions / "
            f"{len(self.expressions)} expressions / {len(self.param_ranges)} param-ranges"
            + (f" | ⚠ {'; '.join(self.warnings)}" if self.warnings else "")
        )

    def describe(self) -> str:
        lines = [self.summary_line()]
        if self.motions:
            lines.append("  motions: " + ", ".join(
                f"{m.name}({m.duration:g}s{',loop' if m.loop else ''})" for m in self.motions))
        else:
            lines.append("  motions: (无 —— 该模型只有参数，动作走预设路径)")
        if self.expressions:
            lines.append("  expressions: " + ", ".join(self.expression_names))
        lines.append(f"  presets: {len(self.presets)} 个（{', '.join(self.preset_names[:8])}...）")
        return "\n".join(lines)

    def labels_line(self) -> str:
        """候选标签一览（给决策 schema 用，单行）。"""
        return "/".join(self.preset_labels)

    def grouped_labels_line(self, limit_per_emotion: int = 12,
                            max_chars: int = 420) -> str:
        """按情绪分组的候选标签（给主 LLM 的 prompt 用）。

        与 ``labels_line()`` 的区别：那是**平铺 53 个**，给本地引擎的结构化
        schema 用（它能处理长列表）；这个是**分组且带情绪前缀**，给主 LLM 看。

        为何要分组（实测，见 docs/表达决策层-2026-09-20.md §3.2）：

        - 53 个平铺 → 6-8/9，且**不稳定**（换措辞就变）
        - 按情绪分组（每组 ≤ 12）→ **10/10**，prob 明显拉开

        另外通用项（连续眨眼/坐下/躺下）**只留在「平静」组**——
        把它们撒进其他组会把专属项稀释掉（实测：「开心强」会被判成「快速眨眼」）。

        本方法的数据源是 ``PRESET_EMOTIONS``（手写但经过实测校验的分组表），
        **只输出模型真实拥有的预设**——模型没有的不列。

        Args:
            limit_per_emotion: 每组最多列几个。默认 **12**，与实测结论一致
                （``MAX_CHOICES_PER_EMOTION = 12``，见 docs/表达决策层-2026-09-20.md
                §3.2：每组 ≤12 时 10/10，再大就开始稀释）。
                实测踩坑：第一版默认写 8，把开心组的 13 个砍到 8 个——
                **「挥手」直接被截掉了**，而它是可播的。
            max_chars: 本片段的长度预算（默认 420，含调用方的模板文字）。
                超出时**自动收窄每组数量**，而不是把 prompt 护栏撞红。
                实测结论是「决定性因素不是长度，是分组」，所以宁可每组
                少列几个，也要保住分组结构。

        Returns:
            形如 ``"开心:灿烂笑容/咧嘴大笑 害羞:害羞脸红/羞怯 ..."`` 的单行。
            模型没有任何预设时返回空串。
        """
        if not self.presets:
            return ""
        # 情绪 → 该情绪下**实际存在**的标签（保持 PRESET_EMOTIONS 的定义顺序）
        by_emotion: dict[str, list[str]] = {}
        label_of = {p.name: p.label for p in self.presets}
        for name, emotions in PRESET_EMOTIONS.items():
            label = label_of.get(name)
            if label is None:          # 模型没有这个预设 → 不列
                continue
            for emo in emotions:
                by_emotion.setdefault(emo, []).append(label)
        if not by_emotion:
            # 分组表一个都对不上（新模型/预设全改名）→ 回退平铺
            return "/".join(self.preset_labels)
        def _render(lim: int) -> str:
            parts = []
            for emo, labels in by_emotion.items():
                # 去重（一个预设可能同时属于多个情绪，同组内不会重复，但保险）
                seen = []
                for lb in labels:
                    if lb not in seen:
                        seen.append(lb)
                if not seen:
                    continue
                parts.append(f"{emo}:" + "/".join(seen[:lim]))
            return " ".join(parts)

        # ── 预算内取最大覆盖（2026-09-20）──
        # 目标不是「尽量短」，而是「在护栏内尽量多列」——
        # tests/test_do_aliases.py 的护栏注释自己写了：
        #   「限制选项数本身不是目的，可读性与覆盖度的平衡才是」
        # 所以从上限**逐级往上试**，取能装下的最大一组，
        # 而不是从大往下砍（那会把高价值动作提前砍掉）。
        budget = max(40, int(max_chars) - 206)   # 206 ≈ 调用方模板文字
        best = _render(2)
        for lim in range(3, int(limit_per_emotion) + 1):
            cand = _render(lim)
            if len(cand) <= budget:
                best = cand
            else:
                break
        return best

    def ungrouped_presets(self) -> list[str]:
        """有预设但未进任何情绪分组的标签（诊断用：提示该补 PRESET_EMOTIONS）。"""
        grouped = set(PRESET_EMOTIONS)
        return [p.label for p in self.presets if p.name not in grouped]


# ── 曲线值解析 ────────────────────────────────────────────


def _curve_values(segments: list) -> list[float]:
    """解析 motion3.json 的 Segments 数组，返回全部控制点值。

    格式：``[t0, v0, type, t1, (c1, c2)?, v1, ...]``
    - type 0 = linear，1 = stepped → 一个值
    - type 2 = bezier → c1, c2, v（取 v）
    - type 3 = stepped-with-... → 保守跳过
    """
    if len(segments) < 2:
        return []
    vals = [segments[1]]
    k = 2
    while k < len(segments):
        typ = segments[k]
        k += 1
        if k >= len(segments):
            break
        k += 1  # skip timestamp
        if typ == 2:                      # bezier: c1, c2, v
            if k + 2 < len(segments):
                vals.append(segments[k + 2])
            k += 3
        elif typ in (0, 1):               # linear / stepped: v
            if k < len(segments):
                vals.append(segments[k])
            k += 1
        else:
            break
    return [v for v in vals if isinstance(v, (int, float))]


def _accumulate_range(rng: dict, param_id: str, value: float) -> None:
    lo, hi = rng.get(param_id, (float("inf"), float("-inf")))
    rng[param_id] = (min(lo, value), max(hi, value))


# ── 扫描 ──────────────────────────────────────────────────


def _scan_motions(model_dir: str) -> tuple[list[Motion], dict]:
    """扫描 motion3.json 文件，返回 (motions, 参数范围累积)。"""
    motions: list[Motion] = []
    rng: dict[str, tuple[float, float]] = {}
    for path in sorted(glob.glob(os.path.join(model_dir, "motions", "*.motion3.json"))
                       + glob.glob(os.path.join(model_dir, "**", "*.motion3.json"), recursive=True)):
        if any(m.file == os.path.relpath(path, model_dir).replace("\\", "/") for m in motions):
            continue
        try:
            data = json.load(open(path, encoding="utf-8"))
        except Exception as e:
            logger.debug("capability_snapshot: motion 解析失败 %s: %s", path, e)
            continue
        meta = data.get("Meta") or {}
        params: list[str] = []
        for c in data.get("Curves") or []:
            pid = c.get("Id")
            if not pid:
                continue
            if pid not in params:
                params.append(pid)
            for v in _curve_values(c.get("Segments") or []):
                _accumulate_range(rng, pid, v)
        motions.append(Motion(
            name=os.path.basename(path).replace(".motion3.json", ""),
            file=os.path.relpath(path, model_dir).replace("\\", "/"),
            duration=float(meta.get("Duration") or 0.0),
            loop=bool(meta.get("Loop")),
            fade_in=float(meta.get("FadeInTime") or 0.3),
            fade_out=float(meta.get("FadeOutTime") or 0.3),
            params=tuple(params),
        ))
    return motions, rng


def _scan_expressions(model_dir: str) -> tuple[list[Expression], dict]:
    """扫描 exp3.json，返回 (expressions, 参数范围累积)。"""
    exprs: list[Expression] = []
    rng: dict[str, tuple[float, float]] = {}
    for path in sorted(glob.glob(os.path.join(model_dir, "expressions", "*.exp3.json"))):
        try:
            data = json.load(open(path, encoding="utf-8"))
        except Exception as e:
            logger.debug("capability_snapshot: expression 解析失败 %s: %s", path, e)
            continue
        params: list[str] = []
        for x in data.get("Parameters") or []:
            pid, val = x.get("Id"), x.get("Value")
            if not pid:
                continue
            if pid not in params:
                params.append(pid)
            if isinstance(val, (int, float)):
                _accumulate_range(rng, pid, float(val))
        name = os.path.basename(path).replace(".exp3.json", "")
        if any(k in name.lower() for k in _IGNORED_EXPRESSION_KEYWORDS):
            continue
        exprs.append(Expression(name=name, file=os.path.relpath(path, model_dir).replace("\\", "/"),
                                params=tuple(params)))
    return exprs, rng


def _read_model3_names(model_path: str) -> dict:
    """从 model3.json 读 Expressions 的 Name（中文名在这里，exp3 文件名可能是拼音）。"""
    try:
        data = json.load(open(model_path, encoding="utf-8"))
    except Exception:
        return {}
    out = {}
    for e in (data.get("FileReferences", {}).get("Expressions") or []):
        f = (e.get("File") or "").replace("\\", "/")
        nm = e.get("Name") or os.path.splitext(os.path.basename(f))[0]
        if f:
            out[os.path.basename(f)] = nm
    return out


def _scan_presets(oc_pet_root: str) -> list[Preset]:
    """从 avatar/emote_presets.py 读 LIVE2D_PRESETS（不 import，避免拉起 Qt）。"""
    path = os.path.join(oc_pet_root, "avatar", "emote_presets.py")
    if not os.path.exists(path):
        return []
    try:
        src = open(path, encoding="utf-8").read()
    except Exception as e:
        logger.debug("capability_snapshot: 预设文件读取失败: %s", e)
        return []

    block = src.split("LIVE2D_PRESETS", 1)
    if len(block) < 2:
        return []
    body = block[1].split("SPRITE_PRESET_MAP", 1)[0]

    presets: list[Preset] = []
    # 匹配 `    "name": [` 到下一个同级 key 或块尾
    for m in re.finditer(r'^    "([a-z_0-9]+)":\s*\[', body, re.M):
        name = m.group(1)
        start = m.end() - 1
        depth, i = 0, start
        while i < len(body):
            if body[i] == "[":
                depth += 1
            elif body[i] == "]":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        raw = body[start:i + 1]
        # 用 ast.literal_eval 而不是 json.loads：这些是 **Python 字面量**
        # （带尾逗号、可能用单引号），json 解不动。
        # literal_eval 只解析字面量、不执行任何代码，所以安全；
        # 也避免了 import emote_presets 会把 Qt 拉起来的问题。
        try:
            import ast

            steps = ast.literal_eval(raw)
            if not isinstance(steps, list):
                steps = []
        except Exception as e:
            logger.debug("capability_snapshot: 预设 %s 解析失败: %s", name, e)
            steps = []
        params: list[str] = []
        for s in steps:
            for p in (s.get("params") or {}):
                if p not in params:
                    params.append(p)
        presets.append(Preset(name=name, steps=tuple(steps), params=tuple(params)))
    return presets


def _find_model_file(char_dir: str) -> Optional[str]:
    """在角色目录里找 model3.json（Cubism 4）或 model.json（Cubism 2）。"""
    for pattern in ("live2d/*.model3.json", "*.model3.json", "live2d/*.model.json", "*.model.json"):
        hits = glob.glob(os.path.join(char_dir, pattern))
        if hits:
            return sorted(hits)[0]
    return None


# ── 主入口 ────────────────────────────────────────────────


def build_snapshot(character_id: str, oc_pet_root: Optional[str] = None) -> CapabilitySnapshot:
    """构建一个角色的能力快照。

    Args:
        character_id: 角色目录名，如 ``"miku"`` / ``"Rory"``。
        oc_pet_root: oc-pet 项目根；默认按本文件位置推断。

    Returns:
        :class:`CapabilitySnapshot`。**任何一步失败都只记 warning，不抛异常**——
        快照缺失时调用方应回退到「不决策」，而不是崩掉渲染。
    """
    if oc_pet_root is None:
        oc_pet_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    char_dir = os.path.join(oc_pet_root, "characters", character_id)
    snap = CapabilitySnapshot(character_id=character_id)

    if not os.path.isdir(char_dir):
        snap.warnings.append(f"角色目录不存在: {char_dir}")
        logger.warning("capability_snapshot: %s", snap.warnings[-1])
        snap.presets = _scan_presets(oc_pet_root)
        return snap

    model_path = _find_model_file(char_dir)
    if not model_path:
        snap.warnings.append("未找到 model3.json")
    else:
        snap.model_path = model_path
        model_dir = os.path.dirname(model_path)
        snap.model_id = os.path.splitext(os.path.basename(model_path))[0]

        motions, rng_m = _scan_motions(model_dir)
        exprs, rng_e = _scan_expressions(model_dir)
        snap.motions = motions

        # 表情名优先用 model3.json 里的中文 Name
        name_map = _read_model3_names(model_path)
        renamed = []
        for e in exprs:
            fname = os.path.basename(e.file)
            renamed.append(Expression(name=name_map.get(fname, e.name), file=e.file, params=e.params))
        snap.expressions = renamed

        merged = dict(rng_m)
        for k, (lo, hi) in rng_e.items():
            if k in merged:
                mlo, mhi = merged[k]
                merged[k] = (min(mlo, lo), max(mhi, hi))
            else:
                merged[k] = (lo, hi)
        snap.param_ranges = merged

        if not motions:
            snap.warnings.append("模型无 motion 文件，动作只能走参数预设路径")

    snap.presets = _scan_presets(oc_pet_root)
    if not snap.presets:
        snap.warnings.append("未读到 emote_presets.py 的预设表")

    logger.info(snap.summary_line())
    return snap


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)
    cid = sys.argv[1] if len(sys.argv) > 1 else "miku"
    s = build_snapshot(cid)
    print(s.describe())
    print()
    print("参数实测范围（模型自己的 motion/expression 曲线量出来的）:")
    for pid in sorted(s.param_ranges):
        lo, hi = s.param_ranges[pid]
        print(f"  {pid:24s} [{lo:8.3f}, {hi:8.3f}]")
