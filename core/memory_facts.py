# -*- coding: utf-8 -*-
"""FactStore — 事实库（P1-2，N.E.K.O. memory/facts.py + evidence.py + fact_dedup.py 参考重写）

桌宠的长期"事实记忆"：从对话/事件文本中用 LLM（Hanako 通道，source="memory_extract"）
抽取值得记住的事实（用户偏好/身份/重要事件/关系），经**本地去重**后持久化，并附
**证据引用**（哪条事件/哪段原文支撑了这条事实）。

规模缩小到单机桌宠：N.E.K.O. 是 asyncio + 角色级目录 + 本地 ONNX embedding 做余弦
去重；oc-pet 改为 threading + ``~/.oc-pet/memory/<agent_id>_facts.json`` + 字符 n-gram
相似度去重（无 embedding 依赖，P1-1 落地后可升级为向量余弦，参考
``third_party_reference/neko/memory/fact_dedup.py`` 的 0.85 阈值思路）。

设计要点：
- **抽取失败不阻塞**：LLM 超时/无通道/解析失败 → 记日志并跳过，绝不抛异常打扰主链路。
- **去重（本地，无 LLM）**：① 归一化精确匹配（繁简折叠 + 去标点空白 + 小写）；
  ② n-gram Jaccard / 覆盖率相似度（中文友好，2/3-gram 由 ``core.memory_keywords``
  提供）。同事实不同表述 → 合并：提升 importance、累计 reinforcement、追加证据。
- **证据（evidence）**：简化自 N.E.K.O. ``memory/evidence.py`` 的"reinforcement /
  disputation 双时钟衰减 + 派生状态"。``confidence`` 由证据强度 sigmoid 映射；
  ``status`` 派生为 pending/confirmed/promoted/archive_candidate。
- **线程约束（0x8001010D 教训）**：LLM 调用在后台 daemon 线程执行，结果经 Qt Signal
  回主线程再通知 UI；无 Qt 环境（headless 单测）退化为同步路径。
- **持久化**：``{"version": 2, "meta": {...}, "facts": [...]}``；v1 裸数组/缺字段旧文件
  读取时自动归一，不崩。

用法：
    store = FactStore("miku")
    store.record_text("我喜欢在深夜写代码", extra_context="对话", evidence={"source": "conversation", "ts": 123.0})
    hits = store.search("深夜")
"""
from __future__ import annotations

import json
import logging
import math
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

DEFAULT_MEMORY_DIR = Path.home() / ".oc-pet" / "memory"

# 事实文本长度上限（超长截断，防噪声）
FACT_MAX_TEXT_CHARS = 200
# 单条事实最多保留的证据引用数
FACT_EVIDENCE_LIMIT = 8
# 事实库总条数上限（超出时优先裁剪最早 archive_candidate）
FACT_MAX_FACTS = 2000
FACTS_FILE_VERSION = 2

# 证据数学阈值（简化自 N.E.K.O. memory/evidence.py：RFC §3.1.4 分档）
EVIDENCE_PROMOTED_THRESHOLD = 1.5
EVIDENCE_CONFIRMED_THRESHOLD = 0.8
EVIDENCE_ARCHIVE_THRESHOLD = -0.5
EVIDENCE_REIN_HALF_LIFE_DAYS = 30.0
EVIDENCE_DISP_HALF_LIFE_DAYS = 14.0

# importance → 初始 reinforcement 种子（N.E.K.O. evidence.initial_reinforcement_from_importance）
_IMPORTANCE_TO_INITIAL_REIN: tuple[tuple[int, float], ...] = (
    (10, 0.8),
    (9, 0.6),
    (8, 0.4),
    (7, 0.2),
)

DEFAULT_FACT_IMPORTANCE = 5
SOURCE_MEMORY_EXTRACT = "memory_extract"

# 去重阈值：n-gram Jaccard（"不同表述"命中线）。N.E.K.O. 用 embedding cosine 0.85，
# 这里退化为字符 n-gram——阈值按中文短句经验校准（0.75 能命中"主人喜欢喝咖啡" vs
# "主人喜欢喝拿铁咖啡"，0.25 能放过"主人喜欢猫" vs "主人讨厌猫"）。
DEDUP_JACCARD_THRESHOLD = 0.75
# 覆盖率（candidate token 命中 existing 的比例）兜底：短表述是长表述的子集时命中
DEDUP_COVERAGE_THRESHOLD = 0.70

_WS_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[，。！？；：、,.!?;:()（）\[\]【】\"'“”‘’\-—_/\\]+")


# ── 时间/工具 ───────────────────────────────────────────────────────


def _now_iso() -> str:
    """当前 UTC 时间的 ISO 字符串（无时区歧义）。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_fact_id() -> str:
    return f"f_{uuid.uuid4().hex[:10]}"


def _iso_to_ts(iso: str | None, fallback: float = 0.0) -> float:
    """ISO 时间字符串 → epoch 秒；非法/空 → fallback。"""
    if not iso:
        return fallback
    try:
        parsed = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        return parsed.timestamp()
    except (ValueError, TypeError, OverflowError):
        return fallback


def _age_days(ts_iso: str | None, now: datetime) -> float:
    """ISO 时间戳距今的天数；空/非法/未来 → 0（不衰减）。"""
    if not ts_iso:
        return 0.0
    try:
        parsed = datetime.fromisoformat(str(ts_iso).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        delta = (now - parsed).total_seconds()
        if delta <= 0:
            return 0.0
        return delta / 86400.0
    except (ValueError, TypeError, OverflowError):
        return 0.0


def initial_reinforcement_from_importance(max_importance: int) -> float:
    """importance → 初始 reinforcement 种子（N.E.K.O. evidence.py 简化）。
    高 importance 事实（昵称/身份/用户明确请记住）给初始鼓励，穿越状态机更快。"""
    try:
        imp = int(max_importance)
    except (TypeError, ValueError):
        return 0.0
    for threshold, seed in _IMPORTANCE_TO_INITIAL_REIN:
        if imp >= threshold:
            return seed
    return 0.0


def effective_reinforcement(entry: dict, now: datetime) -> float:
    """按 rein 时钟衰减后的强化值。"""
    r = float(entry.get("reinforcement", 0.0) or 0.0)
    if r == 0.0:
        return 0.0
    age = _age_days(entry.get("rein_last_signal_at"), now)
    return r * (0.5 ** (age / EVIDENCE_REIN_HALF_LIFE_DAYS))


def effective_disputation(entry: dict, now: datetime) -> float:
    """按 disp 时钟衰减后的质疑值。"""
    d = float(entry.get("disputation", 0.0) or 0.0)
    if d == 0.0:
        return 0.0
    age = _age_days(entry.get("disp_last_signal_at"), now)
    return d * (0.5 ** (age / EVIDENCE_DISP_HALF_LIFE_DAYS))


def evidence_score(entry: dict, now: datetime | None = None) -> float:
    """证据净强度（+rein -disp）；protected 恒 inf（永不裁剪）。"""
    now = now or datetime.now(timezone.utc)
    if entry.get("protected"):
        return float("inf")
    return effective_reinforcement(entry, now) - effective_disputation(entry, now)


def derive_status(entry: dict, now: datetime | None = None) -> str:
    """evidence_score → 派生状态：promoted/confirmed/pending/archive_candidate。"""
    s = evidence_score(entry, now)
    if s >= EVIDENCE_PROMOTED_THRESHOLD:
        return "promoted"
    if s >= EVIDENCE_CONFIRMED_THRESHOLD:
        return "confirmed"
    if s <= EVIDENCE_ARCHIVE_THRESHOLD:
        return "archive_candidate"
    return "pending"


def fact_confidence(entry: dict, now: datetime | None = None) -> float:
    """0~1 置信度：evidence_score 经 sigmoid 映射（未有任何信号 → 0.5）。"""
    if entry.get("protected"):
        return 1.0
    s = evidence_score(entry, now)
    conf = 1.0 / (1.0 + math.exp(-s))
    return max(0.05, min(0.99, conf))


# ── 文本归一化 / 相似度（本地去重核心）────────────────────────────────


def normalize_fact_text(text: str) -> str:
    """归一化事实文本：繁转简 + 去空白/标点 + 小写（用于精确去重比较）。"""
    try:
        from core.memory_keywords import fold_script
        folded = fold_script(str(text or ""))
    except Exception:
        folded = str(text or "")
    folded = _WS_RE.sub("", folded)
    folded = _PUNCT_RE.sub("", folded)
    return folded.strip().lower()


def _char_ngrams(text: str, n: int = 2) -> set[str]:
    """字符 n-gram 兜底分词（memory_keywords 不可用时）。"""
    cleaned = _WS_RE.sub("", str(text or ""))
    if len(cleaned) <= n:
        return {cleaned} if cleaned else set()
    return {cleaned[i:i + n] for i in range(len(cleaned) - n + 1)}


def _tokenize(text: str) -> set[str]:
    """事实文本 → token 集合：优先 core.memory_keywords（CJK 2/3-gram + 繁简折叠），
    兜底字符 bigram。"""
    try:
        from core.memory_keywords import tokenize
        tokens = tokenize(str(text or ""))
        return {str(t) for t in tokens if str(t).strip()}
    except Exception:
        return _char_ngrams(str(text or ""), n=2)


def text_similarity(a: str, b: str) -> dict:
    """两段文本的 n-gram 相似度。返回 {"jaccard", "coverage"}：
    - jaccard: |A∩B| / |A∪B|
    - coverage: |A∩B| / |A|（A 对 B 的覆盖率，子句命中）
    """
    ta = _tokenize(a)
    tb = _tokenize(b)
    if not ta or not tb:
        return {"jaccard": 0.0, "coverage": 0.0}
    inter = ta & tb
    union = ta | tb
    return {
        "jaccard": len(inter) / len(union),
        "coverage": len(inter) / len(ta),
    }


# ── LLM 抽取 prompt / 解析 ───────────────────────────────────────────


def build_extract_prompt(text: str, extra_context: str = "") -> str:
    """构造事实抽取 LLM 指令（作为 user 消息发给适配器）。"""
    lines = [
        "你是记忆抽取助手。从以下文本中提取值得长期记住的事实（用户偏好、身份信息、重要事件、人际关系等）。",
        "只输出 JSON 数组，不要输出解释或多余文字。每个元素格式：",
        '{"text": "事实文本（一句话，≤60字）", "importance": 1到10的整数, "category": "偏好/身份/事件/关系/其他 之一"}。',
        "没有值得记住的事实就输出 []。",
    ]
    if extra_context:
        lines.append(f"背景：{extra_context}")
    lines.append("文本：")
    lines.append(str(text)[:2000])
    return "\n".join(lines)


def parse_extracted_facts(raw: str, source: str = SOURCE_MEMORY_EXTRACT) -> list[dict]:
    """解析 LLM 输出的 JSON 数组 → fact dict 列表（防御式，坏片段丢弃）。

    Returns:
        事实列表：[{"text", "importance", "category", "source"}]；无有效内容 → []。
    """
    if not raw or not str(raw).strip():
        return []
    raw = str(raw).strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    data: Any = None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("[")
        end = raw.rfind("]")
        if start != -1 and end > start:
            try:
                data = json.loads(raw[start:end + 1])
            except json.JSONDecodeError:
                return []
        else:
            return []
    if not isinstance(data, list):
        return []
    facts: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text or len(text) > FACT_MAX_TEXT_CHARS:
            continue
        try:
            importance = int(item.get("importance", DEFAULT_FACT_IMPORTANCE)
                             or DEFAULT_FACT_IMPORTANCE)
        except (TypeError, ValueError):
            importance = DEFAULT_FACT_IMPORTANCE
        importance = max(1, min(10, importance))
        category = str(item.get("category") or "其他").strip() or "其他"
        facts.append({
            "text": text,
            "importance": importance,
            "category": category,
            "source": source,
        })
    return facts


# ── FactStore ────────────────────────────────────────────────────────


class FactStore:
    """事实库：LLM 抽取 → 本地去重 → 证据引用 → 持久化 → 查询。

    Args:
        agent_id: 桌宠/角色 id（事实文件按 agent 分实例）
        memory_dir: 记忆目录；缺省 ~/.oc-pet/memory
        filename: 覆盖文件名（默认 ``<agent_id>_facts.json``）
        llm_fn: 可选注入的同步抽取函数 ``fn(prompt: str) -> str | None``
            （单测/无适配器环境）
        adapter: HanakoPetAdapter（或实现 ``chat(message, inject_memory,
            extra_context, source) -> (text, emotion)`` 的对象）
        dedup_threshold: n-gram Jaccard 去重阈值（默认 0.75）
        use_qt_bridge: 后台线程结果是否经 Qt Signal 回主线程（headless 关闭）
    """

    def __init__(
        self,
        agent_id: str = "default",
        memory_dir: str | Path | None = None,
        filename: str | None = None,
        llm_fn: Callable[[str], str | None] | None = None,
        adapter: Any = None,
        dedup_threshold: float | None = None,
        use_qt_bridge: bool = True,
    ):
        self._agent_id = agent_id or "default"
        self._dir = Path(memory_dir) if memory_dir else DEFAULT_MEMORY_DIR
        self._path = self._dir / (filename or f"{agent_id}_facts.json")
        self._llm_fn = llm_fn
        self._adapter = adapter
        if dedup_threshold is None:
            # 默认从配置读取（memory.facts.dedup_threshold），缺省 DEDUP_JACCARD_THRESHOLD
            try:
                from config import load_config
                threshold = load_config().get("memory", {}).get("facts", {}).get("dedup_threshold")
                if threshold:
                    dedup_threshold = float(threshold)
            except Exception:
                logger.debug("memory_facts: 非致命异常(已静默吞掉)", exc_info=True)
        self._dedup_jaccard = float(dedup_threshold) if dedup_threshold else DEDUP_JACCARD_THRESHOLD
        self._lock = threading.Lock()
        self._facts: list[dict] = []
        self._meta: dict = {}
        self._changed_callbacks: list[Callable[[dict], None]] = []
        self._bridge = None
        if use_qt_bridge:
            self._setup_bridge()
        self.load()

    # ── Qt 桥（后台 LLM → 主线程通知；0x8001010D 约束）──────────────

    def _setup_bridge(self) -> None:
        """创建 QObject 信号桥；无 PySide6 时退化为 None（同步路径）。"""
        try:
            from PySide6.QtCore import QObject, Signal

            class _Bridge(QObject):
                extracted = Signal(str)  # JSON payload：add_facts 结果

            self._bridge = _Bridge()
            self._bridge.extracted.connect(self._on_extracted)
        except Exception as exc:  # 无 Qt / 无 PySide6 → 同步路径
            logger.debug("FactStore Qt bridge unavailable, use sync path: %s", exc)
            self._bridge = None

    def _on_extracted(self, payload: str) -> None:
        """主线程槽：解析后台抽取结果并通知监听者（不触碰 Qt/COM 之外的 UI）。"""
        try:
            result = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            result = {"added": 0, "merged": 0, "skipped": 0}
        self._notify_changed(result)

    def set_changed_callback(self, callback: Callable[[dict], None] | None) -> None:
        """注册事实库变化通知（主线程回调；result: {"added","merged","skipped"}）。"""
        if callback is None:
            self._changed_callbacks = []
            return
        if callback not in self._changed_callbacks:
            self._changed_callbacks.append(callback)

    def _notify_changed(self, result: dict) -> None:
        for cb in list(self._changed_callbacks):
            try:
                cb(result)
            except Exception as exc:  # 通知失败不影响主链路
                logger.error("FactStore changed callback error: %s", exc)

    def shutdown(self) -> None:
        """清理回调引用（进程退出时调用）。"""
        self._changed_callbacks = []

    # ── 路径/属性 ──

    @property
    def path(self) -> Path:
        return self._path

    @property
    def agent_id(self) -> str:
        return self._agent_id

    @property
    def facts(self) -> list[dict]:
        with self._lock:
            return [dict(f) for f in self._facts]

    @property
    def meta(self) -> dict:
        return dict(self._meta)

    # ── 持久化 ──

    def load(self) -> None:
        """从磁盘加载；文件不存在/损坏 → 空档（不崩）。"""
        try:
            if not self._path.exists():
                self._facts = []
                self._meta = {}
                return
            data = json.loads(self._path.read_text(encoding="utf-8"))
            self._load_data(data)
        except Exception as exc:
            logger.warning("FactStore 加载失败（用空档）: %s", exc)
            self._facts = []
            self._meta = {}

    def _load_data(self, data: Any) -> None:
        """兼容 v1（裸数组）与 v2（{"facts": [...]}）两种格式。"""
        now = datetime.now(timezone.utc)
        if isinstance(data, list):
            raw_facts = data
            self._meta = {}
        elif isinstance(data, dict):
            self._meta = dict(data.get("meta") or {})
            raw_facts = data.get("facts") or []
        else:
            raw_facts = []
        facts: list[dict] = []
        for f in raw_facts:
            if not isinstance(f, dict):
                continue
            entry = self._normalize_entry(f, now)
            if not entry.get("text"):
                continue
            facts.append(entry)
        self._facts = facts

    @staticmethod
    def _normalize_entry(f: dict, now: datetime) -> dict:
        """旧文件兼容：缺字段补默认值，confidence/status 重新派生。"""
        entry = dict(f)
        entry.setdefault("id", _new_fact_id())
        entry.setdefault("text", "")
        entry.setdefault("importance", DEFAULT_FACT_IMPORTANCE)
        entry.setdefault("category", "其他")
        entry.setdefault("subject", "")
        entry.setdefault("source", SOURCE_MEMORY_EXTRACT)
        entry.setdefault("protected", False)
        entry.setdefault("reinforcement", 0.0)
        entry.setdefault("disputation", 0.0)
        entry.setdefault("rein_last_signal_at", "")
        entry.setdefault("disp_last_signal_at", "")
        entry.setdefault("user_fact_reinforce_count", 0)
        entry.setdefault("sub_zero_days", 0)
        entry.setdefault("evidence", [])
        entry.setdefault("created_at", entry.get("updated_at") or _now_iso())
        entry.setdefault("created_ts", _iso_to_ts(entry.get("created_at"), now.timestamp()))
        entry.setdefault("updated_at", entry.get("created_at"))
        entry.setdefault("updated_ts", _iso_to_ts(entry.get("updated_at"), now.timestamp()))
        entry.setdefault("last_signal_at", entry.get("created_at"))
        entry["confidence"] = fact_confidence(entry, now)
        entry["status"] = derive_status(entry, now)
        return entry

    def save(self) -> bool:
        """显式写盘（幂等）。"""
        with self._lock:
            return self._save_locked()

    def _save_locked(self) -> bool:
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            data = {
                "version": FACTS_FILE_VERSION,
                "agent_id": self._agent_id,
                "updated_at": _now_iso(),
                "meta": self._meta,
                "facts": self._facts,
            }
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self._path)
            return True
        except Exception as exc:
            logger.warning("FactStore 保存失败: %s", exc)
            return False

    # ── 去重 ──

    def _find_duplicate(self, candidate: dict) -> dict | None:
        """本地去重：① 归一化精确匹配；② n-gram Jaccard/覆盖率相似度（双向取强）。

        同事实不同表述（"主人喜欢喝咖啡" vs "主人喜欢喝拿铁咖啡"）→ 命中合并；
        反义事实（"主人喜欢猫" vs "主人讨厌猫"）→ 不命中。
        """
        cand_text = str(candidate.get("text") or "")
        cand_norm = normalize_fact_text(cand_text)
        if not cand_norm:
            return None
        for existing in self._facts:
            existing_norm = normalize_fact_text(existing.get("text", ""))
            if existing_norm and existing_norm == cand_norm:
                return existing
        for existing in self._facts:
            sim = text_similarity(cand_text, existing.get("text", ""))
            rev = text_similarity(existing.get("text", ""), cand_text)
            jaccard = max(sim["jaccard"], rev["jaccard"])
            coverage = max(sim["coverage"], rev["coverage"])
            if jaccard >= self._dedup_jaccard or coverage >= DEDUP_COVERAGE_THRESHOLD:
                return existing
        return None

    # ── 写入 ──

    def add_facts(self, facts: list[dict], evidence: list[dict] | None = None,
                  source: str = SOURCE_MEMORY_EXTRACT) -> dict:
        """去重后写入事实库。

        Args:
            facts: 事实 dict 列表（text 必填；importance/category 可选）
            evidence: 证据引用列表 [{"event_id", "ts", "source", "quote"}]，
                附加到**每条**新事实/被合并事实
            source: 来源标记（默认 "memory_extract"）

        Returns:
            {"added": 新事实数, "merged": 命中重复合并数, "skipped": 无效条数}
        """
        if not facts:
            return {"added": 0, "merged": 0, "skipped": 0}
        now = datetime.now(timezone.utc)
        now_iso = _now_iso()
        result = {"added": 0, "merged": 0, "skipped": 0}
        with self._lock:
            for cand in facts:
                if not isinstance(cand, dict):
                    result["skipped"] += 1
                    continue
                text = str(cand.get("text") or "").strip()
                if not text:
                    result["skipped"] += 1
                    continue
                dup = self._find_duplicate(cand)
                if dup is not None:
                    self._merge_into(dup, cand, evidence, now_iso)
                    result["merged"] += 1
                    continue
                self._facts.append(self._new_fact(cand, evidence, source, now_iso, now))
                result["added"] += 1
            self._prune_archived_locked()
            self._save_locked()
        return result

    @staticmethod
    def _append_evidence(entry: dict, ev: dict) -> None:
        """向事实追加一条证据引用（保持上限 FACT_EVIDENCE_LIMIT）。"""
        evidence = list(entry.get("evidence") or [])
        evidence.append({
            "event_id": str(ev.get("event_id") or ""),
            "ts": float(ev.get("ts") or 0.0),
            "source": str(ev.get("source") or ""),
            "quote": str(ev.get("quote") or "")[:80],
        })
        entry["evidence"] = evidence[-FACT_EVIDENCE_LIMIT:]

    def _new_fact(self, cand: dict, evidence: list[dict] | None,
                  source: str, now_iso: str, now: datetime) -> dict:
        """由抽取结果构造一条事实条目（含初始 reinforcement 与证据引用）。"""
        importance = max(1, min(10, int(cand.get("importance", DEFAULT_FACT_IMPORTANCE)
                                        or DEFAULT_FACT_IMPORTANCE)))
        rein = initial_reinforcement_from_importance(importance)
        entry = {
            "id": _new_fact_id(),
            "text": str(cand.get("text") or "").strip(),
            "importance": importance,
            "category": str(cand.get("category") or "其他").strip() or "其他",
            "subject": str(cand.get("subject") or "").strip(),
            "confidence": 0.0,  # 稍后按证据推导
            "status": "pending",
            "source": str(cand.get("source") or source or SOURCE_MEMORY_EXTRACT),
            "protected": bool(cand.get("protected")),
            "reinforcement": rein,
            "disputation": 0.0,
            "rein_last_signal_at": now_iso,
            "disp_last_signal_at": "",
            "user_fact_reinforce_count": 0,
            "sub_zero_days": 0,
            "evidence": [],
            "created_at": now_iso,
            "created_ts": now.timestamp(),
            "updated_at": now_iso,
            "updated_ts": now.timestamp(),
            "last_signal_at": now_iso,
        }
        for ev in (evidence or []):
            self._append_evidence(entry, ev)
        entry["confidence"] = fact_confidence(entry, now)
        entry["status"] = derive_status(entry, now)
        return entry

    def _merge_into(self, existing: dict, candidate: dict,
                    evidence: list[dict] | None, now_iso: str) -> None:
        """同事实合并：取更高 importance、更长/更新表述、累计 reinforcement、追加证据。"""
        now = datetime.now(timezone.utc)
        existing["importance"] = max(
            int(existing.get("importance", DEFAULT_FACT_IMPORTANCE) or DEFAULT_FACT_IMPORTANCE),
            int(candidate.get("importance", DEFAULT_FACT_IMPORTANCE) or DEFAULT_FACT_IMPORTANCE),
        )
        cand_text = str(candidate.get("text") or "").strip()
        if len(cand_text) > len(str(existing.get("text") or "")):
            existing["text"] = cand_text
        # 重申强化：每次命中重复给 +0.5 reinforcement（简化 N.E.K.O. user_fact combo）
        existing["reinforcement"] = float(existing.get("reinforcement", 0.0) or 0.0) + 0.5
        existing["rein_last_signal_at"] = now_iso
        existing["user_fact_reinforce_count"] = int(existing.get("user_fact_reinforce_count", 0) or 0) + 1
        existing["updated_at"] = now_iso
        existing["updated_ts"] = now.timestamp()
        existing["last_signal_at"] = now_iso
        for ev in (evidence or []):
            self._append_evidence(existing, ev)
        existing["confidence"] = fact_confidence(existing, now)
        existing["status"] = derive_status(existing, now)

    def _prune_archived_locked(self) -> None:
        """超上限时优先裁剪最早 archive_candidate（protected 永不裁剪）。"""
        if len(self._facts) <= FACT_MAX_FACTS:
            return
        victims = [
            f for f in self._facts
            if f.get("status") == "archive_candidate" and not f.get("protected")
        ]
        victims.sort(key=lambda f: f.get("created_ts", 0.0))
        overflow = len(self._facts) - FACT_MAX_FACTS
        drop_ids = {f["id"] for f in victims[:overflow]}
        if not drop_ids:
            return
        self._facts = [f for f in self._facts if f.get("id") not in drop_ids]
        logger.info("FactStore 裁剪 %d 条 archive_candidate", len(drop_ids))

    # ── 证据信号（用户确认/否认 → 强化/质疑）──────────────────────

    def apply_signal(self, fact_id: str, reinforcement: float = 0.0,
                     disputation: float = 0.0, source: str = "") -> bool:
        """给事实施加证据信号（reinforce/dispute）。返回是否命中该事实。

        用法：用户确认"对，我喜欢这样" → apply_signal(fid, reinforcement=1.0, source="user_fact")；
        用户否认 → apply_signal(fid, disputation=1.0)。
        """
        with self._lock:
            for f in self._facts:
                if f.get("id") == fact_id:
                    self._apply_signal_locked(f, reinforcement, disputation, source, _now_iso())
                    self._save_locked()
                    return True
        return False

    @staticmethod
    def _apply_signal_locked(entry: dict, rein_delta: float, disp_delta: float,
                             source: str, now_iso: str) -> None:
        """证据信号应用：rein/disp 独立时钟；user_fact 强化有 combo 加成。"""
        now = datetime.now(timezone.utc)
        rein = float(rein_delta or 0.0)
        disp = float(disp_delta or 0.0)
        if rein != 0.0:
            entry["reinforcement"] = float(entry.get("reinforcement", 0.0) or 0.0) + rein
            entry["rein_last_signal_at"] = now_iso
        if disp != 0.0:
            entry["disputation"] = max(0.0, float(entry.get("disputation", 0.0) or 0.0) + disp)
            entry["disp_last_signal_at"] = now_iso
        if source == "user_fact" and rein > 0:
            entry["user_fact_reinforce_count"] = int(entry.get("user_fact_reinforce_count", 0) or 0) + 1
            if entry["user_fact_reinforce_count"] > 2:
                entry["reinforcement"] = float(entry.get("reinforcement", 0.0) or 0.0) + 0.5
        entry["updated_at"] = now_iso
        entry["updated_ts"] = now.timestamp()
        entry["last_signal_at"] = now_iso
        entry["confidence"] = fact_confidence(entry, now)
        entry["status"] = derive_status(entry, now)

    # ── LLM 抽取入口 ──

    def extract_facts(self, text: str, extra_context: str = "") -> list[dict]:
        """同步调用 LLM 抽取事实；失败/无通道 → []（跳过不阻塞，只记日志）。"""
        if self._llm_fn is not None:
            try:
                raw = self._llm_fn(build_extract_prompt(text, extra_context))
                return parse_extracted_facts(raw or "")
            except Exception as exc:
                logger.warning("[FactStore] LLM 抽取失败（跳过）: %s", exc)
                return []
        if self._adapter is None:
            logger.info("[FactStore] 无 LLM 通道，跳过事实抽取")
            return []
        prompt = build_extract_prompt(text, extra_context)
        try:
            reply, _emotion = self._adapter.chat(
                prompt,
                inject_memory=False,
                extra_context=extra_context,
                source=SOURCE_MEMORY_EXTRACT,
            )
        except Exception as exc:
            logger.warning("[FactStore] LLM 抽取异常（跳过）: %s", exc)
            return []
        return parse_extracted_facts(reply or "")

    def record_text_sync(self, text: str, extra_context: str = "",
                         evidence: list[dict] | None = None) -> dict:
        """同步：抽取 + 去重 + 入库（headless 单测 / 无 Qt 环境路径）。

        Returns:
            {"added", "merged", "skipped"}；LLM 失败时三者全 0（不阻塞）。
        """
        if not text or not str(text).strip():
            return {"added": 0, "merged": 0, "skipped": 0}
        facts = self.extract_facts(text, extra_context)
        return self.add_facts(facts, evidence=evidence, source=SOURCE_MEMORY_EXTRACT)

    def record_text(self, text: str, extra_context: str = "",
                    evidence: list[dict] | None = None) -> bool:
        """异步：后台线程 LLM 抽取 → 去重入库 → Qt 信号回主线程通知。

        Returns:
            True=已调度后台线程；False=退化为同步执行（headless）。
        """
        if self._bridge is None:
            self.record_text_sync(text, extra_context, evidence)
            return False
        threading.Thread(
            target=self._extract_worker,
            args=(text, extra_context, evidence),
            daemon=True,
            name="fact-extract",
        ).start()
        return True

    def _extract_worker(self, text: str, extra_context: str,
                        evidence: list[dict] | None) -> None:
        """后台线程：抽取 + 入库（文件 I/O，安全），结果经信号回主线程。"""
        try:
            result = self.record_text_sync(text, extra_context, evidence)
        except Exception as exc:  # 防御：后台绝不抛到线程外
            logger.warning("[FactStore] 后台抽取异常: %s", exc)
            result = {"added": 0, "merged": 0, "skipped": 0}
        self._bridge.extracted.emit(json.dumps(result, ensure_ascii=False))

    # ── 查询接口 ──

    def search(self, query: str, limit: int = 10) -> list[dict]:
        """按关键词检索事实（token 重叠度排序；archive_candidate 不参与）。"""
        if not query or not self._facts:
            return []
        q_tokens = _tokenize(query)
        scored: list[tuple[float, dict]] = []
        for f in self._facts:
            if f.get("status") == "archive_candidate":
                continue
            tokens = _tokenize(f.get("text", ""))
            inter = q_tokens & tokens
            if not inter:
                continue
            score = len(inter) / max(1, len(q_tokens))
            hay = " ".join(str(x) for x in (f.get("category"), f.get("subject"), f.get("text")))
            if str(query).lower() in hay.lower():
                score += 0.5
            scored.append((score, f))
        scored.sort(key=lambda x: (-x[0], -float(x[1].get("created_ts", 0.0))))
        return [f for _s, f in scored[:limit]]

    def by_topic(self, topic: str, limit: int = 10) -> list[dict]:
        """按主题过滤（category/subject/text 包含匹配，最新在前）。"""
        if not topic:
            return []
        topic_l = str(topic).strip().lower()
        hits = []
        for f in self._facts:
            if f.get("status") == "archive_candidate":
                continue
            hay = " ".join(str(x) for x in (f.get("category"), f.get("subject"), f.get("text")))
            if topic_l and topic_l in hay.lower():
                hits.append(f)
        hits.sort(key=lambda f: -float(f.get("created_ts", 0.0)))
        return hits[:limit]

    def by_time(self, start_ts: float = 0.0, end_ts: float = 0.0,
                limit: int = 50) -> list[dict]:
        """按创建时间范围过滤（闭区间，最新在前）。"""
        try:
            start = float(start_ts or 0.0)
        except (TypeError, ValueError):
            start = 0.0
        try:
            end = float(end_ts or 0.0)
        except (TypeError, ValueError):
            end = 0.0
        if end and end < start:
            start, end = end, start
        hits = []
        for f in self._facts:
            ts = float(f.get("created_ts", 0.0) or 0.0)
            if ts >= start and (not end or ts <= end):
                hits.append(f)
        hits.sort(key=lambda f: -float(f.get("created_ts", 0.0)))
        return hits[:limit]

    def by_confidence(self, min_confidence: float = 0.0, limit: int = 50) -> list[dict]:
        """按置信度过滤（≥min_confidence，倒序；archive_candidate 不参与）。"""
        try:
            min_conf = float(min_confidence or 0.0)
        except (TypeError, ValueError):
            min_conf = 0.0
        hits = [
            f for f in self._facts
            if f.get("status") != "archive_candidate"
            and float(f.get("confidence", 0.0) or 0.0) >= min_conf
        ]
        hits.sort(key=lambda f: -float(f.get("confidence", 0.0)))
        return hits[:limit]

    def get_facts(self, status: str | None = None, limit: int = 100) -> list[dict]:
        """取事实列表（可按状态过滤，最新在前）。"""
        hits = list(self._facts)
        if status:
            hits = [f for f in hits if f.get("status") == status]
        hits.sort(key=lambda f: -float(f.get("created_ts", 0.0)))
        return hits[:limit]

    def stats(self) -> dict:
        """统计：总数 + 按状态分布。"""
        counts: dict[str, int] = {}
        for f in self._facts:
            s = f.get("status") or "pending"
            counts[s] = counts.get(s, 0) + 1
        return {"total": len(self._facts), "by_status": counts}


__all__ = [
    "FactStore",
    "DEFAULT_MEMORY_DIR",
    "DEFAULT_FACT_IMPORTANCE",
    "SOURCE_MEMORY_EXTRACT",
    "DEDUP_JACCARD_THRESHOLD",
    "DEDUP_COVERAGE_THRESHOLD",
    "FACT_MAX_TEXT_CHARS",
    "normalize_fact_text",
    "text_similarity",
    "build_extract_prompt",
    "parse_extracted_facts",
    "evidence_score",
    "derive_status",
    "fact_confidence",
    "initial_reinforcement_from_importance",
]
