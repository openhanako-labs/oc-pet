"""影子模式决策记录器 —— 本地引擎并行判断「这条主动搭话值不值得花一次 LLM 生成」。

## 它治什么病

`core/llm_gate.py` 管的是 LLM 调用的**数量**（并发/预算/429 冷却），
`core/perception/proactive.py` 一旦意图命中就**必然**调 LLM 把模板改写成
更自然的一句（失败才回退模板池）。

桌宠缺一层「值不值得」的本地判断 —— 但直接接进决策链风险高，所以先做
**影子模式**：让本地引擎（复用 `core.expression_director.LocalDecisionClient`，
已处理好地址 / 超时 / 可用性探测）在后台并行跑一次，只写 JSONL，
**零行为改变**（关掉开关时连一次调用都不产生）。

跑一段时间后可以：
- 统计「引擎判『不值得』的那些，事后看多少真的没被投递 / 没被回复」
- 用这个数字决定是否升级成真的闸门（P1）

## 数据落盘

默认路径：`data/shadow_decisions/shadow_YYYY-MM-DD.jsonl`（每日一个文件，
按 oc-pet 惯例放在项目的 data 目录下，见 `paths.py`）。
每行一条 JSON，两种 record_type：

- `decision`：决策时写入 —— 输入摘要 + 引擎判断(值/概率/档位) + 延迟 + 引擎可用性
- `outcome`：结果回写 —— delivered / user_replied / source / prompt / 备注

## 硬纪律

1. **绝不抛异常**：所有公开入口包 try/except；异常只落到 logger.debug。
2. **绝不阻塞主线程**：所有引擎调用在后台 daemon 线程做；主线程只做
   入队 + 内存计数（微秒级）。
3. **关掉即无痕**：`enabled=false` 时不入队、不建线程、不做任何 I/O。
   行为与未接入本模块完全一致。
4. **复用不重写**：HTTP 客户端复用 `core.expression_director.LocalDecisionClient`
   —— 引擎地址沿用 `LOCAL_RLCD_BASE` 环境变量，默认 `http://127.0.0.1:8077`。
"""
from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# 影子判断用的问题类型（noul = 是/否；本地引擎的 schema 只支持 enum/choice 两种）
N_OUL_KIND = "noul"

# 影子记录器默认 JSONL 目录（项目 data 目录下；见 paths.py 的 DATA_DIR 惯例）
DEFAULT_JSONL_DIR = "shadow_decisions"

# 引擎单字段最大候选数（引擎约束，别超）——这里只用两个值，远不到。
_YES_NO_CHOICES = ("yes", "no")


def _default_jsonl_path(config: dict | None = None) -> str:
    """解析落盘路径。

    config.shadow_decision.jsonl_path：
      - 相对路径（非绝对）→ 相对 oc-pet 项目根（本项目 data/ 惯例）
      - 绝对路径 → 原样使用
      - 空 / 缺省 → 用 DEFAULT_JSONL_DIR + 日期后缀

    返回最终 JSONL 文件绝对路径（不做 mkdir，只在首次写入前创建）。
    """
    cfg = config or {}
    raw = (cfg.get("jsonl_path") or "").strip()
    root = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if not raw:
        raw = DEFAULT_JSONL_DIR
    p = Path(raw)
    if not p.is_absolute():
        p = root / p
    if p.suffix.lower() != ".jsonl":
        # 目录式：追加日期后缀（每日一个文件）
        p = p / f"shadow_{datetime.now().strftime('%Y-%m-%d')}.jsonl"
    return str(p)


def _build_noul_schema(question: str) -> dict:
    """构建 noul 型问题 schema。

    硬约束：选项必须写在 description 的**第一行**（引擎只取第一行），
    候选必须是引擎能分辨的短标签 —— 这里就两个值 "yes"/"no"。
    """
    q = (question or "是否值得花一次 LLM 生成").replace("\n", " ").strip()
    desc = f"问题：{q}。回答从这些里选一个：yes/no"
    return {
        "verdict": {
            "type": "enum",
            "description": desc,
            "choices": list(_YES_NO_CHOICES),
        }
    }


# ── 档位判定（复用 expression_director 的置信阈值口径）──────────────

try:
    from core.expression_director import BAND_HIGH, BAND_LOW  # type: ignore
except Exception:  # pragma: no cover — 极端情况下的兜底
    BAND_HIGH = 0.85
    BAND_LOW = 0.55


def _band_of(prob: float) -> str:
    """概率 → 档位（与 expression_director 一致：high / medium / low）。"""
    if prob >= BAND_HIGH:
        return "high"
    if prob >= BAND_LOW:
        return "medium"
    return "low"


# ── 摘要工具 ────────────────────────────────────────────────


def _summarize_context(context: dict) -> dict:
    """把原始 context 压缩成能塞进 prompt 的结构化摘要。

    只保留关键字段（scenario / intent / signals / fallback_prompt / source_key 等），
    长文本一律截断（引擎长文本会「自信地错」，见 expression_director 模块注释）。
    """
    if not isinstance(context, dict):
        return {"raw": str(context)[:120]}
    keep = (
        "scenario", "intent", "category", "period", "activity",
        "signals", "fallback_prompt", "source_key", "kind",
        "screen_scene", "screen_intent", "confidence", "is_weekend",
    )
    out: dict[str, Any] = {}
    for k in keep:
        if k in context and context[k] not in (None, "", [], {}):
            v = context[k]
            if isinstance(v, str):
                out[k] = v[:120]
            else:
                out[k] = v
    # signals 单独做一层截断（避免整个 signals 字典太长）
    sig = out.get("signals")
    if isinstance(sig, dict):
        slim: dict[str, Any] = {}
        for k in ("period", "category", "activity", "screen_scene",
                  "screen_intent", "conversation_idle_min", "fg_duration_min",
                  "window_switches_5min", "is_weekend", "screen_confidence"):
            if k in sig and sig[k] not in (None, "", [], {}):
                slim[k] = sig[k]
        if slim:
            out["signals"] = slim
    return out


# ── 记录器 ──────────────────────────────────────────────────


class ShadowDecisionRecorder:
    """影子模式记录器：本地引擎并行判断，只写 JSONL，不改行为。

    Args:
        config: 影子模式配置块（通常来自 config.shadow_decision）。支持键：
            - ``enabled``: bool（默认 False，关掉即无痕）
            - ``jsonl_path``: str（落盘路径；默认 data/shadow_decisions/…）
            - ``engine_base_url``: str（默认沿用 LOCAL_RLCD_BASE 环境变量）
            - ``timeout``: float（秒；默认 8）
            - ``queue_size``: int（内部工作队列容量；默认 128）
            - ``question``: str（noul 问题的文本；默认内置）
    """

    def __init__(self, config: dict | None = None):
        self._config: dict = dict(config or {})
        self._enabled: bool = bool(self._config.get("enabled", False))
        self._jsonl_path: str = _default_jsonl_path(self._config)

        # 内部 worker 队列（惰性创建 —— 关掉时无线程、无 I/O）
        self._queue: Optional["queue.Queue[dict]"] = None
        self._worker: Optional[threading.Thread] = None
        self._worker_ready: bool = False
        self._lock = threading.Lock()
        self._closed = False

        # 内存镜像计数（供 stats() 快速读取；权威数据仍在 JSONL）
        self._decisions_total: int = 0
        self._engine_said_no_worth: int = 0
        self._decided_delivered_count: int = 0
        self._delivered_outcomes: int = 0
        self._user_replied_true: int = 0
        self._user_replied_false: int = 0
        self._verdict_hist: dict[str, int] = {}
        self._band_hist: dict[str, int] = {}
        self._source_hist: dict[str, int] = {}
        self._engine_available_count: int = 0

        self._outcome_meta: dict[str, dict] = {}
        self._pending_user_reply: dict[str, bool] = {}
        self._last_decision_id: Optional[str] = None

    # ── 开关 ───────────────────────────────────────────────

    def enabled(self) -> bool:
        """总开关。关掉时本类完全静默：不入队、不建线程、不做 I/O。"""
        return bool(self._enabled)

    def set_enabled(self, enabled: bool) -> None:
        """运行时切换开关（不会立刻关线程；已排队的条目照写）。"""
        self._enabled = bool(enabled)

    def set_config(self, config: dict | None) -> None:
        """重新载入配置（例如 config.json 热更新）。"""
        new_cfg = dict(config or {})
        self._config = new_cfg
        self._enabled = bool(new_cfg.get("enabled", False))
        # 只在新路径与旧路径不同时更新（避免运行中改文件）
        new_path = _default_jsonl_path(new_cfg)
        if new_path != self._jsonl_path:
            self._jsonl_path = new_path

    # ── 记录决策 ───────────────────────────────────────────

    def record_decision(
        self,
        *,
        kind: str,
        context: dict,
        fallback_prompt: str = "",
    ) -> Optional[str]:
        """记录一次「值不值得花一次 LLM 生成」的影子判断。

        **纯旁路**：主线程只做入队 + 内存计数（微秒级），引擎调用在
        后台 daemon 线程执行。返回 decision_id；未启用时返回 None。

        Args:
            kind: 决策点类型（如 ``"proactive.intent"`` / ``"proactive.rule"``）。
            context: 决策时点的上下文（scenario / signals / intent 等）。
            fallback_prompt: 若引擎判「不值得」，实际会用的兜底模板文案。

        Returns:
            decision_id（uuid4 hex 前 12 位）；未启用 / 入队失败时返回 None。
            **永不抛异常**。
        """
        try:
            if not self._enabled:
                return None
            q = self._ensure_worker()
            if q is None:
                return None

            decision_id = uuid.uuid4().hex[:12]
            item: dict[str, Any] = {
                "action": "decision",
                "decision_id": decision_id,
                "kind": kind or "",
                "context": context or {},
                "fallback_prompt": (fallback_prompt or "")[:200],
                "ts_decision": time.time(),
            }
            try:
                q.put_nowait(item)
            except queue.Full:
                # 队列满 = 引擎太慢或调用太密；这条直接放弃（不阻塞、不抛）
                logger.debug("shadow_decision: 队列已满，放弃 decision_id=%s",
                             decision_id)
                return None
            # 内存计数（主线程安全，锁内一次赋值）
            with self._lock:
                self._decisions_total += 1
                self._last_decision_id = decision_id
            return decision_id
        except Exception:
            logger.debug("shadow_decision: record_decision 失败（忽略）",
                         exc_info=True)
            return None

    # ── 记录结果 ───────────────────────────────────────────

    def record_outcome(
        self,
        decision_id: str,
        *,
        delivered: bool,
        user_replied: Optional[bool] = None,
        source: str = "",
        prompt: str = "",
        note: str = "",
    ) -> None:
        """为某个 decision_id 追加一条 outcome 记录（多次追加均可）。

        与 decision 记录共享同一 decision_id，方便事后 join 分析
        「引擎判『不值得』的那些，事后看真的没被投递 / 没被回复吗？」。

        **纯旁路**：入队 + 内存计数，永不抛异常、永不阻塞。
        """
        try:
            if not self._enabled or not decision_id:
                return
            q = self._ensure_worker()
            if q is None:
                return

            item: dict[str, Any] = {
                "action": "outcome",
                "decision_id": decision_id,
                "delivered": bool(delivered),
                "user_replied": user_replied,
                "source": source or "",
                "prompt": (prompt or "")[:200],
                "note": (note or "")[:120],
                "ts_outcome": time.time(),
            }
            try:
                q.put_nowait(item)
            except queue.Full:
                logger.debug("shadow_decision: 队列已满，放弃 outcome=%s",
                             decision_id)
                return
            with self._lock:
                if decision_id not in self._outcome_meta:
                    self._outcome_meta[decision_id] = {
                        "delivered": bool(delivered),
                        "user_replied": user_replied,
                        "first_ts": time.time(),
                    }
                else:
                    # 后到的 delivered=True 覆盖 False（更严格的事实）
                    meta = self._outcome_meta[decision_id]
                    if delivered and not meta.get("delivered"):
                        meta["delivered"] = True
                    if user_replied is True:
                        meta["user_replied"] = True
                    elif user_replied is False and meta.get("user_replied") is None:
                        meta["user_replied"] = False
                if delivered:
                    self._decided_delivered_count += 1
                    self._delivered_outcomes += 1
                if user_replied is True:
                    self._user_replied_true += 1
                elif user_replied is False:
                    self._user_replied_false += 1
        except Exception:
            logger.debug("shadow_decision: record_outcome 失败（忽略）",
                         exc_info=True)

    def record_user_reply(self, decision_id: str, replied: bool = True) -> None:
        """便捷方法：登记用户对某条主动搭话的回复情况。

        通常由 ``ProactiveScheduler.mark_conversation(user_reply=True)`` 调用。
        """
        try:
            self.record_outcome(
                decision_id,
                delivered=False,
                user_replied=bool(replied),
                note="user_reply_recorded",
            )
        except Exception:
            logger.debug("shadow_decision: record_user_reply 失败（忽略）",
                         exc_info=True)

    def last_decision_id(self) -> Optional[str]:
        """最近一次 record_decision 的 ID（None 表示尚未记录）。"""
        return self._last_decision_id

    # ── 汇总统计 ───────────────────────────────────────────

    def stats(self) -> dict:
        """返回内存镜像统计（权威数据在 JSONL；这里给快速读数）。"""
        with self._lock:
            total = self._decisions_total
            return {
                "enabled": self._enabled,
                "jsonl_path": self._jsonl_path,
                "decisions_total": total,
                "engine_said_no_worth": self._engine_said_no_worth,
                "decided_and_delivered": self._decided_delivered_count,
                "no_worth_delivered_rate": (
                    round(self._decided_delivered_count / total, 3) if total else 0.0
                ),
                "verdict_hist": dict(self._verdict_hist),
                "band_hist": dict(self._band_hist),
                "source_hist": dict(self._source_hist),
                "engine_available_count": self._engine_available_count,
                "outcomes_total": len(self._outcome_meta),
                "delivered_outcomes": self._delivered_outcomes,
                "user_replied_true": self._user_replied_true,
                "user_replied_false": self._user_replied_false,
            }

    # ── 内部：worker 线程管理 ──────────────────────────────

    def _ensure_worker(self) -> Optional["queue.Queue[dict]"]:
        """惰性创建队列 + daemon worker 线程。

        关掉开关时不建线程、不建队列 —— 保证「关掉即无痕」。
        """
        try:
            if not self._enabled:
                return None
            with self._lock:
                if self._queue is None:
                    self._queue = queue.Queue(
                        maxsize=int(self._config.get("queue_size", 128) or 128)
                    )
                if self._worker is None or not self._worker.is_alive():
                    t = threading.Thread(
                        target=self._worker_loop,
                        name="shadow_decision_worker",
                        daemon=True,
                    )
                    t.start()
                    self._worker = t
                    self._worker_ready = True
            return self._queue
        except Exception:
            logger.debug("shadow_decision: 启动 worker 失败（忽略）", exc_info=True)
            return None

    def _worker_loop(self) -> None:
        """daemon worker：从队列取条目，写 JSONL；绝不抛异常。"""
        while not self._closed:
            try:
                q = self._queue
                if q is None:
                    time.sleep(0.1)
                    continue
                try:
                    item = q.get_nowait()
                except queue.Empty:
                    time.sleep(0.05)  # 50ms 空转；队列非阻塞，主线程不受影响
                    continue
                try:
                    if item.get("action") == "decision":
                        self._handle_decision(item)
                    elif item.get("action") == "outcome":
                        self._handle_outcome(item)
                except Exception:
                    logger.debug("shadow_decision: 处理条目失败（忽略）",
                                 exc_info=True)
            except Exception:
                logger.debug("shadow_decision: worker 主循环异常（忽略）",
                             exc_info=True)
                try:
                    time.sleep(0.2)
                except Exception:
                    break

    def _handle_decision(self, item: dict) -> None:
        """后台线程：调引擎 → 写 JSONL → 更新内存计数。"""
        decision_id = item.get("decision_id", "")
        kind = item.get("kind", "")
        context = item.get("context") or {}
        fallback_prompt = item.get("fallback_prompt", "")
        ts_decision = float(item.get("ts_decision") or time.time())
        question = self._config.get("question") or (
            "这条主动搭话值不值得花一次 LLM 生成？"
        )

        engine_available = False
        verdict: Optional[str] = None
        prob = 0.0
        elapsed_ms = 0.0
        reason = ""
        source = "engine_off"
        available_probe_ms = 0.0
        parsed_json = None

        try:
            from core.expression_director import LocalDecisionClient
            base_url = (
                self._config.get("engine_base_url")
                or os.environ.get("LOCAL_RLCD_BASE")
                or "http://127.0.0.1:8077"
            )
            timeout = float(self._config.get("timeout", 8) or 8)
            client = LocalDecisionClient(base_url=base_url, timeout=timeout)
            t_probe = time.perf_counter()
            engine_available = bool(client.is_available())
            available_probe_ms = (time.perf_counter() - t_probe) * 1000

            if engine_available:
                t0 = time.perf_counter()
                schema = _build_noul_schema(question)
                context_str = self._build_context_str(context, fallback_prompt)
                parsed_json = client.decide(context_str, schema)
                elapsed_ms = (time.perf_counter() - t0) * 1000
                if parsed_json:
                    ans = parsed_json.get("verdict")
                    if isinstance(ans, dict) and ans.get("value") in ("yes", "no"):
                        verdict = str(ans["value"])
                        try:
                            prob = float(ans.get("prob") or 0.0)
                        except Exception:
                            prob = 0.0
                        source = "engine"
                if source != "engine":
                    reason = "引擎未返回有效 verdict"
        except Exception as e:
            reason = f"引擎调用异常: {e!r}"
            source = "engine_err"

        band = _band_of(prob)
        record = {
            "record_type": "decision",
            "decision_id": decision_id,
            "kind": kind,
            "ts_decision": ts_decision,
            "ts_utc": datetime.now(timezone.utc).isoformat(),
            "input_summary": _summarize_context(context),
            "fallback_prompt": fallback_prompt,
            "engine_available": engine_available,
            "verdict": verdict,
            "prob": round(prob, 4),
            "band": band,
            "source": source,
            "elapsed_ms": round(elapsed_ms, 1),
            "available_probe_ms": round(available_probe_ms, 1),
            "reason": reason,
            "question": question,
        }
        self._append(record)
        with self._lock:
            if source in ("engine", "engine_err"):
                self._source_hist[source] = self._source_hist.get(source, 0) + 1
            elif engine_available:
                self._source_hist["engine"] = self._source_hist.get("engine", 0) + 1
            else:
                self._source_hist["engine_off"] = (
                    self._source_hist.get("engine_off", 0) + 1
                )
            if engine_available:
                self._engine_available_count += 1
            if verdict:
                self._verdict_hist[verdict] = (
                    self._verdict_hist.get(verdict, 0) + 1
                )
                if verdict == "no":
                    self._engine_said_no_worth += 1
            self._band_hist[band] = self._band_hist.get(band, 0) + 1

    @staticmethod
    def _build_context_str(context: dict, fallback_prompt: str) -> str:
        """把结构化 context 拼成引擎要读的短上下文。

        硬约束（见 expression_director 注释）：**必须是结构化 key=value 形式**，
        不能塞原始中文长句。这里挑关键信号拼成 ≤300 字的短摘要。
        """
        parts: list[str] = []
        if context.get("scenario"):
            parts.append(f"scenario={context['scenario']}")
        if context.get("category"):
            parts.append(f"category={context['category']}")
        if context.get("period"):
            parts.append(f"period={context['period']}")
        if context.get("activity"):
            parts.append(f"activity={context['activity']}")
        sig = context.get("signals") if isinstance(context.get("signals"), dict) else {}
        for k in ("conversation_idle_min", "fg_duration_min",
                  "window_switches_5min", "is_weekend"):
            v = sig.get(k)
            if v is None or v == "":
                continue
            parts.append(f"{k}={v}")
        if sig.get("screen_scene"):
            parts.append(f"screen_scene={sig['screen_scene']}")
        if sig.get("screen_intent"):
            parts.append(f"screen_intent={sig['screen_intent']}")
        if context.get("intent") and isinstance(context.get("intent"), dict):
            parts.append(f"intent_name={context['intent'].get('intent', '')}")
        if context.get("confidence"):
            try:
                parts.append(f"confidence={float(context['confidence']):.2f}")
            except Exception:
                pass
        if fallback_prompt:
            parts.append(f"template={fallback_prompt[:40]}")
        s = ", ".join(parts)
        return s[:300]

    def _handle_outcome(self, item: dict) -> None:
        """后台线程：把 outcome 直接写 JSONL。"""
        record = {
            "record_type": "outcome",
            "decision_id": item.get("decision_id", ""),
            "delivered": bool(item.get("delivered")),
            "user_replied": item.get("user_replied"),
            "source": item.get("source", ""),
            "prompt": item.get("prompt", ""),
            "note": item.get("note", ""),
            "ts_outcome": float(item.get("ts_outcome") or time.time()),
            "ts_utc": datetime.now(timezone.utc).isoformat(),
        }
        self._append(record)

    def _append(self, record: dict) -> None:
        """追加一条 JSONL；失败仅 debug 日志，不抛出。"""
        try:
            path = self._jsonl_path
            d = os.path.dirname(path)
            if d:
                os.makedirs(d, exist_ok=True)
            line = json.dumps(record, ensure_ascii=False, default=str)
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception as e:
            logger.debug("shadow_decision: 写入 JSONL 失败: %s", e)

    def close(self) -> None:
        """关闭 worker（尽力而为；测试用）。"""
        try:
            self._closed = True
        except Exception:
            pass
