# Copyright 2025-2026 Project N.E.K.O. Team
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""专注模式 — FocusScorer 三信号加权 + hysteresis 状态机（P0-5）

移植说明：
- ``FocusScorer`` / ``FocusScore`` **直接搬运**自 N.E.K.O.
  ``main_logic/activity/focus_scorer.py``（Apache-2.0，见
  ``docs/THIRD_PARTY_NOTICES.md``）；算法与信号定义保持原样，仅把对
  N.E.K.O. ``config`` 的依赖替换为 oc-pet 本地常量/``config.focus`` 段
  （见 ``load_focus_settings``）。
- ``scan_vulnerability_keywords`` 是 oc-pet 本地实现的脆弱性关键词表
  （N.E.K.O. 的 ``config.prompts.prompts_focus`` 未随仓库迁移），属于
  参考重写而非直接搬运。
- ``FocusStateMachine`` 按 oc-pet threading 约束**重写**（N.E.K.O. 是
  asyncio 会话状态机）：纯 Python + ``threading.Lock``，无 Qt 依赖，
  可离屏单测；UI 侧一律在主线程回调（后台线程结果经 Signal 绕回）。

默认关：``config.focus.enabled=false`` 时 ``FocusStateMachine`` 的
``enabled`` 为 False，``update()`` 零副作用（不累积、不切换、不回调、
不写日志）——满足 P0-5 验收「focus.enabled=false 时零行为」。
"""
from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import dataclass, field
from statistics import median
from typing import Callable, Optional

logger = logging.getLogger(__name__)


# ── 默认配置（与 config.py 的 focus 段对齐；信号权重参考 N.E.K.O. 数值）──

DEFAULT_FOCUS_SETTINGS: dict = {
    "enabled": False,              # 专注模式总开关（默认关）
    "glow_strength": 0.3,          # 专注辉光强度 0~1（0=零视觉）
    # FocusScorer 三信号权重：literal per-signal contributions（不归一化）
    "signal_weights": {
        "keyword": 1.0,            # 脆弱性关键词
        "cadence": 0.8,            # 消息节奏（长度骤降 → 证据放大）
        "emotion": 1.0,            # 情绪（负效价 → 正向证据；正效价 → 负向投票）
        "question": 0.6,           # 复杂/客观提问（认知负荷）
    },
    "keyword_saturation": 3,       # 关键词计数饱和点（≥3 个即 1.0）
    "cadence_baseline_window": 8,  # 节奏基线滚动窗口（最近 N 条消息长度）
    "cadence_min_samples": 3,      # 基线最少样本数（不足则 cadence 缺席）
    "cadence_drop_ratio": 0.5,     # 长度降至基线 50% 视为"骤降"→ 1.0
    "emotion_arousal_floor": 0.5,  # 情绪信号 arousal 放大器下限
    "emotion_positive_scale": 0.5, # 正效价（愉悦）对分数的压制系数
    # FocusStateMachine hysteresis / 电荷
    "charge_enter": 0.6,           # 进入专注阈值（与前端 glow enter 对齐）
    "charge_exit": 0.3,            # 退出专注阈值（hysteresis 带）
    "charge_cap": 1.0,             # 电荷上限
    "charge_gain": 0.35,           # 单次正分积累进电荷的比例
    "charge_decay": 0.08,          # 无证据时每次 update 的泄漏衰减
    "idle_cool_faster": 1.6,       # 主动搭话（她说过了）时冷却加速
    "idle_cool_slower": 0.7,       # 沉默时冷却放缓
}

# 可被 FocusScorer 消费的配置键（其余键如 charge_* 交给状态机）
_SCORER_KEYS = frozenset({
    "signal_weights", "keyword_saturation", "cadence_baseline_window",
    "cadence_min_samples", "cadence_drop_ratio",
    "emotion_arousal_floor", "emotion_positive_scale",
})


def load_focus_settings() -> dict:
    """读取 oc-pet ``config.focus`` 段并深度合并默认值。

    安全导入：``config`` 模块无 core 依赖，不会形成循环导入；读取失败时
    回退默认（默认 ``enabled=False`` → 零行为）。
    """
    settings = dict(DEFAULT_FOCUS_SETTINGS)
    settings["signal_weights"] = dict(DEFAULT_FOCUS_SETTINGS["signal_weights"])
    try:
        from config import load_config  # type: ignore
        cfg = load_config().get("focus", None) or {}
        for key, value in cfg.items():
            if key == "signal_weights" and isinstance(value, dict):
                for wk, wv in value.items():
                    if wv is not None:
                        settings["signal_weights"][wk] = float(wv)
            elif key in settings and value is not None:
                settings[key] = value
    except Exception as exc:  # pragma: no cover - 防御式回退
        # 失败 = 专注功能静默关闭（enabled=False），用户以为设置不生效
        logger.warning("focus 配置读取失败，回退默认（enabled=False）: %s", exc)
    return settings


def _scorer_settings(settings: dict) -> dict:
    """从全量设置里挑出 FocusScorer 需要的键（缺省补默认）。"""
    base = DEFAULT_FOCUS_SETTINGS
    out: dict = {}
    for key in _SCORER_KEYS:
        value = settings.get(key, base.get(key))
        if key == "signal_weights":
            merged = dict(base["signal_weights"])
            if isinstance(value, dict):
                for wk, wv in value.items():
                    if wv is not None:
                        merged[wk] = float(wv)
            out[key] = merged
        else:
            out[key] = value
    return out


# ── 脆弱性关键词（oc-pet 本地表，参考 N.E.K.O. prompts_focus 思路重写）──

_VULNERABILITY_KEYWORDS: tuple[str, ...] = (
    # 中文
    "累", "好累", "烦", "烦躁", "崩溃", "压力", "焦虑", "难受", "抑郁",
    "撑不住", "坚持不住", "不行了", "好难", "太难了", "加班", "熬夜",
    "失眠", "头痛", "头疼", "痛苦", "委屈", "难过", "想哭", "无助",
    "害怕", "担心", "紧张", "迷茫", "孤独", "空虚", "没劲", "不想动",
    "完蛋", "糟糕", "失败", "放弃", "绝望", "心累", "emo",
    # English
    "tired", "exhausted", "stressed", "stress", "anxious", "anxiety",
    "depressed", "depression", "overwhelmed", "burnout", "stuck",
    "hopeless", "worried", "scared", "lonely", "sad", "cry", "give up",
    "hard", "difficult", "pressure", "unhappy", "blue", "down",
)


def scan_vulnerability_keywords(text: str) -> int:
    """统计文本中脆弱性关键词的出现次数（区分大小写不敏感）。

    Args:
        text: 用户消息原文（可为空）。

    Returns:
        命中次数（未命中返回 0）。中英文关键词都参与扫描——
        混合语言输入是常见场景，扫描与语言无关。
    """
    if not text:
        return 0
    lowered = text.lower()
    count = 0
    for kw in _VULNERABILITY_KEYWORDS:
        if kw in lowered:
            count += lowered.count(kw)
    return count


# ── 打分结果 ──

@dataclass(frozen=True)
class FocusScore:
    """一次打分的输出：最终分数 + 各子信号明细。

    ``signals`` 保存每个子信号的值（keyword/cadence/question 为 ``[0,1]``，
    emotion 为 SIGNED，愉悦时为负），未适用时为 ``None``——保留给诊断/
    日志，方便调参时看*为什么*这一轮喂给电荷累加器高分或低分。
    """

    score: float
    signals: dict[str, float | None] = field(default_factory=dict)


class FocusScorer:
    """每会话专注信号打分器。廉价、同步、无 I/O。

    与 N.E.K.O. 原版一致：唯一的状态是"最近用户消息长度"的滚动基线
    （cadence 信号用）。每个会话一个实例，与行为层同生命周期。
    """

    def __init__(self, settings: dict | None = None) -> None:
        cfg = _scorer_settings(settings or {})
        weights: dict = cfg["signal_weights"] or {}
        self.signal_weights: dict[str, float] = {
            name: float(weights.get(name, 0.0)) for name in ("keyword", "cadence", "emotion", "question")
        }
        self.keyword_saturation: int = max(1, int(cfg["keyword_saturation"]))
        self.cadence_baseline_window: int = max(1, int(cfg["cadence_baseline_window"]))
        self.cadence_min_samples: int = max(1, int(cfg["cadence_min_samples"]))
        self.cadence_drop_ratio: float = max(0.0, float(cfg["cadence_drop_ratio"]))
        self.emotion_arousal_floor: float = max(0.0, min(1.0, float(cfg["emotion_arousal_floor"])))
        self.emotion_positive_scale: float = max(0.0, min(1.0, float(cfg["emotion_positive_scale"])))
        # 滚动最近消息长度 → cadence 基线（中位数）。
        self._recent_lengths: deque[int] = deque(maxlen=self.cadence_baseline_window)

    # ── public API ──────────────────────────────────────────────────

    def score(self, *, user_text: str, emotion_reading=None) -> FocusScore:
        """给一轮内联对话打分（keyword + cadence + emotion + question）。

        副作用：当 ``user_text`` 是真实（非空）消息时，其长度在 cadence
        信号**计算之后**追加进基线（cadence 总是拿当前消息和*之前*的消息比）。

        ``emotion_reading`` 是主情绪读数（duck-typed：任意带
        ``valence``/``arousal`` 浮点的对象，可选 ``complexity``），
        来自调用方（oc-pet 侧可用 ``emotion_reading_from_state`` 适配），
        或 ``None``。打分器本身不做任何 I/O/分析。

        与 N.E.K.O. 一致：cadence 不是独立触发信号，只在存在脆弱性证据
        （关键词 / 复杂提问 / 负效价情绪）时参与加权，避免短句欢乐回复误触发。
        """
        kw = self._signal_keyword(user_text)
        emotion = self._signal_emotion(emotion_reading)
        question = self._signal_question(emotion_reading)
        cadence = self._signal_cadence(user_text)
        has_distress_evidence = (
            kw is not None
            or question is not None
            or (emotion is not None and emotion > 0.0)
        )
        if not has_distress_evidence:
            cadence = None

        signals: dict[str, float | None] = {
            "keyword": kw, "cadence": cadence, "emotion": emotion, "question": question,
        }
        score = _weighted_sum(signals, self.signal_weights)

        if user_text.strip():
            self._recent_lengths.append(len(user_text.strip()))

        return FocusScore(score=score, signals=signals)

    def reset(self) -> None:
        """清空 cadence 基线（会话结束/热切换时调用）。"""
        self._recent_lengths.clear()

    # ── sub-signals ──────────────────────────────────────────────────

    def _signal_keyword(self, user_text: str) -> Optional[float]:
        count = scan_vulnerability_keywords(user_text)
        if count <= 0:
            return None
        sat = self.keyword_saturation
        return min(count / sat, 1.0)

    def _signal_cadence(self, user_text: str) -> Optional[float]:
        text = user_text.strip()
        if not text:
            return None
        if len(self._recent_lengths) < self.cadence_min_samples:
            return None
        baseline = median(self._recent_lengths)
        if baseline <= 0:
            return None
        cur = len(text)
        lo = self.cadence_drop_ratio * baseline
        if cur >= baseline:
            return 0.0
        if cur <= lo:
            return 1.0
        return (baseline - cur) / (baseline - lo)

    def _signal_emotion(self, emotion_reading) -> Optional[float]:
        """SIGNED 情绪信号：负效价 → 正向证据（≤ +1）；正效价 → 负向
        投票（≥ -POSITIVE_SCALE ≈ -0.5）。中性（无读数/无轴/≈0）返回
        ``None``——不稀释 keyword/question；真实愉悦读数是一个真（负）
        投票，不是 no-op。
        """
        if emotion_reading is None:
            return None
        valence = getattr(emotion_reading, "valence", None)
        arousal = getattr(emotion_reading, "arousal", None)
        if valence is None or arousal is None:
            return None
        try:
            valence = float(valence)
            arousal = float(arousal)
        except (TypeError, ValueError):
            return None
        floor = self.emotion_arousal_floor
        pos_scale = self.emotion_positive_scale
        arousal = max(0.0, min(1.0, arousal))
        m = floor + (1.0 - floor) * arousal  # arousal amplifier ∈ [floor, 1]
        if valence < 0.0:
            return min(1.0, -valence * m)
        if valence > 0.0:
            return -min(1.0, valence * m * pos_scale)
        return None

    def _signal_question(self, emotion_reading) -> Optional[float]:
        """认知负荷加分：主模型 ``complexity`` 读数（数学/逻辑/推理等
        复杂客观提问）。正交于情绪但并入同一电荷。"只有正证据"规则：
        无读数/无复杂度返回 ``None``（缺席），绝不稀释情绪轮次。
        """
        if emotion_reading is None:
            return None
        complexity = getattr(emotion_reading, "complexity", None)
        if complexity is None:
            return None
        try:
            complexity = float(complexity)
        except (TypeError, ValueError):
            return None
        complexity = max(0.0, min(1.0, complexity))
        return complexity if complexity > 0.0 else None


def _weighted_sum(signals: dict, weights: dict) -> float:
    """适用（非 None）信号上的直接加权和——无分母。

    每个在场信号贡献 ``weight × value`` 的绝对量；``None``（不适用）
    直接缺席、既不拉向 0 也不放大。无信号适用时返回 0.0。
    饱和时总和可超过 1.0（≈ sum(weights)），由电荷累加器的 cap 收敛。
    """
    total = 0.0
    for name, val in signals.items():
        if val is None:
            continue
        total += float(weights.get(name, 0.0)) * float(val)
    return total


# ── 状态机（hysteresis，按 oc-pet threading 重写）──────────────────

StateListener = Callable[[bool, float, dict], None]


class FocusStateMachine:
    """专注 hysteresis 状态机：泄漏电荷累加器 + 进出阈值。

    线程安全：所有状态写/读加 ``threading.Lock``——行为层 tick 与后台
    线程（如对话引擎回调）都可能调用 ``update``。UI 侧回调一律要求
    在主线程执行（后台线程请先经 Signal 绕回）。

    默认关：``enabled=False`` 时 ``update()`` 返回 False 且**零副作用**
    （不累积、不切换、不回调、不写日志）——P0-5 验收硬约束。
    """

    def __init__(self, settings: dict | None = None, enabled: bool | None = None) -> None:
        cfg = settings or load_focus_settings()
        self._enabled = bool(cfg.get("enabled", False)) if enabled is None else bool(enabled)
        self._enter = max(0.0, float(cfg.get("charge_enter", 0.6)))
        self._exit = max(0.0, min(self._enter, float(cfg.get("charge_exit", 0.3))))
        self._cap = max(0.0, float(cfg.get("charge_cap", 1.0)))
        self._gain = max(0.0, float(cfg.get("charge_gain", 0.35)))
        self._decay = max(0.0, float(cfg.get("charge_decay", 0.08)))
        self._cool_faster = max(0.0, float(cfg.get("idle_cool_faster", 1.6)))
        self._cool_slower = max(0.0, float(cfg.get("idle_cool_slower", 0.7)))
        self._lock = threading.Lock()
        self._charge: float = 0.0
        self._active: bool = False
        self._last_score: float = 0.0
        self._last_signals: dict[str, float | None] = {}
        self._listeners: list[StateListener] = []

    # ── 只读属性 ──

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._enabled

    @property
    def active(self) -> bool:
        with self._lock:
            return self._active

    @property
    def charge(self) -> float:
        with self._lock:
            return self._charge

    @property
    def last_score(self) -> float:
        with self._lock:
            return self._last_score

    @property
    def last_signals(self) -> dict[str, float | None]:
        with self._lock:
            return dict(self._last_signals)

    # ── 事件 ──

    def add_listener(self, listener: StateListener) -> None:
        """注册状态变化回调：``cb(active, charge, signals)``。

        回调在 ``update`` 调用线程内执行——行为层在 Qt 主线程调用即主线程
        回调；后台线程调用方必须自行把 UI 操作经 Signal 绕回主线程。
        """
        if callable(listener):
            with self._lock:
                self._listeners.append(listener)

    def _notify(self, active: bool, charge: float, signals: dict) -> None:
        listeners = list(self._listeners)
        for cb in listeners:
            try:
                cb(active, charge, signals)
            except Exception as exc:
                # listener 异常会每 tick 重发，故按回调名去重报警
                seen = getattr(self, "_listener_err_names", None)
                if seen is None:
                    seen = self._listener_err_names = set()
                name = getattr(cb, "__name__", repr(cb))
                if name not in seen:
                    seen.add(name)
                    logger.warning("focus listener %s 异常（后续同类静音）: %s", name, exc)

    # ── 状态更新 ──

    def update(self, score: FocusScore | None = None, *, spoke: bool | None = None) -> bool:
        """积分一轮（可为 None = 空转/冷却 tick）。

        Args:
            score: FocusScorer.score() 的输出；``None`` 表示无新证据的冷却 tick。
            spoke: 本轮桌宠是否说过话（主动搭话）；影响活跃期的冷却速度
                   （说过 → 更快冷却）。

        Returns:
            状态是否发生切换（False = 未切换 / 禁用）。
        """
        if not self._enabled:
            return False
        with self._lock:
            s = score.score if score is not None else 0.0
            delta = s * self._gain
            if delta > 0.0:
                self._charge = min(self._cap, self._charge + delta)
            else:
                decay = self._decay
                if self._active:
                    decay *= self._cool_faster if spoke else self._cool_slower
                self._charge = max(0.0, self._charge + delta - decay)
            self._last_score = s
            self._last_signals = dict(score.signals) if score is not None else {}
            changed = self._transition_locked()
            active_now = self._active
            charge_now = self._charge
            signals_now = dict(self._last_signals)
        if changed:
            logger.info("focus=%s charge=%.3f signals=%s", "on" if active_now else "off", charge_now, signals_now)
            self._notify(active_now, charge_now, signals_now)
        return changed

    def idle_cool(self, *, spoke: bool = False) -> bool:
        """主动搭话/沉默冷却 tick（不进分，只衰减）。

        ``spoke=True`` 表示桌宠主动说了话（更快冷却）；``False`` 表示沉默
        （更慢冷却）——与 N.E.K.O. ``_focus_idle_cooldown`` 语义一致。
        """
        return self.update(None, spoke=spoke)

    def reset(self) -> None:
        """清空电荷并强制回到非专注（不触发监听回调）。"""
        with self._lock:
            self._charge = 0.0
            self._active = False
            self._last_score = 0.0
            self._last_signals = {}

    def _transition_locked(self) -> bool:
        """hysteresis：进入需 ≥ enter，退出需 < exit（调用方已持锁）。"""
        if not self._active and self._charge >= self._enter:
            self._active = True
            return True
        if self._active and self._charge < self._exit:
            self._active = False
            return True
        return False


# ── 便捷构造 / 情绪适配 ───────────────────────────────────────────

def create_focus_core(settings: dict | None = None) -> tuple[FocusScorer, FocusStateMachine]:
    """一次性创建 (FocusScorer, FocusStateMachine)，二者共享同一份配置。

    T05 接线用：行为层持有这两个实例即可完成打分→状态机联动。
    """
    cfg = settings if settings is not None else load_focus_settings()
    return FocusScorer(settings=cfg), FocusStateMachine(settings=cfg)


# oc-pet 情绪名 → (valence, arousal) 近似映射（负效价 → 专注证据）
EMOTION_VA_MAP: dict[str, tuple[float, float]] = {
    "sad": (-0.65, 0.30),
    "angry": (-0.50, 0.80),
    "surprised": (-0.15, 0.75),
    "thinking": (0.05, 0.40),
    "missing": (-0.25, 0.30),
    "working": (-0.10, 0.45),
    "listening": (0.05, 0.25),
    "speaking": (0.20, 0.50),
    "happy": (0.60, 0.50),
    "cute": (0.65, 0.45),
    "neutral": (0.0, 0.0),
}


class EmotionReading:
    """最小情绪读数适配器：把 oc-pet 情绪快照映射为 (valence, arousal)。

    duck-typed 满足 ``FocusScorer._signal_emotion/_signal_question``
    的 ``valence``/``arousal``/``complexity`` 读取。``complexity`` 默认
    None（oc-pet 暂无主模型复杂度读数，留 T05 扩展）。
    """

    __slots__ = ("valence", "arousal", "complexity")

    def __init__(self, emotion: str, intensity: float = 0.0, complexity: float | None = None):
        valence, arousal = EMOTION_VA_MAP.get(str(emotion or "neutral"), (0.0, 0.0))
        self.valence: float = valence
        self.arousal: float = max(0.0, min(1.0, arousal * max(0.05, intensity or 0.5)))
        self.complexity: float | None = complexity


def emotion_reading_from_state(emotion_state) -> EmotionReading | None:
    """把 oc-pet ``EmotionStateMachine``（或任意带 current/intensity 的对象）
    转成 ``EmotionReading``；中性/无读数返回 None（信号缺席）。
    """
    if emotion_state is None:
        return None
    try:
        emotion = emotion_state.current
        intensity = emotion_state.intensity
    except Exception:
        return None
    if not emotion or emotion == "neutral":
        return None
    return EmotionReading(emotion=emotion, intensity=float(intensity or 0.0))
