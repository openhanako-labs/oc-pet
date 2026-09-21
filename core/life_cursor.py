# -*- coding: utf-8 -*-
"""生活游标 — 把事件流的时间轴压成"一句近况"，注入 prompt。

## 要解决的问题

桌宠**不知道你今天怎么过的**。素材其实已经躺在磁盘上了：
``~/.oc-pet/memory/<agent_id>_events.jsonl``（`core/event_stream.py`，append-only，
带 ``ts/start_ts/end_ts/category/scenario/intent/topic/source``）。
但**没有任何消费者把它变成叙述**——它只被写、被裁、被面板读。

## 和已有 ReflectionEngine 的区别（别搞混）

| | ReflectionEngine（已有，B 线 P1-3） | 生活游标（本模块） |
|---|---|---|
| 周期 | **24 小时** | 默认 6 小时窗口 / 60 分钟刷新 |
| 输入量 | 最近 200 条 | 最近 120 条 |
| 产出 | **洞察条目**（observation/conclusion/confidence） | **一句叙述** |
| 去向 | ``<agent_id>_reflections.json`` → **只进 UI 面板** | **注入对话 prompt** |

两者消费同一份素材，但一个回答"你是个怎样的人"，一个回答"你刚在干嘛"。
本模块**复用它的成例**（后台线程 + Qt 信号、隐私处理、失败跳过不推进时间戳），
不重复造管道。

## 素材质量约束（踩过的坑，别重走）

- **不用 ``emotion`` 字段**。事件流里的 emotion 是旧管线产物（CHANGELOG 自述
  的"正价区全塌成 happy"），拿它当信号等于把噪声当结论。
- 只用 ``category / scenario / intent / topic`` + 时间戳。
- ``topic`` 入库时已被截断 60 字；``source="vision"`` 的事件**入库时就不落文本**
  （`EventStream` 双保险），本模块**再排除一次**。
- 隐私底线：**只把确定性 brief 交给模型**，不把原始事件行直接灌进 prompt。

## 模型是可选的

`build_brief()` 产出的确定性文本**本身就能用**（"最近 6 小时：开发 12 条 /
娱乐 5 条；最近话题：桌面宠物、配置"）。模型只负责把它润成一句人话；
拿不到模型时**退化为 brief**，不让这一层静默消失。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_ENABLED = False
DEFAULT_WINDOW_HOURS = 6.0
DEFAULT_INTERVAL_MINUTES = 60.0
DEFAULT_MIN_EVENTS = 5
DEFAULT_MAX_EVENTS = 120

#: brief 自身长度上限（确定性文本，喂给模型用）
MAX_BRIEF_CHARS = 420
#: 最终注入 prompt 的那句话上限
MAX_NARRATIVE_CHARS = 80

#: 这些 category 不值得念出来（噪声）
_NOISE_CATEGORIES = {"unknown", "", "none", "idle"}

#: 分类名 → 中文（念给人听）
_CATEGORY_CN = {
    "development": "写代码",
    "coding": "写代码",
    "work": "工作",
    "study": "学习",
    "reading": "看东西",
    "entertainment": "娱乐",
    "video": "看视频",
    "game": "打游戏",
    "gaming": "打游戏",
    "chat": "聊天",
    "social": "聊天",
    "browsing": "逛网页",
    "browser": "逛网页",
    "music": "听音乐",
    "writing": "写作",
    "design": "做设计",
    "other": "其他",
}


def _fmt_span(hours: float) -> str:
    if hours < 1:
        return f"{int(max(1, hours * 60))} 分钟"
    if hours < 48:
        return f"{hours:.0f} 小时"
    return f"{hours / 24:.1f} 天"


def category_cn(name: Any) -> str:
    key = str(name or "").strip().lower()
    return _CATEGORY_CN.get(key, key or "其他")


def pick_records(records: list, start_ts: float, end_ts: float,
                 max_events: int = DEFAULT_MAX_EVENTS) -> list:
    """按时间窗筛事件，**按 ts 升序**返回；max_events>0 时只留最近的那么多条。

    **隐私**：``source == "vision"`` 的一律丢弃（入库时已不落文本，这里是双保险）。

    为什么主动排序而不是"假定调用方给的是有序的"：真实的 ``read_since``
    确实按追加顺序（= 时间序）返回，但一旦上游换成别的读取方式，
    "保留最近 N 条"会**静默地保留最旧的 N 条**——错得很安静。
    这一点上一轮就被单测抓到过（我拿倒序数据试的）。
    """
    out = []
    for r in records or []:
        if not isinstance(r, dict):
            continue
        if str(r.get("source") or "").strip().lower() == "vision":
            continue
        try:
            ts = float(r.get("ts") or 0.0)
        except Exception:
            continue
        if ts < start_ts or ts > end_ts:
            continue
        out.append((ts, r))
    out.sort(key=lambda p: p[0])
    picked = [r for _ts, r in out]
    if max_events and len(picked) > max_events:
        picked = picked[-max_events:]
    return picked


def build_brief(records: list, now: Optional[float] = None) -> str:
    """把事件窗口压成一段**确定性**中文简报（无模型、零成本）。

    只用 category / scenario / topic / 时间戳 + 条数。样本不足返回空串。
    """
    try:
        recs = [r for r in (records or []) if isinstance(r, dict)]
        if not recs:
            return ""
        now = float(now or time.time())

        cats: dict[str, int] = {}
        topics: list[str] = []
        for r in recs:
            c = str(r.get("category") or "").strip().lower()
            if c and c not in _NOISE_CATEGORIES:
                cats[c] = cats.get(c, 0) + 1
            t = str(r.get("topic") or "").strip()
            if t and t not in topics:
                topics.append(t)

        stamps = []
        for r in recs:
            try:
                stamps.append(float(r.get("ts") or 0.0))
            except Exception:
                continue
        span = ""
        if stamps:
            span = _fmt_span(max(0.0, (max(stamps) - min(stamps)) / 3600.0))

        parts = []
        if span:
            parts.append(f"跨约 {span}")
        parts.append(f"{len(recs)} 条活动记录")
        top = sorted(cats.items(), key=lambda kv: -kv[1])[:3]
        if top:
            parts.append("主要是" + "、".join(
                f"{category_cn(k)}{v} 次" for k, v in top))
        if topics:
            parts.append("最近话题：" + "、".join(topics[:3]))

        text = "；".join(parts)
        return text[:MAX_BRIEF_CHARS]
    except Exception:
        logger.debug("life_cursor: build_brief 失败", exc_info=True)
        return ""


def build_prompt(brief: str) -> str:
    """让 utility 模型把简报润成一句人话。不给指令、不许编造。"""
    return (
        "你在帮一个桌面宠物写它的「当下近况」。下面是从它主人电脑上采集到的"
        "**结构化活动统计**（不含任何原文内容，所以不要猜测具体做了什么）：\n"
        f"  {brief}\n\n"
        "请用 1 句话中文写出「他最近在忙什么」。要求：\n"
        "  1. 只描述倾向与节奏，不要罗列数字；\n"
        "  2. **不要**出现「你应该」「建议」这类说法；\n"
        "  3. 不要编造统计里没有的具体事件（人名、项目名、文件内容都不许猜）；\n"
        f"  4. 总长不超过 {MAX_NARRATIVE_CHARS} 字。"
    )


class LifeCursor:
    """游标状态：什么时候该刷新、上一句是什么。**纯内存，无 IO。**

    只做三件事：判到期（interval / 新素材 / 样本量）、记上一次渲染结果、
    给可观测快照。取事件、调模型、注入 prompt 都由调用方做。
    """

    def __init__(self, config: Optional[dict] = None):
        self.set_config(config)

    def set_config(self, config: Optional[dict] = None) -> None:
        cfg = config if isinstance(config, dict) else {}

        def _f(key, default):
            try:
                v = cfg.get(key)
                return float(v) if v not in (None, "") else float(default)
            except Exception:
                return float(default)

        def _i(key, default):
            v = _f(key, default)
            return int(v)

        self._enabled = bool(cfg.get("enabled", DEFAULT_ENABLED))
        self._window_hours = max(0.05, _f("window_hours", DEFAULT_WINDOW_HOURS))
        self._interval_min = max(0.0, _f("interval_minutes", DEFAULT_INTERVAL_MINUTES))
        self._min_events = max(1, _i("min_events", DEFAULT_MIN_EVENTS))
        self._max_events = max(1, _i("max_events", DEFAULT_MAX_EVENTS))
        if not hasattr(self, "_last_render_ts"):
            self.reset()
        elif not self._enabled:
            self.reset()

    @property
    def enabled(self) -> bool:
        return self._enabled

    def reset(self) -> None:
        self._last_render_ts: float = 0.0
        self._text: str = ""
        self._brief: str = ""
        self._n_rendered: int = 0
        self._last_skip: str = ""

    @property
    def text(self) -> str:
        return self._text

    def window(self, now: Optional[float] = None) -> tuple[float, float]:
        now = float(now or time.time())
        return now - self._window_hours * 3600.0, now

    def maybe_brief(self, records: list,
                    now: Optional[float] = None) -> Optional[str]:
        """到期且有料 → 回一份 brief；否则回 None（调用方什么都不用做）。

        不给 brief 的原因记在 ``snapshot()['last_skip']`` 里——本层静默会让
        "没配好"和"时间没到"看起来一模一样。
        """
        if not self._enabled:
            self._last_skip = "disabled"
            return None
        try:
            now = float(now or time.time())
            if now - self._last_render_ts < self._interval_min * 60.0:
                self._last_skip = "interval"
                return None
            start, end = self.window(now)
            recs = pick_records(records, start, end, self._max_events)
            if len(recs) < self._min_events:
                self._last_skip = f"too_few({len(recs)}<{self._min_events})"
                return None
            brief = build_brief(recs, now=now)
            if not brief:
                self._last_skip = "empty_brief"
                return None
            if brief == self._brief:
                #  materially 相同 → 不必再花一次模型调用
                self._last_render_ts = now
                self._last_skip = "same_brief"
                return None
            self._last_skip = ""
            return brief
        except Exception:
            logger.debug("life_cursor: maybe_brief 失败", exc_info=True)
            self._last_skip = "error"
            return None

    def mark_rendered(self, text: str, brief: str = "",
                      now: Optional[float] = None) -> None:
        """渲染完成后回填（无论用的是模型文字还是 brief 兜底）。"""
        try:
            self._last_render_ts = float(now or time.time())
            self._text = str(text or "")[:MAX_NARRATIVE_CHARS * 2]
            self._brief = str(brief or "")
            self._n_rendered += 1
        except Exception:
            logger.debug("life_cursor: mark_rendered 失败", exc_info=True)

    def snapshot(self) -> dict:
        try:
            return {
                "enabled": self._enabled,
                "window_hours": self._window_hours,
                "interval_minutes": self._interval_min,
                "min_events": self._min_events,
                "n_rendered": self._n_rendered,
                "last_render_ts": self._last_render_ts,
                "last_skip": self._last_skip,
                "text": self._text,
            }
        except Exception:
            return {"enabled": False, "error": "snapshot 失败"}


def prompt_section(text: str) -> str:
    """把近况包成 prompt 段（空串 → 不出现这一段）。"""
    body = str(text or "").strip()
    return f"【近况】{body}" if body else ""


__all__ = [
    "DEFAULT_ENABLED", "DEFAULT_WINDOW_HOURS", "DEFAULT_INTERVAL_MINUTES",
    "DEFAULT_MIN_EVENTS", "DEFAULT_MAX_EVENTS",
    "MAX_BRIEF_CHARS", "MAX_NARRATIVE_CHARS",
    "LifeCursor", "build_brief", "build_prompt", "category_cn",
    "pick_records", "prompt_section",
]
