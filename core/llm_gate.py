"""后台 LLM 调用的全局闸门（O1-P2，429 治理的最后半边）。

## 为什么需要它

2026-08 那次 429 事故的成因不是"某个函数写错了"，是**三条线程互不知情**：
屏幕视觉、语义增强、主动对话各自独立地往同一个 provider 打，
同一时刻一起发，限流器自然翻脸。

之前已经落地的（各自为政）：
- 屏幕侧：撞到 429 → 自己拉长轮询间隔；
- adapter：撞到 429 → 自己指数重试；内部来源改走 `utility_model` 分走配额；
- enrich 有 300s 冷却；截图有 dHash 去重。

这些都是**单点防护**。缺的是那张**全局的网**，本模块就是它。

## 三件事，缺一不可

1. **并发上限**：同一时刻只放行 N 个（默认 1）。
   后台任务晚两秒无所谓，一起撞上去会让限流器记恨你。
2. **每源预算**：滑动窗口内的调用总量上限（如 enrich 40 次/时）。
   **冷却管频率，预算管总量**——长时段跑下来只有预算能兜住。
3. **全局 429 冷却**：任何一条路撞到 429，**所有**后台源一起收手 N 秒；
   连续撞墙时冷却翻倍（60→120→240，封顶 900），一次成功即复位。

## 设计取舍

- **默认不阻塞**：抢不到槽就放弃这一次，**不排队**。后台少做一次语义增强，
  远比把线程堆起来健康（延迟无所谓，堆积有所谓）。
- **拒绝一定留痕**（限频日志），否则"为什么没增强"会变成一个新的谜。
- 并发槽用 `threading.Semaphore`；时间来自注入的 `now`，所以逻辑可单测。
"""
from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# 每源默认预算（次/小时）。0 = 该源彻底不放行（当开关用）。
DEFAULT_BUDGETS: dict[str, int] = {
    "vision": 120,      # 屏幕视觉：正常节奏约 30/h，给足余量
    "enrich": 40,       # 语义增强：冷却 300s 时约 12/h，给足余量
    "proactive": 30,    # 主动对话：本身还有自己的每日额度
}
BUDGET_WINDOW_SECONDS = 3600.0


class LlmGate:
    """后台 LLM 调用的全局闸门。

    Args:
        max_concurrent: 同时放行的后台调用数（默认 1）。
        budgets: 每源每小时预算；``None`` 用默认；传 dict 则**合并**到默认上。
            某源不在表里 = 不限量。
        cooldown_seconds: 撞一次 429 后全体静默的基础秒数。
        max_cooldown_seconds: 连续撞墙时冷却的上限（指数翻倍到此为止）。
        enabled: False 时一律放行（等于关掉闸门）。
        now: 取时间函数（测试注入）。
    """

    def __init__(
        self,
        max_concurrent: int = 1,
        budgets: Optional[dict] = None,
        cooldown_seconds: float = 60.0,
        max_cooldown_seconds: float = 900.0,
        enabled: bool = True,
        now: Callable[[], float] = time.time,
    ):
        self._enabled = bool(enabled)
        self._now = now
        self._max_concurrent = max(1, int(max_concurrent))
        self._sem = threading.Semaphore(self._max_concurrent)
        self._lock = threading.Lock()

        self._budgets = dict(DEFAULT_BUDGETS)
        if budgets:
            for k, v in budgets.items():
                try:
                    self._budgets[str(k)] = int(v)
                except (TypeError, ValueError):
                    logger.warning("llm_gate: 预算项 %r 不是整数，已忽略", k)
        self._usage: dict[str, list[float]] = {}

        self._cooldown_base = max(0.0, float(cooldown_seconds))
        self._cooldown_max = max(self._cooldown_base, float(max_cooldown_seconds))
        self._cooldown_until = 0.0
        self._cooldown_len = 0.0          # 当前这一轮冷却时长（指数增长的结果）
        self._hits_429 = 0
        # ── 2026-09-19：按来源归因 ──
        # 动机：桌宠有三条**互不共享配额**的 LLM 流 ——
        #   · vision     屏幕视觉（screen.py 独立 HTTP 直连，走 vision_model）
        #   · utility    后台任务（screen_enrich / proactive / idle /
        #                memory_extract / memory_reflect，走 Hana utility_model）
        #   · chat       用户对话（走 models.chat）
        # 但 429 此前只有一个全局 int，撞墙时无法回答「到底是哪条流在撞」。
        # 于是只能笼统归因为「共用 provider」，进而调错杠杆。
        # 这里补上按 source 的计数（只加统计，不改任何放行/冷却语义）。
        self._hits_429_by_source: dict[str, int] = {}
        self._ok_by_source: dict[str, int] = {}
        self._rejects_by_source: dict[str, int] = {}

    # ── 状态 ──────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        return self._enabled

    def cooldown_remaining(self) -> float:
        """距冷却结束还剩多少秒（0 = 没在冷却）。"""
        with self._lock:
            return max(0.0, self._cooldown_until - self._now())

    def stats(self) -> dict:
        """当前状态快照（供状态口 / 日志用）。"""
        now = self._now()
        with self._lock:
            usage = {s: len([t for t in ts if now - t <= BUDGET_WINDOW_SECONDS])
                     for s, ts in self._usage.items()}
            return {
                "enabled": self._enabled,
                "max_concurrent": self._max_concurrent,
                "cooldown_remaining": round(max(0.0, self._cooldown_until - now), 1),
                "cooldown_len": self._cooldown_len,
                "hits_429": self._hits_429,
                "budgets": dict(self._budgets),
                "used_last_hour": usage,
                # ── 归因（2026-09-19 新增，累计不复位）──
                "hits_429_by_source": dict(self._hits_429_by_source),
                "rejects_by_source": dict(self._rejects_by_source),
                "by_stream": self._stream_breakdown_locked(usage),
            }

    def _stream_breakdown_locked(self, usage: dict[str, int]) -> dict[str, dict]:
        """把 used / 429 / 拒绝 三类计数按 vision / utility / chat / other 汇总。

        调用者必须已持有 ``self._lock``。
        """
        out: dict[str, dict] = {}
        streams = ("vision", "utility", "chat", "other")
        for s in streams:
            out[s] = {"used_last_hour": 0, "hits_429": 0, "rejects": 0,
                      "budget": None, "sources": []}
        for src, n in usage.items():
            st = self.stream_of(src)
            out[st]["used_last_hour"] += n
            out[st]["sources"].append(src)
        for src, n in self._hits_429_by_source.items():
            out[self.stream_of(src)]["hits_429"] += n
        for src, n in self._rejects_by_source.items():
            out[self.stream_of(src)]["rejects"] += n
        # 预算：取该流下各 source 预算之和（None 表示不限量）
        for src, b in self._budgets.items():
            st = self.stream_of(src)
            cur = out[st]["budget"]
            out[st]["budget"] = b if cur is None else cur + b
        return out

    def attribution_report(self) -> str:
        """一行人可读的归因摘要（供日志 / 状态口展示）。

        回答「到底是哪条流在撞 429」——这是 2026-09-19 之前无法回答的问题。
        """
        st = self.stats()
        bs = st.get("by_stream", {})
        parts = []
        for name in ("vision", "utility", "chat", "other"):
            d = bs.get(name, {})
            used, hits, rej = (d.get("used_last_hour", 0),
                               d.get("hits_429", 0), d.get("rejects", 0))
            if not (used or hits or rej):
                continue
            b = d.get("budget")
            btxt = "∞" if b is None else str(b)
            parts.append(f"{name}: 用{used}/{btxt} 429={hits} 拒={rej}")
        if not parts:
            return "LLM 闸门：本小时无后台调用"
        return "LLM 归因 | " + " | ".join(parts)

    # ── 来源归因（2026-09-19）────────────────────────────

    #: 具体来源 → 三条流之一。未列出的来源归到 ``other``。
    #: 这三条流的**配额是分开的**，撞 429 时的应对也完全不同：
    #:   vision  → 调 phash_threshold / 拉长截屏间隔 / 换 vision_model
    #:   utility → 换 utility_model / 收紧 per-source 预算
    #:   chat    → 用户对话，优先级最高，应尽量少被前两条挤占
    _STREAM_OF_SOURCE: dict[str, str] = {
        "vision": "vision",
        "enrich": "vision",          # screen_enrich：屏幕语义增强，同属屏幕流
        "proactive": "utility",
        "idle": "utility",
        "memory_extract": "utility",
        "memory_reflect": "utility",
        "user": "chat",
        "direct": "chat",
    }

    @classmethod
    def stream_of(cls, source: str) -> str:
        """把具体来源归入 vision / utility / chat / other 四条流之一。"""
        key = str(source or "").strip().lower()
        return cls._STREAM_OF_SOURCE.get(key, "other")

    def _norm_source(self, source: str) -> str:
        """归因用的 key：直接用传入的 source，空则 unknown。"""
        return str(source or "unknown").strip()

    # ── 放行判定 ──────────────────────────────────────────

    def check(self, source: str) -> tuple[bool, str]:
        """只判不占：返回 ``(是否放行, 拒因)``。拒因用于日志/诊断。"""
        if not self._enabled:
            return True, ""
        now = self._now()
        key = self._norm_source(source)
        with self._lock:
            if now < self._cooldown_until:
                self._rejects_by_source[key] = self._rejects_by_source.get(key, 0) + 1
                return False, "cooldown"
            budget = self._budgets.get(key)
            if budget is not None:
                if budget <= 0:
                    self._rejects_by_source[key] = self._rejects_by_source.get(key, 0) + 1
                    return False, "budget=0"
                ts = [t for t in self._usage.get(key, [])
                      if now - t <= BUDGET_WINDOW_SECONDS]
                self._usage[key] = ts
                if len(ts) >= budget:
                    self._rejects_by_source[key] = self._rejects_by_source.get(key, 0) + 1
                    return False, "budget"
        return True, ""

    def _record(self, source: str) -> None:
        now = self._now()
        with self._lock:
            self._usage.setdefault(str(source), []).append(now)

    # ── 占用 / 归还 ───────────────────────────────────────

    def acquire(self, source: str, timeout: float = 0.0) -> bool:
        """尝试占用一个并发槽并记一次用量。

        Args:
            source: 来源标识（``vision`` / ``enrich`` / ``proactive`` …）。
            timeout: 等待槽位的秒数；0 = 不等待（默认，抢不到就放弃）。

        Returns:
            True 表示已占用——**必须**配对调用 :meth:`release`。
        """
        ok, _why = self.check(source)
        if not ok:
            return False
        # ⚠️ threading.Semaphore.acquire(blocking=False, timeout=0) 会直接
        # 抛 ValueError（“can't specify timeout for non-blocking acquire”），
        # 所以两种模式分开调。
        if timeout > 0:
            got = self._sem.acquire(timeout=timeout)
        else:
            got = self._sem.acquire(blocking=False)
        if not got:
            return False
        self._record(source)
        return True

    def release(self) -> None:
        """归还并发槽（与 acquire 配对）。多还一次也不会把信号量弄坏。"""
        try:
            if self._sem._value < self._max_concurrent:  # type: ignore[attr-defined]
                self._sem.release()
        except Exception:  # noqa: BLE001 — 归还失败绝不该抛给调用方
            logger.debug("llm_gate: release 异常（忽略）", exc_info=True)

    @contextmanager
    def guard(self, source: str, timeout: float = 0.0):
        """上下文管理器：放行时 yield True 并持有槽；拒绝时 yield False。不抛异常。

        用法::

            with get_gate().guard("enrich") as ok:
                if not ok:
                    return
                ...

        ⚠️ 拒绝**不记日志**在这里做——由调用方按自己的日志级别决定（避免刷屏）。
        """
        if not self.acquire(source, timeout=timeout):
            yield False
            return
        try:
            yield True
        finally:
            self.release()

    # ── 429 信号 ──────────────────────────────────────────

    def notify_429(self, source: str = "") -> float:
        """报告一次 429：全体进入冷却，连续撞墙则翻倍。

        闸门关掉时是**空操作**（返回 0）：关掉就完全不管，
        不留一个“在冷却中”的状态让人误读。

        Returns:
            本轮冷却时长（秒）；闸门关闭时为 0。
        """
        if not self._enabled:
            return 0.0
        key = self._norm_source(source)
        with self._lock:
            self._hits_429 += 1
            self._hits_429_by_source[key] = self._hits_429_by_source.get(key, 0) + 1
            per_source = self._hits_429_by_source[key]
            if self._cooldown_until > self._now():
                # 冷却期内又撞 → 翻倍（上限封顶）
                self._cooldown_len = min(
                    max(self._cooldown_base, self._cooldown_len * 2), self._cooldown_max
                )
            else:
                self._cooldown_len = self._cooldown_base
            self._cooldown_until = self._now() + self._cooldown_len
            length = self._cooldown_len
            breakdown = dict(self._hits_429_by_source)
        logger.warning(
            "LLM 全局闸门：%s 撞到 429，后台调用全体静默 %.0fs"
            "（全局第 %d 次 / 该来源第 %d 次；归因 %s）",
            source or "未知来源", length, self._hits_429, per_source, breakdown,
        )
        return length

    def notify_ok(self) -> None:
        """报告一次成功：复位 429 计数与冷却长度（冷却本身自然到期）。

        闸门关掉时是空操作。
        """
        if not self._enabled:
            return
        with self._lock:
            if self._hits_429 or self._cooldown_len:
                logger.info("LLM 全局闸门：调用恢复正常，429 计数复位（此前 %d 次；归因 %s）",
                            self._hits_429, dict(self._hits_429_by_source))
            self._hits_429 = 0
            self._cooldown_len = 0.0
            # 注意：按 source 的计数**不复位** —— 它是累计归因证据，
            # 复位会让「哪条流在撞墙」这个答案随一次成功而消失。
            # 只有全局计数（驱动冷却翻倍）才复位。


# ── 进程级单例 ────────────────────────────────────────────

_gate: LlmGate = LlmGate()
_gate_lock = threading.Lock()


def get_gate() -> LlmGate:
    """取当前闸门。调用点应**每次现取**，不要缓存到 self 上（配置可能热替换）。"""
    return _gate


def configure_gate(config: Optional[dict]) -> LlmGate:
    """按 ``config.json`` 的 ``llm_gate`` 块重建闸门（启动时调一次）。

    配置项::

        "llm_gate": {
          "enabled": true,
          "max_concurrent": 1,
          "cooldown_seconds": 60,
          "max_cooldown_seconds": 900,
          "budgets": { "vision": 120, "enrich": 40, "proactive": 30 }
        }
    """
    global _gate
    cfg = ((config or {}).get("llm_gate") or {}) if isinstance(config, dict) else {}
    with _gate_lock:
        _gate = LlmGate(
            max_concurrent=int(cfg.get("max_concurrent", 1) or 1),
            budgets=cfg.get("budgets") if isinstance(cfg.get("budgets"), dict) else None,
            cooldown_seconds=float(cfg.get("cooldown_seconds", 60) or 60),
            max_cooldown_seconds=float(cfg.get("max_cooldown_seconds", 900) or 900),
            enabled=bool(cfg.get("enabled", True)),
        )
        logger.info("LLM 全局闸门已配置：并发 %d ｜ 预算 %s ｜ 冷却 %.0fs 起",
                    _gate._max_concurrent, _gate.stats()["budgets"], _gate._cooldown_base)
        return _gate
