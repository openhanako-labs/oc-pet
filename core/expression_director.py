"""表达决策层 —— 让桌宠自己决定「这次该用什么表情、什么动作」。

## 它治什么病

`live2d_renderer.py` 里有一条实测记录：把 53 个预设名 + 13 个 motion 名
（共 66 个）挤成一行塞进 prompt，`[do:]` **全历史 0 次使用**。

后来做了 `_AI_DO_ALIASES`（22 个中文语义标签）绕过去——但那是**给 LLM 猜**：
LLM 说「害羞」，代码映射到 `blush_shy`。中间那一跳靠的是人手写的映射表，
LLM 并没有真的在「选动作」，它只是在「描述情绪」。

本模块把这一跳换成**一次结构化决策**：给本地决策引擎一份带描述的候选清单，
让它直接挑。好处是候选可以任意扩张（53 个预设全给），而不用人手维护别名表。

## 三条实测得出的硬约束（不要绕过）

1. **候选选项必须写在 description 的第一行**。
   引擎的 `to_parallel_schema_str()` 只取 `description.split('\\n')[0]`——
   换行后的选项说明**根本不会进 prompt**。这是踩过的坑。

2. **输入必须是结构化状态，不是原始中文句子**。
   实测（RTX 3060 / Qwen2.5-1.5B-RLCD，temp=0.2）：

   | 输入形式 | 命中 |
   |---|---|
   | 原始中文句子（「在吗」「你今天真好看」） | 差——中性句一律判「惊讶」，prob 还能到 1.0 |
   | 结构化状态（`emotion=happy, intensity=strong, cause=被夸奖`） | 6/9 命中 |

   情绪理解交给主 LLM（它本来就在读对话），本层只做
   **「情绪 → 具体动作」的翻译**。这也符合 `本地决策引擎接入计划` 里
   「本地引擎只做窄判断，中文长文一律回退」的铁律。

3. **概率不是置信度**（引擎 README 明示：normalized model scores）。
   实测中性句也能给出 prob=1.0。所以 `prob` **只用于相对排序**，
   绝对值必须过 :class:`FallbackGate` 的档位判断，低档一律不动作。

## 用法

    from core.expression_director import ExpressionDirector

    d = ExpressionDirector("miku")
    result = d.decide(emotion="happy", intensity="strong", cause="被夸奖")
    if result.accepted:
        renderer.trigger_gesture(result.gesture)      # 如 "grin"
        renderer.play_emote_sequence(result.preset)   # 如 "smile_bright"

失败时 `result.accepted=False`，`result.reason` 说明为什么——
调用方**什么都不做**即可（宁可不动，不能乱动）。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional

from core.capability_snapshot import (
    MAX_CHOICES_PER_FIELD,
    CapabilitySnapshot,
    build_snapshot,
    presets_for_emotion,
)

logger = logging.getLogger(__name__)

# 本地 RLCD 决策引擎地址（见 projects/local-rlcd）
DEFAULT_ENGINE_URL = os.environ.get("LOCAL_RLCD_BASE", "http://127.0.0.1:8077")

# 置信档位阈值。**不是校准概率**，是按实测分布自己调的（见模块 docstring 第 3 条）。
BAND_HIGH = float(os.environ.get("RLCD_CONF_HIGH", "0.85"))
BAND_LOW = float(os.environ.get("RLCD_CONF_LOW", "0.55"))

# 引擎默认超时（毫秒级响应，给足余量）
DEFAULT_TIMEOUT = float(os.environ.get("RLCD_TIMEOUT", "8"))

# 情绪词表（与 oc-pet 现有 _EMOTION_FACIAL_TARGETS / _VA_EMOTIONS 对齐）
EMOTION_CHOICES = ("开心", "害羞", "惊讶", "思考", "疑惑", "生气", "失落", "困", "平静")
INTENSITY_CHOICES = ("轻", "中", "强")

# 情绪 → 兜底预设（引擎不可用 / 低置信时用）。
# 这不是「决策」，是安全网——保证桌宠不会因为引擎挂了而彻底没有反应。
_FALLBACK_PRESET: dict[str, str] = {
    "开心": "smile_bright",
    "害羞": "blush_shy",
    "惊讶": "surprise_gasp",
    "思考": "think_look",
    "疑惑": "doubt",
    "生气": "angry_glare",
    "失落": "sad_droop",
    "困": "yawn",
    "平静": "smile_soft",
}

# 强度 → 动作放大系数（给调用方做幅度缩放）
INTENSITY_SCALE: dict[str, float] = {"轻": 0.5, "中": 0.75, "强": 1.0}


# ── 结果模型 ──────────────────────────────────────────────


@dataclass
class DecisionResult:
    """一次表达决策的结果。

    Attributes:
        accepted: 是否被采纳。False 时调用方**不要动作**。
        emotion: 决策出的情绪（原始输入情绪，或引擎修正后的）。
        intensity: 强度档位。
        gesture: 动作名（预设名或 motion 名，来自快照）。
        preset: 表情序列预设名（可播放；None 表示走 motion 路径）。
        scale: 幅度系数。
        bands: 各字段的置信档位。
        probs: 各字段的原始 prob（**仅供日志/调参**，不要当正确率）。
        reason: 拒绝原因 / 采纳说明。
        elapsed_ms: 决策耗时。
        source: "engine" | "fallback" | "none"。
    """

    accepted: bool = False
    emotion: str = ""
    intensity: str = ""
    gesture: Optional[str] = None
    preset: Optional[str] = None
    scale: float = 1.0
    bands: dict[str, str] = field(default_factory=dict)
    probs: dict[str, float] = field(default_factory=dict)
    reason: str = ""
    elapsed_ms: float = 0.0
    source: str = "none"

    def as_line(self) -> str:
        flag = "✓" if self.accepted else "✗"
        g = self.gesture or "-"
        return (f"{flag} {self.emotion}/{self.intensity} → {g}"
                f" (scale={self.scale:g}, src={self.source}, {self.elapsed_ms:.0f}ms)"
                + (f" | {self.reason}" if self.reason else ""))


# ── 本地引擎客户端 ────────────────────────────────────────


class LocalDecisionClient:
    """本地 RLCD 引擎的 HTTP 客户端（无第三方依赖，用 urllib）。

    引擎不可用时**不抛异常**，返回 None——调用方走兜底。
    """

    def __init__(self, base_url: str = DEFAULT_ENGINE_URL, timeout: float = DEFAULT_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._available: Optional[bool] = None
        self._last_check = 0.0

    def is_available(self, ttl: float = 30.0) -> bool:
        """探测引擎是否在线（结果缓存 ttl 秒，避免每次都打网络）。"""
        now = time.time()
        if self._available is not None and now - self._last_check < ttl:
            return self._available
        self._last_check = now
        try:
            req = urllib.request.Request(f"{self.base_url}/api/presets", method="GET")
            with urllib.request.urlopen(req, timeout=min(self.timeout, 3)) as resp:
                self._available = 200 <= resp.status < 300
        except Exception:
            self._available = False
        return self._available

    def decide(self, context: str, schema: dict, temperature: float = 0.2) -> Optional[dict]:
        """调用引擎做一次并行约束决策。

        Returns:
            ``{"emotion": {"value": ..., "prob": ...}, ...}``；失败返回 None。
        """
        payload = json.dumps(
            {"context": context, "schema": schema, "temperature": temperature},
            ensure_ascii=False,
        ).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/api/run-rlcd",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
            logger.debug("expression_director: 引擎调用失败: %s", e)
            return None
        except Exception as e:  # noqa: BLE001 — 引擎返回畸形 JSON 也不该拖垮桌宠
            logger.warning("expression_director: 引擎返回异常: %s", e)
            return None
        parsed = data.get("parsed_json")
        return parsed if isinstance(parsed, dict) else None


# ── 决策器 ────────────────────────────────────────────────


class ExpressionDirector:
    """把「情绪状态」翻译成「具体表情/动作」的决策器。

    Args:
        character_id: 角色目录名，如 ``"miku"``。
        client: 注入的引擎客户端（测试可传假实现）。
        enabled: 总开关。关掉后 :meth:`decide` 一律返回兜底结果。
        oc_pet_root: oc-pet 根目录（默认按本文件推断）。
    """

    def __init__(
        self,
        character_id: str,
        client: Optional[LocalDecisionClient] = None,
        enabled: bool = True,
        oc_pet_root: Optional[str] = None,
    ):
        self.character_id = character_id
        self.client = client or LocalDecisionClient()
        self._enabled = bool(enabled)
        self._lock = threading.Lock()
        self._snapshot: Optional[CapabilitySnapshot] = None
        self._snapshot_root = oc_pet_root
        # 统计（供状态口观测命中率）
        self.stats: dict[str, int] = {"total": 0, "accepted": 0, "fallback": 0, "none": 0}

    # ── 快照 ──────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)

    def snapshot(self, refresh: bool = False) -> CapabilitySnapshot:
        """取能力快照（懒建 + 缓存；换模型后调 refresh=True 重建）。"""
        with self._lock:
            if self._snapshot is None or refresh:
                self._snapshot = build_snapshot(self.character_id, self._snapshot_root)
            return self._snapshot

    # ── schema 构建 ───────────────────────────────────────

    def build_schema(self, emotion: str = "") -> dict:
        """按情绪构建决策 schema（**两阶段决策的第二阶段**）。

        ⚠️ 两条实测得出的硬约束（见模块 docstring）：
        1. 选项必须写进 description 的**第一行**——引擎只取第一行。
        2. 选项必须是**中文语义标签**，且**按情绪分组**（每组 ≤ 12 个）。
           53 个全给时命中率 6-8/9 且不稳定；分组后 10/10 且 prob 拉开差距。

        Args:
            emotion: 主 LLM 定下的情绪；为空时用全量候选（不推荐）。
        """
        snap = self.snapshot()
        emo = "、".join(EMOTION_CHOICES)
        schema: dict[str, Any] = {
            "emotion": {
                "type": "enum",
                "description": f"角色此刻最直接的情绪，从这些里选一个：{emo}",
                "choices": list(EMOTION_CHOICES),
            },
            "intensity": {
                "type": "enum",
                "description": "情绪强度。日常寒暄用轻，明确的情绪用中，强烈冲击才用强。"
                               "从这些里选一个：轻、中、强",
                "choices": list(INTENSITY_CHOICES),
            },
        }

        if not snap.presets:
            return schema

        candidates = presets_for_emotion(snap, emotion) if emotion else list(snap.presets)
        labels = [p.label for p in candidates]
        if len(labels) > MAX_CHOICES_PER_FIELD:
            logger.warning(
                "expression_director: 候选数 %d 超引擎上限 %d，已截断",
                len(labels), MAX_CHOICES_PER_FIELD,
            )
            labels = labels[:MAX_CHOICES_PER_FIELD]

        schema["preset"] = {
            "type": "enum",
            "description": "根据角色状态选最贴合的表演动作，从这些里选一个："
                           + "/".join(labels),
            "choices": labels,
        }
        return schema

    # ── 主决策 ────────────────────────────────────────────

    def decide(
        self,
        emotion: str,
        intensity: str = "中",
        cause: str = "",
        extra: Optional[dict] = None,
    ) -> DecisionResult:
        """做一次表达决策。

        Args:
            emotion: 主 LLM 判断出的情绪（中文词，如 ``"开心"``）。
            intensity: 强度档位（``"轻"`` / ``"中"`` / ``"强"``）。
            cause: 触发原因（简短，如 ``"被夸奖"``）。**必须短**——
                长文本会让引擎自信判错（见模块 docstring）。
            extra: 额外状态字段，拼进 context（如 ``{"screen": "在看代码"}``）。

        Returns:
            :class:`DecisionResult`。**永不抛异常**——最差返回兜底结果。
        """
        t0 = time.perf_counter()
        self.stats["total"] += 1

        # 1) 开关 / 可用性闸门 → 兜底
        if not self._enabled:
            return self._fallback(emotion, intensity, "决策器已关闭", t0)
        if not self.client.is_available():
            return self._fallback(emotion, intensity, "本地引擎未就绪", t0)

        # 2) 组装结构化状态（**不是原始句子**——这是实测得出的硬约束 2）
        parts = [f"emotion={emotion}", f"intensity={intensity}"]
        if cause:
            parts.append(f"cause={cause[:60]}")
        for k, v in (extra or {}).items():
            parts.append(f"{k}={str(v)[:40]}")
        context = ", ".join(parts)

        # 3) 调引擎（两阶段：先用情绪筛候选，再在小组里挑细节动作）
        schema = self.build_schema(emotion)
        parsed = self.client.decide(context, schema)
        elapsed = (time.perf_counter() - t0) * 1000

        if not parsed:
            return self._fallback(emotion, intensity, "引擎无响应", t0)

        # 4) 解析 + 档位判定
        fields: dict[str, tuple[Any, float, str]] = {}
        for name in schema:
            ans = parsed.get(name)
            if not isinstance(ans, dict) or ans.get("value") in (None, ""):
                continue
            prob = float(ans.get("prob") or 0.0)
            band = "high" if prob >= BAND_HIGH else ("medium" if prob >= BAND_LOW else "low")
            fields[name] = (ans["value"], prob, band)

        if not fields:
            return self._fallback(emotion, intensity, "引擎未返回有效字段", t0)

        bands = {k: v[2] for k, v in fields.items()}
        probs = {k: v[1] for k, v in fields.items()}

        # 5) 回退闸：**preset 低置信就不采用**（宁可不动，不能乱动）
        preset = fields.get("preset", (None, 0.0, "low"))
        preset_ok = preset[0] is not None and preset[2] != "low"

        emo_out = fields.get("emotion", (emotion, 0.0, "low"))[0] or emotion
        inten_out = fields.get("intensity", (intensity, 0.0, "low"))[0] or intensity

        if not preset_ok:
            reason = (f"preset 置信不足（{preset[2]}）"
                      if preset[0] is not None else "未产出 preset")
            res = self._fallback(emo_out, inten_out, reason, t0)
            res.bands = bands
            res.probs = probs
            return res

        # 中文标签 → 预设名（引擎返回的是标签，播的是预设）
        snap = self.snapshot()
        preset_obj = snap.preset_by_label(str(preset[0]))
        preset_name = preset_obj.name if preset_obj else str(preset[0])

        self.stats["accepted"] += 1
        result = DecisionResult(
            accepted=True,
            emotion=emo_out,
            intensity=inten_out,
            gesture=preset_name,
            preset=preset_name,
            scale=INTENSITY_SCALE.get(inten_out, 0.75),
            bands=bands,
            probs=probs,
            reason=f"引擎采纳（preset {preset[2]}，标签「{preset[0]}」）",
            elapsed_ms=elapsed,
            source="engine",
        )
        self._trace(result, cause)
        return result

    # ── 兜底 ──────────────────────────────────────────────

    def _fallback(self, emotion: str, intensity: str, reason: str, t0: float) -> DecisionResult:
        """引擎不可用 / 低置信时的安全网。

        不是「决策」——只是保证桌宠还有反应，且反应是**情绪一致的**。
        """
        self.stats["fallback"] += 1
        preset = _FALLBACK_PRESET.get(emotion)
        result = DecisionResult(
            accepted=bool(preset),
            emotion=emotion,
            intensity=intensity,
            gesture=preset,
            preset=preset,
            scale=INTENSITY_SCALE.get(intensity, 0.75),
            bands={},
            probs={},
            reason=reason,
            elapsed_ms=(time.perf_counter() - t0) * 1000,
            source="fallback" if preset else "none",
        )
        if not preset:
            self.stats["none"] += 1
        self._trace(result, "")
        return result

    # ── 可观测性 ──────────────────────────────────────────

    @staticmethod
    def _trace(result: DecisionResult, cause: str) -> None:
        """写入 decision_trace（没有可观测性，调表情就是抓瞎）。"""
        try:
            from avatar.decision_trace import trace

            trace.record(
                "expression",
                chosen=result.gesture or "(none)",
                source=result.source,
                rejected={k: v for k, v in result.bands.items() if v == "low"},
                note=f"{result.emotion}/{result.intensity}"
                     + (f" ← {cause}" if cause else "")
                     + (f" | {result.reason}" if result.reason else ""),
            )
        except Exception:
            logger.debug("expression_director: trace 记录失败（忽略）", exc_info=True)

    def status(self) -> dict:
        """供状态口 / 调试展示。"""
        snap = self.snapshot()
        total = max(1, self.stats["total"])
        return {
            "enabled": self._enabled,
            "engine_available": self.client.is_available(),
            "character": self.character_id,
            "presets": len(snap.presets),
            "motions": len(snap.motions),
            "param_ranges": len(snap.param_ranges),
            "warnings": snap.warnings,
            "stats": dict(self.stats),
            "accept_rate": round(self.stats["accepted"] / total, 3),
        }


# ── 进程级缓存 ────────────────────────────────────────────

_directors: dict[str, ExpressionDirector] = {}
_directors_lock = threading.Lock()


def get_director(character_id: str, **kwargs) -> ExpressionDirector:
    """取角色对应的决策器（进程级缓存，同一角色只建一次）。"""
    with _directors_lock:
        d = _directors.get(character_id)
        if d is None:
            d = ExpressionDirector(character_id, **kwargs)
            _directors[character_id] = d
        return d


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    cid = sys.argv[1] if len(sys.argv) > 1 else "miku"
    d = ExpressionDirector(cid)

    print("── 引擎状态 ──")
    print(json.dumps(d.status(), ensure_ascii=False, indent=2))

    print("\n── 决策实测 ──")
    cases = [
        ("开心", "强", "被夸奖"),
        ("开心", "轻", "日常寒暄"),
        ("害羞", "强", "被盯着看"),
        ("失落", "中", "工作受挫"),
        ("思考", "轻", "被问问题"),
        ("惊讶", "强", "意外成功"),
        ("困", "强", "凌晨三点"),
        ("生气", "中", "被打扰"),
        ("平静", "轻", "用户在打字"),
    ]
    for emo, inten, cause in cases:
        r = d.decide(emo, inten, cause)
        print(f"  {r.as_line()}")
