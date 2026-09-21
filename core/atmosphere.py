# -*- coding: utf-8 -*-
"""氛围累积层 — 慢变量情绪（3 态极性份额 EMA）。

设计（2026-09-21，方案见 docs/氛围累积层-方案-2026-09-21.md v3）
----------------------------------------------------------------

**要解决的问题**：长对话里桌宠的语气越来越像同一个人。缺的不是情绪识别
（`emotion_classifier` 已经很好），而是**跨轮次累积的那个慢变量**——单轮情绪
的"平均"不等于"我们之间的氛围"。

**为什么不是"让主模型多输出一个字段"**：`feel` / `emotion` / `do` / `action`
这一整批标签在链路上会被消费/剥离（`conversation_engine` 与 `hanako_monitor`
两处）。跨轮状态不能建在一条当轮就可能被吃掉的流上。本模块只消费
`emotion_classifier` 的**类别**输出，不新增任何模型契约。

**为什么是"3 态极性份额"而不是"连续效价"**（三次实测的结论，别重走）：

| 方案 | 实测触发率 | 死因 |
|---|---|---|
| 连续效价 + 一阶 IIR（α=0.15，θ=0.35） | **0.0%** | 解析矛盾：放大系数 ≈0.84 配不上阈值 0.35；理想输入稳态也只有 0.168 |
| 14 类类别份额 EMA | **0.0%**（慢记忆） | 类别空间太大，集中度永远够不着阈值 |
| **3 态极性份额 EMA（本模块）** | **8.1%**（原始越阈率）/ **1.4%**（状态机 arm 率） | — |

**归因要说准**：修复成立的原因是**类别空间从 14 压到 3**，不是"离散优于连续"。

**⚠️ 两个 8.1% 与 1.4% 的区别（2026-09-21 扫参后发现）**：8.1% 是**原始 EMA
越过阈值**的比例（v5 测的）；真实语料重放**完整状态机**后 arm 率只有 1.4%。
预估行为时用 1.4%，不要用 8.1%。

**θ_hi 不是有效旋钮（实测）**：真实语料上 θ_hi ∈ [0.35, 0.45] 三格结果几乎相同，
**> 0.45 直接归零**——因为真实 clear 峰值约 0.45。这不是参数不好调，是**输入动态
范围太窄**。多数派闸（dominant != neu）在 0.35–0.45 与不设闸几乎等价，保留它只是更保守。

**⚠️ 上线前提（当前未满足）**：上游分类器对中文技术陈述句系统性误判——
「让我先搞清楚问题在哪」→ confused、「不用等，现在就跑」→ anxiety、
「第一个成功了！…修一下」→ anger。pet 侧非中性里大部分是这类误判，不是真情绪；
两侧同时非中性仅 6/138。**现在开启，注进去的是误判。**

**未解决的质疑（写在最前面，别被"跑通了"盖过去）**：本模块量的是
**用户/桌宠单条文本情绪的慢平均**，严格说不是"关系层氛围"（后者应包含
「他在防备我」这类对对方的反应）。v3 只是缓解，没有解决。

线程/边界约束
-------------
- **纯计算**：不发网络请求、不碰 Qt、不写盘、不建线程。
- 调用方（mixin）负责把分类结果喂进 :meth:`AtmosphereState.observe`，
  并按返回的 action 决定是否调 utility model / 注入 prompt。
- 任何异常都不该冒泡影响对话：公开方法内部一律 try 兜底。
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── 常量 ────────────────────────────────────────────────

POL_POS = "pos"
POL_NEG = "neg"
POL_NEU = "neu"
POLARITIES = (POL_POS, POL_NEG, POL_NEU)

#: 效价绝对值低于此值 → 记为中性（与 EMOTION_VAD_PRESETS 的量级对齐）
POLARITY_EPS = 0.05

DEFAULT_BETA = 0.10          # 记忆 ≈ 1/β = 10 轮（实测：这是"氛围"该有的时间尺度）
DEFAULT_THETA_HI = 0.40      # 触发阈值（实测触发率 8.1%）
DEFAULT_THETA_LO = 0.25      # 解除阈值（迟滞带；待扫参复核）
DEFAULT_DELTA_RENDER = 0.15  # 已渲染态下，clear 变化超过它才重渲染
DEFAULT_RENDER_MIN_TURNS = 8 # 已渲染态下，至少隔这么多轮才允许重渲染

#: 份额向中性回落的半衰期（小时）。
#:
#: 取值依据（2026-09-21 实测）：用户两轮回话的间隔是**双峰**的——同一场对话里
#: 中位 14.5 分钟，跨开对话则 p90 达 24.3 小时。两个峰的几何中点 =
#: √(0.24h × 24.3h) ≈ 2.4h，取整 **3 小时**。
#: 效果：隔 15 分钟回落 6%（不影响同一场对话），隔 3 小时剩一半，
#: 隔 24 小时剩 0.2%（隔夜的"氛围"不会冒充"最近"）。
DEFAULT_HALF_LIFE_HOURS = 3.0

# 触发后的状态机取值
ACT_DISABLED = "disabled"    # 开关关着
ACT_IDLE = "idle"            # 未触发
ACT_ARM = "arm"              # 从 idle 首次触发 → 该调 utility 渲染
ACT_HOLD = "hold"            # 已渲染，维持注入
ACT_REFRESH = "refresh"      # 已渲染且变化够大/够久 → 重新渲染
ACT_RELEASE = "release"      # 回落到迟滞带以下 → 撤注入

#: ⚠️ 槽位名 **pos / neg / neu 只是槽位**，不等于"正价 / 负价 / 中性"。
#: 两种输入都会映到这三个槽：
#:   * 情绪输入：pos=正价，neg=负价，neu=中性（由 :func:`polarity_of` 映射）
#:   * 结构输入：pos=高一端，neg=低一端，neu=常态带（由
#:     ``core.atmosphere_input`` 的滚动分位映射）
#: 状态机只认"两端 + 中间"，这个抽象是它同时能吃两种输入的原因。
#: 沿旧名是为了不动状态机与既有测试，**不要按字面读**（日志里看 label 而不是槽位）。


# ── 纯函数（好单测）────────────────────────────────────

#: 已经报过"未知类别"的名字。**只报一次**：本函数每一轮都调，
#: 不能刷屏；但完全静默会让"把槽位当类别喂"这类接线错误永久不可见。
_UNKNOWN_CATEGORY_WARNED: set = set()


def polarity_of(category: Any) -> str:
    """情绪类别 → 槽位（pos / neg / neu）。

    槽位取自 ``EMOTION_VAD_PRESETS`` 的效价分量符号；查不到的类别一律 neu
    （宁可不动，不能乱动——与分类器的兜底取向一致）。

    ⚠️ **查不到的类别会报一次 warning**。2026-09-21 两次真实事故的根都是它：
    把**槽位标签**（pos/neg/neu）当情绪类别喂进来，查询落空 → 静默全 neu →
    整层永不触发，而且不报错。若看到这条警告，先检查是不是该走
    :meth:`AtmosphereState.observe_label`。
    """
    try:
        from core.emotion_classifier import EMOTION_VAD_PRESETS
        key = str(category or "")
        if key and key not in EMOTION_VAD_PRESETS \
                and key not in _UNKNOWN_CATEGORY_WARNED \
                and len(_UNKNOWN_CATEGORY_WARNED) < 64:
            _UNKNOWN_CATEGORY_WARNED.add(key)
            logger.warning(
                "atmosphere: 未知情绪类别 %r → 按中性处理。常见原因："
                "把槽位标签(pos/neg/neu)当类别喂给了 observe()；"
                "结构输入应走 observe_label()。", key)
        v = float(EMOTION_VAD_PRESETS.get(key, (0.0, 0.0, 0.0))[0])
    except Exception:
        return POL_NEU
    if v > POLARITY_EPS:
        return POL_POS
    if v < -POLARITY_EPS:
        return POL_NEG
    return POL_NEU


def new_shares() -> dict[str, float]:
    """初始份额：全中性（等价于"还没有任何倾向"）。"""
    return {POL_POS: 0.0, POL_NEG: 0.0, POL_NEU: 1.0}


def update_shares(shares: dict[str, float], polarity: str, beta: float) -> dict[str, float]:
    """一轮份额 EMA：``p_c ← β·1[c] + (1−β)·p_c``。

    输入是 one-hot，所以噪声在平均里被消化，而不是被幅度放大——这正是
    连续效价方案死掉的地方（它把微小的 |obs| 当信号）。返回新 dict，不改入参。
    """
    b = max(0.0, min(1.0, float(beta)))
    out: dict[str, float] = {}
    for c in POLARITIES:
        prev = float(shares.get(c, 0.0) or 0.0)
        out[c] = b * (1.0 if c == polarity else 0.0) + (1.0 - b) * prev
    # 数值归一（防长期浮点漂移把份额推离 1.0）
    total = sum(out.values()) or 1.0
    return {c: out[c] / total for c in POLARITIES}


def clear_value(shares: dict[str, float]) -> float:
    """"明确极性"份额 = max(pos, neg)；neu 主导时它自然很小。"""
    return max(float(shares.get(POL_POS, 0.0) or 0.0),
               float(shares.get(POL_NEG, 0.0) or 0.0))


def dominant(shares: dict[str, float]) -> str:
    """"份额最大的极性（并列时优先 neu，避免无倾向被读成有倾向）。"""
    best, best_v = POL_NEU, float(shares.get(POL_NEU, 0.0) or 0.0)
    for c in (POL_POS, POL_NEG):
        v = float(shares.get(c, 0.0) or 0.0)
        if v > best_v:
            best, best_v = c, v
    return best


# ── 状态与状态机 ────────────────────────────────────────

@dataclass
class AtmosphereConfig:
    """config.atmosphere 的归一化视图（缺省全部按方案默认值）。"""
    enabled: bool = False
    beta: float = DEFAULT_BETA
    theta_hi: float = DEFAULT_THETA_HI
    theta_lo: float = DEFAULT_THETA_LO
    delta_render: float = DEFAULT_DELTA_RENDER
    render_min_turns: int = DEFAULT_RENDER_MIN_TURNS
    half_life_hours: float = DEFAULT_HALF_LIFE_HOURS

    @classmethod
    def from_dict(cls, cfg: Optional[dict]) -> "AtmosphereConfig":
        cfg = cfg if isinstance(cfg, dict) else {}
        def _f(key, default):
            try:
                v = cfg.get(key)
                return float(v) if v is not None and v != "" else float(default)
            except Exception:
                return float(default)
        def _i(key, default):
            try:
                v = cfg.get(key)
                return int(v) if v is not None and v != "" else int(default)
            except Exception:
                return int(default)
        theta_hi = _f("theta_hi", DEFAULT_THETA_HI)
        theta_lo = _f("theta_lo", DEFAULT_THETA_LO)
        if theta_lo > theta_hi:      # 迟滞带反了 → 回退默认，别让配置把状态机拧成振荡器
            theta_lo, theta_hi = DEFAULT_THETA_LO, DEFAULT_THETA_HI
        return cls(
            enabled=bool(cfg.get("enabled", False)),
            beta=_f("beta", DEFAULT_BETA),
            theta_hi=theta_hi,
            theta_lo=theta_lo,
            delta_render=_f("delta_render", DEFAULT_DELTA_RENDER),
            render_min_turns=max(1, _i("render_min_turns", DEFAULT_RENDER_MIN_TURNS)),
            half_life_hours=max(0.0, _f("half_life_hours", DEFAULT_HALF_LIFE_HOURS)),
        )


@dataclass
class AtmosphereDecision:
    """一次 observe 的结果（给调用方看，不含副作用）。"""
    action: str = ACT_IDLE
    clear: float = 0.0
    polarity: str = POL_NEU
    turns: int = 0
    reason: str = ""

    def as_line(self) -> str:
        return (f"atmos action={self.action} clear={self.clear:.3f} "
                f"pol={self.polarity} turns={self.turns}"
                + (f" ({self.reason})" if self.reason else ""))


class AtmosphereState:
    """慢变量持有者：份额 + 迟滞状态机。**纯内存，无 IO。**"""

    def __init__(self, config: Optional[dict] = None):
        self.set_config(config)

    # ── 配置 ──
    def set_config(self, config: Optional[dict] = None) -> None:
        self._cfg = AtmosphereConfig.from_dict(config)
        # 首次调用要建状态；关掉开关时清干净（避免"关了再开"带着旧氛围回来）
        if not hasattr(self, "_shares") or not self._cfg.enabled:
            self.reset()

    @property
    def cfg(self) -> AtmosphereConfig:
        return self._cfg

    @property
    def enabled(self) -> bool:
        return bool(self._cfg.enabled)

    # ── 状态 ──
    def reset(self) -> None:
        self._shares: dict[str, float] = new_shares()
        self._rendered: bool = False
        self._clear_at_render: float = 0.0
        self._polarity_at_render: str = POL_NEU
        self._turns_since_render: int = 0
        self._turns: int = 0
        self._text: str = ""
        self._last_decay: float = 1.0

    def decay(self, elapsed_sec: Optional[float]) -> float:
        """把份额按半衰期向中性拉回，返回实际系数（1.0 = 没动）。

        为什么需要：状态活在进程里，而对话会**隔很久才续**。实测用户两轮回话的
        间隔是双峰的（同一场对话中位 14.5 分钟；跨开对话 p90 24.3 小时），
        所以"最近 10 轮"完全可能横跨三天。半衰期把它压到"一场对话"的量级。

        为什么是"拉回"而不是"清零"：短会话（实测中位 12 轮）一旦清零就再也
        攒不起来，这层等于不存在。按比例拉回则保留了"同一场对话内"的连续性，
        又不会让隔夜的状态冒充"最近"。

        异常安全：出任何事都返回 1.0（不动），绝不冒泡。
        """
        try:
            hl = float(self._cfg.half_life_hours) * 3600.0
            if hl <= 0 or elapsed_sec is None:
                return 1.0
            sec = float(elapsed_sec)
            # NaN / inf 必须挡住：0.5 ** (nan/hl) = nan，会把份额永久毒化成 NaN
            # （2026-09-21 单测抓出来的真 bug，不是防御性装饰）。
            if not math.isfinite(sec) or sec <= 0:
                return 1.0
            f = 0.5 ** (sec / hl)
        except Exception:
            return 1.0
        if not math.isfinite(f) or f >= 1.0:
            return 1.0
        try:
            pos = float(self._shares.get(POL_POS, 0.0)) * f
            neg = float(self._shares.get(POL_NEG, 0.0)) * f
            # neu 用 1 − 两端兜底，保证份额和恒为 1（不让浮点漂出去）
            self._shares = {POL_POS: pos, POL_NEG: neg,
                            POL_NEU: max(0.0, 1.0 - pos - neg)}
            self._last_decay = f
        except Exception:
            return 1.0
        return f

    @property
    def shares(self) -> dict[str, float]:
        return dict(self._shares)

    @property
    def text(self) -> str:
        """当前注入用的氛围描述（未渲染时为空串）。"""
        return self._text

    # ── 主循环 ──
    def observe(self, category: Any = None,
                elapsed_sec: Optional[float] = None) -> AtmosphereDecision:
        """喂一轮的**情绪类别**（内部做 类别→槽位 映射）。

        ``elapsed_sec``：距上一次喂料的秒数。给了就先按半衰期向中性拉回
        （见 :meth:`decay`），让"最近"不变成"很久以前"。

        调用方按 ``action`` 决定后续：
          - ``arm`` / ``refresh`` → 去调 utility model 渲染一句，再回填
            :meth:`mark_rendered`
          - ``release`` → 撤掉 prompt 段
          - 其余 → 什么都不做
        """
        return self.observe_label(polarity_of(category), elapsed_sec)

    def observe_label(self, label: Any,
                      elapsed_sec: Optional[float] = None) -> AtmosphereDecision:
        """喂一轮**已归一好的槽位标签**（pos/neg/neu），跳过类别→槽位映射。

        为什么单开一个入口：结构输入（``core.atmosphere_input``）产出的**就是**
        槽位标签。若让它走 :meth:`observe`，会被 ``polarity_of`` 再映一次——
        而 ``polarity_of("pos")`` 查不到预设、**静默返回 neu**，整层就永不触发。
        （这个坑 2026-09-21 在扫参脚本里真踩过：整张表 0.0%，连理想输入都是 0，
        而且**不报错**。所以把两个入口分开写死，而不是靠调用方小心。）

        异常一律吞掉并回 ``idle``——**绝不能因为氛围层把对话打断**。
        """
        try:
            if not self._cfg.enabled:
                return AtmosphereDecision(action=ACT_DISABLED, turns=self._turns)

            if elapsed_sec is not None:
                self.decay(elapsed_sec)

            # 已经是槽位就用它；否则当作情绪类别再映一次（对未知值 → neu）
            pol = label if label in POLARITIES else polarity_of(label)
            self._shares = update_shares(self._shares, pol, self._cfg.beta)
            self._turns += 1
            if self._rendered:
                self._turns_since_render += 1

            clear = clear_value(self._shares)
            dom = dominant(self._shares)

            if self._rendered:
                if clear <= self._cfg.theta_lo:
                    self._rendered = False
                    self._text = ""
                    return AtmosphereDecision(ACT_RELEASE, clear, dom, self._turns,
                                              "回落迟滞带以下")
                drift = abs(clear - self._clear_at_render)
                if (drift >= self._cfg.delta_render
                        or self._turns_since_render >= self._cfg.render_min_turns):
                    return AtmosphereDecision(
                        ACT_REFRESH, clear, dom, self._turns,
                        f"drift={drift:.3f} turns_since={self._turns_since_render}")
                return AtmosphereDecision(ACT_HOLD, clear, dom, self._turns)

            if clear >= self._cfg.theta_hi and dom != POL_NEU:
                return AtmosphereDecision(ACT_ARM, clear, dom, self._turns,
                                          "越过触发阈值")
            return AtmosphereDecision(ACT_IDLE, clear, dom, self._turns)
        except Exception:
            logger.debug("atmosphere: observe 异常（忽略）", exc_info=True)
            return AtmosphereDecision(action=ACT_IDLE, turns=getattr(self, "_turns", 0))

    def mark_rendered(self, text: str, decision: Optional[AtmosphereDecision] = None) -> None:
        """utility model 渲染完成后回填（决定进入 rendered 态并记住基线）。"""
        try:
            clear = clear_value(self._shares)
            self._rendered = True
            self._text = str(text or "")
            self._clear_at_render = clear
            self._polarity_at_render = dominant(self._shares)
            self._turns_since_render = 0
        except Exception:
            logger.debug("atmosphere: mark_rendered 异常（忽略）", exc_info=True)

    # ── 可观测性 ──
    def snapshot(self) -> dict:
        """给 pet_state / GET /pet/state 的只读快照。"""
        try:
            return {
                "enabled": self.enabled,
                "shares": {k: round(v, 4) for k, v in self._shares.items()},
                "clear": round(clear_value(self._shares), 4),
                "polarity": dominant(self._shares),
                "rendered": self._rendered,
                "turns": self._turns,
                "text": self._text,
                "last_decay": round(getattr(self, "_last_decay", 1.0), 6),
            }
        except Exception:
            return {"enabled": False, "error": "snapshot 失败"}


# ── prompt 段 ───────────────────────────────────────────

_POLARITY_PHRASE = {
    POL_POS: "偏松弛、偏暖",
    POL_NEG: "偏紧绷、偏防守",
}


def prompt_section(text: str, shares: Optional[dict] = None) -> str:
    """把氛围渲染成 prompt 段。

    - ``text`` 非空 → 用 utility model 写的那句自然语言（首选）
    - ``text`` 为空但份额够（utility 未配置）→ 退化为数值直陈，不让这个层静默消失

    只讲倾向、**不给指令**（不出现"你要更冷淡"这类命令式，避免把语气推过头）。
    """
    body = str(text or "").strip()
    if not body and shares:
        # 注意：这里用 pos 与 neg 直接比大小（问的是"倾向哪边"），
        # 不用 dominant()——后者问的是"三者里谁占多数"（neu 可能占多数
        # 却仍然有明显极性），两个问题不同，不能混用。
        pos = float(shares.get(POL_POS, 0.0) or 0.0)
        neg = float(shares.get(POL_NEG, 0.0) or 0.0)
        lean = POL_NEG if neg > pos else (POL_POS if pos > neg else POL_NEU)
        phrase = _POLARITY_PHRASE.get(lean)
        if phrase:
            body = f"最近几轮的基调{phrase}（强度 {clear_value(shares):.2f}）"
    if not body:
        return ""
    return f"【氛围】{body}"


__all__ = [
    "POL_POS", "POL_NEG", "POL_NEU", "POLARITIES",
    "ACT_DISABLED", "ACT_IDLE", "ACT_ARM", "ACT_HOLD", "ACT_REFRESH", "ACT_RELEASE",
    "AtmosphereConfig", "AtmosphereDecision", "AtmosphereState",
    "polarity_of", "new_shares", "update_shares", "clear_value", "dominant",
    "prompt_section",
]
