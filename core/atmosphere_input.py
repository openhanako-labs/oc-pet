# -*- coding: utf-8 -*-
"""氛围层输入侧 —— 把「一轮对话」压成一个槽位标签（pos / neg / neu）。

两族输入
--------
1. **情绪族**：由 ``core.emotion_classifier`` 给类别，:func:`core.atmosphere.polarity_of`
   映成槽位。→ 2026-09-21 实测**判死**：真实语料上 clear 峰值只有 0.45，
   θ_hi 只能在 [0.35, 0.45] 三格里动，>0.45 直接归零（输入没有动态范围）；
   而且上游分类器对中文技术陈述句系统性误判（「让我查清楚」→ confused）。
2. **结构族（本模块）**：不量情绪，量**节奏与形态**。实测 clear 峰值 0.62–0.98，
   θ_hi 从 0.45 到 0.65 有**真实梯度**——旋钮是活的。

为什么结构族能活：它不依赖分类器，也不依赖任何模型输出，**直接从对话本身读**。

⚠️ 反过来说清楚：结构量 **没有"理想输入"** 这个概念。情绪有明确语义（喜/怒），
结构只有相对高低——所以触发量只能是「偏离自己的常态」，必须**按滚动分位**切三态，
而且基线只能来自**过去**（离线研究可以用全局分位，上线不行，那是偷看未来）。

实测定案（2026-09-21，138 轮 / 5 个 session，滚动窗口 30，β=0.10）
----------------------------------------------------------------
| 信号 | θ_hi/θ_lo | arm 率 | 覆盖 | 中位段 | 抖动 | 调用/100轮 |
|---|---|---|---|---|---|---|
| **ratio**（谁在多说） | 0.50/0.40 | 3.6% | 26.1% | 5 轮 | 0 | 5.1 |
| drift_u（话题漂移） | 0.45/0.35 | 3.8% | 42.9% | 10 轮 | 0 | 7.5 |

⚠️ **样本量警告**：全部数据只有 **5 个 session**（轮数 [2, 8, 12, 34, 82]，
116/138 来自两个长会话）。上面的参数**有依据，但不够格说"验证过"**。
"""
from __future__ import annotations

import logging
import math
from collections import deque
from typing import Any, Optional

logger = logging.getLogger(__name__)

# 槽位（与 core.atmosphere 的 POL_* 同名同义；此处不复用是为了本模块可独立单测）
SLOT_HIGH = "pos"
SLOT_LOW = "neg"
SLOT_MID = "neu"

DEFAULT_WINDOW = 30
DEFAULT_WARMUP = 8
DEFAULT_LOW_Q = 0.33
DEFAULT_HIGH_Q = 0.67

#: 结构信号名 → (取哪个量, 该信号的推荐阈值)
#:
#: 阈值分开放而不是共用：两种族的 clear 工作点完全不同（情绪 0.40 / 结构 0.50），
#: 混用会让某一族的迟滞带整个落空（2026-09-21 踩过：结构量套 0.25 的 θ_lo
#: → 一旦 arm 就再也不 release，覆盖率 87%、中位段 60 轮）。
_STRUCT_THETA = {"theta_hi": 0.50, "theta_lo": 0.40}
_EMOTION_THETA = {"theta_hi": 0.40, "theta_lo": 0.25}

SIGNALS = ("ratio", "len_u", "drift_u")


def defaults_for(source: str) -> dict:
    """给某族输入一组**实测**阈值默认值（config 未显式给时用）。"""
    if str(source or "").strip().lower() in ("structural", "struct", "ratio",
                                             "drift", "drift_u", "len_u"):
        return dict(_STRUCT_THETA)
    return dict(_EMOTION_THETA)


def signal_value(name: str, user_text: str, reply_text: str,
                 user_vec: Optional[list] = None,
                 prev_user_vec: Optional[list] = None) -> Optional[float]:
    """算某个结构量的**原始值**（连续量，还没切三态）。缺料返回 None。

    * ``ratio``   : log1p(len(user)) − log1p(len(reply))。>0 = 用户说得多。
                    零成本（两个长度相减），而且它是**唯一天然的双方量**——
                    正好对上"真正的关系层氛围要双方进同一个量"那条质疑。
    * ``len_u``   : log1p(len(user))。单侧，只作对照。
    * ``drift_u`` : 1 − cos(上一个用户消息向量, 这个用户消息向量)。话题漂移。
                    需要复用分类器已经算过的 query 向量，否则要额外嵌一次。
    """
    try:
        name = str(name or "").strip().lower()
        if name == "ratio":
            if not user_text or not reply_text:
                return None
            return math.log1p(len(user_text)) - math.log1p(len(reply_text))
        if name == "len_u":
            if not user_text:
                return None
            return math.log1p(len(user_text))
        if name == "drift_u":
            if not user_vec or not prev_user_vec:
                return None
            num = sum(float(a) * float(b) for a, b in zip(user_vec, prev_user_vec))
            na = math.sqrt(sum(float(a) ** 2 for a in user_vec))
            nb = math.sqrt(sum(float(b) ** 2 for b in prev_user_vec))
            if not na or not nb:
                return None
            return 1.0 - (num / (na * nb))
    except Exception:
        logger.debug("signal_value(%s) 失败", name, exc_info=True)
    return None


class RollingQuantileTristate:
    """把连续结构量按**过去**窗口的分位切成三态槽位。**纯内存，无 IO。**

    * 基线**只来自过去**（滑动窗口），不偷看未来——离线研究可以用全局分位，
      上线不行（那是泄漏，会让离线指标虚高）。
    * 前 ``warmup`` 轮一律回 ``neu``（还没有基线就没有"偏离"可言）。
    * 全程异常安全：出任何事回 ``neu``（宁可不动，不能乱动）。
    """

    def __init__(self, window: int = DEFAULT_WINDOW, warmup: int = DEFAULT_WARMUP,
                 low_q: float = DEFAULT_LOW_Q, high_q: float = DEFAULT_HIGH_Q):
        self._window = max(2, int(window))
        self._warmup = max(1, int(warmup))
        self._low_q = min(0.49, max(0.0, float(low_q)))
        self._high_q = max(0.51, min(1.0, float(high_q)))
        self._hist: deque = deque(maxlen=self._window)
        self._n_seen = 0
        self._n_hi = 0
        self._n_lo = 0
        self._n_mid = 0

    def label(self, value: Optional[float]) -> str:
        """给定这一轮的原始值，回一个槽位标签；同时把值推入历史。"""
        try:
            if value is None:
                return SLOT_MID
            v = float(value)
            if math.isnan(v) or math.isinf(v):
                return SLOT_MID
            lab = SLOT_MID
            if len(self._hist) >= self._warmup:
                h = sorted(self._hist)
                q1 = h[min(len(h) - 1, int(len(h) * self._low_q))]
                q2 = h[min(len(h) - 1, int(len(h) * self._high_q))]
                if v >= q2:
                    lab = SLOT_HIGH
                elif v <= q1:
                    lab = SLOT_LOW
            self._hist.append(v)
            self._n_seen += 1
            if lab == SLOT_HIGH:
                self._n_hi += 1
            elif lab == SLOT_LOW:
                self._n_lo += 1
            else:
                self._n_mid += 1
            return lab
        except Exception:
            logger.debug("RollingQuantileTristate.label 失败", exc_info=True)
            return SLOT_MID

    def reset(self) -> None:
        self._hist.clear()
        self._n_seen = self._n_hi = self._n_lo = self._n_mid = 0

    def snapshot(self) -> dict:
        """给 /pet/state 的只读快照。"""
        try:
            h = sorted(self._hist)
            med = h[len(h) // 2] if h else None
            return {
                "window": self._window,
                "warmup": self._warmup,
                "samples": len(self._hist),
                "seen": self._n_seen,
                "median": round(med, 4) if med is not None else None,
                "shares": {"hi": self._n_hi, "mid": self._n_mid, "lo": self._n_lo},
            }
        except Exception:
            return {"error": "snapshot 失败"}


__all__ = [
    "SLOT_HIGH", "SLOT_LOW", "SLOT_MID",
    "DEFAULT_WINDOW", "DEFAULT_WARMUP", "DEFAULT_LOW_Q", "DEFAULT_HIGH_Q",
    "SIGNALS", "defaults_for", "signal_value", "RollingQuantileTristate",
]
