"""生动层 —— 让桌宠的反应不像模板。

## 它治什么病

"死"的表现不是功能少，是**可预测**。实查到的现场：

- 每次回到电脑前都是同一句「你回来啦~」+ 同一个 ``happy``
  （`behavior_mixin.py` 硬编码 2 处）；
- 屏幕主动对话失败永远是「你看了个有趣的视频啊～」——同一字符串写死 3 处，
  而且**它在断言自己没看清的内容**；
- 完全不区分「刚走开 5 分钟」和「离开 4 小时」，也不区分白天和凌晨三点。

再聪明的东西，只要**每次一样**，就会显出是机器。

## 三条规矩

1. **同一情境有多句话**，且**最近说过的不会立刻重复**（按情境记环形历史）。
   池子只有 2 句时也保证轮换——历史长度会被钳到 ``len(pool)-1``。
2. **情绪不写死**：同一情境给一组候选（含情绪），随机取，但**合情境**
   （深夜回来不该兴高采烈）。
3. **反应跟着"离开多久"走**：时长分档，夜里另成一档。
   刚走开、久别、深夜，是三件事。

## 用法

    from core.liveliness import greeting

    g = greeting(idle_seconds, hour=time.localtime().tm_hour)
    self._show_bubble(g["text"], emotion=g["emotion"])

所有随机都走注入的 ``rng``，所以行为可单测（不依赖运气）。
"""
from __future__ import annotations

import logging
import random
import threading
from typing import Optional

logger = logging.getLogger(__name__)

# 情境 → 候选台词（文本, 情绪）。情绪取值见 oc-pet 现有词表
# （happy / cute / neutral / thinking / sad / surprised / working / idle）。
POOLS: dict[str, tuple[tuple[str, str], ...]] = {
    # ── 回到电脑前（按离开时长分档）──
    "return_brief": (
        ("回来啦～", "happy"),
        ("就一眼没看住你，去哪了？", "cute"),
        ("我刚想找你呢。", "happy"),
        ("欸，回来了。", "neutral"),
        ("这么快就回来啦。", "cute"),
    ),
    "return_mid": (
        ("你回来啦～等你好一会儿了。", "happy"),
        ("走了这么久，我可没偷懒。", "cute"),
        ("刚才安静得有点不习惯。", "neutral"),
        ("回来啦——刚在忙别的吧？", "happy"),
        ("我还想着你会不会忘了这边。", "cute"),
    ),
    "return_long": (
        ("好久没见，你总算回来了。", "happy"),
        ("你走了好久。我一直在这儿。", "neutral"),
        ("终于。我都开始数秒了。", "cute"),
        ("这么久没动静，我还以为今天见不着你了。", "cute"),
        ("回来了就好。", "neutral"),
    ),
    "return_night": (
        ("这么晚才回来…别熬太狠。", "neutral"),
        ("夜里回来了。我还在。", "neutral"),
        ("凌晨了欸，要不要先歇一会儿。", "thinking"),
        ("这个点回来，辛苦了。", "neutral"),
        ("嘘——这个点了，轻点声。", "cute"),
    ),
    # ── 屏幕主动对话 ──
    "observing": (
        ("🔍 正在观察...", "thinking"),
        ("🔍 让我看看…", "thinking"),
        ("🔍 我瞧着点。", "thinking"),
        ("🔍 嗯…", "thinking"),
    ),
    # 兜底：**不猜内容**。原实现是「你看了个有趣的视频啊～」（断言自己没看清的东西）。
    "screen_fallback": (
        ("我刚才看走神了，没看清。", "neutral"),
        ("嗯…那个画面我没跟上。", "thinking"),
        ("抱歉，这一下我没读懂。", "neutral"),
        ("这个我还真说不准，等下再看一眼。", "thinking"),
        ("刚才那一屏有点糊，我没抓准。", "neutral"),
    ),
}

# 离开时长分档（秒）→ 池子名
_GAP_TIERS: tuple[tuple[float, str], ...] = (
    (1800.0, "return_brief"),      # < 30 分钟
    (10800.0, "return_mid"),       # < 3 小时
    (float("inf"), "return_long"),  # 更久
)

DEFAULT_HISTORY = 6


class LineBank:
    """带"最近说过"记忆的台词池。

    Args:
        pools: 情境 → ((文本, 情绪), ...)；默认用模块级 :data:`POOLS`。
        history: 每个情境记住最近多少条（防止立刻重复）。
        rng: 随机源，默认 ``random.Random()``；测试可注入固定种子。
    """

    def __init__(
        self,
        pools: Optional[dict] = None,
        history: int = DEFAULT_HISTORY,
        rng: Optional[random.Random] = None,
    ):
        self._pools = dict(pools) if pools is not None else dict(POOLS)
        self._history = max(0, int(history))
        self._rng = rng or random.Random()
        self._lock = threading.Lock()
        self._recent: dict[str, list[str]] = {}
        # 给测试与状态口用
        self.picked = 0

    # ── 查询 ──────────────────────────────────────────────

    def keys(self) -> list[str]:
        return sorted(self._pools)

    def pool_size(self, key: str) -> int:
        return len(self._pools.get(key, ()))

    def recent(self, key: str) -> list[str]:
        with self._lock:
            return list(self._recent.get(key, ()))

    # ── 取词 ──────────────────────────────────────────────

    def pick(self, key: str) -> Optional[dict]:
        """取一条台词。返回 ``{"text", "emotion"}``；未知情境/空池返回 None。

        保证：**不会取到最近 ``history`` 条里刚说过的**——历史长度会被钳到
        ``len(pool)-1``，所以哪怕池子只有 2 句也一定轮换。
        """
        pool = self._pools.get(key) or ()
        if not pool:
            logger.debug("liveliness: 情境 %r 没有台词池", key)
            return None

        with self._lock:
            recent = self._recent.get(key, [])
            limit = min(self._history, max(0, len(pool) - 1))
            banned = set(recent[-limit:]) if limit else set()
            candidates = [p for p in pool if p[0] not in banned] or list(pool)
            text, emotion = self._rng.choice(candidates)
            # 记历史（按文本去重后追加）
            recent = [t for t in recent if t != text] + [text]
            self._recent[key] = recent[-(self._history + 1):]
            self.picked += 1
        return {"text": text, "emotion": emotion}


# ── 情境选择（"逻辑要生动"的那一半）──────────────────────

def is_night(hour: int) -> bool:
    """深夜 = 23 点后或凌晨 5 点前。"""
    return hour >= 23 or hour < 5


def greeting_key(gap_seconds: float, hour: Optional[int] = None) -> str:
    """按"离开多久 + 现在几点"选情境。

    深夜优先：凌晨三点回来，重点不是"你走了多久"，是"这个点了"。
    """
    if hour is not None and is_night(int(hour)):
        return "return_night"
    try:
        gap = max(0.0, float(gap_seconds))
    except (TypeError, ValueError):
        gap = 0.0
    for threshold, key in _GAP_TIERS:
        if gap < threshold:
            return key
    return "return_long"


_bank: Optional[LineBank] = None
_bank_lock = threading.Lock()


def get_bank() -> LineBank:
    """进程级台词池（懒建）。"""
    global _bank
    with _bank_lock:
        if _bank is None:
            _bank = LineBank()
        return _bank


def greeting(gap_seconds: float, hour: Optional[int] = None,
             bank: Optional[LineBank] = None) -> dict:
    """回到电脑前该说什么 —— 直接给 ``_show_bubble`` 用。

    Returns:
        ``{"text", "emotion"}``；池子异常时给一条保底，**永不返回 None**
        （打招呼是"该说话"的场合，静默比说错更怪）。
    """
    b = bank or get_bank()
    picked = b.pick(greeting_key(gap_seconds, hour))
    if picked is None:
        return {"text": "回来啦～", "emotion": "happy"}
    return picked
