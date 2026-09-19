"""游戏白名单与识别（陪玩方向 P0-1）。

## 为什么是"内置表 + 可覆盖"而不是"让用户填配置"

用户明确偏好「别让我手动配」（动作从 ``motions/`` 自动扫、不愿手写清单）。
所以这里三层兜底，**零配置即可用**：

1. **内置表** ``DEFAULT_GAMES``：覆盖常见游戏（鸣潮 / 原神 / 星穹铁道 / LOL
   / Minecraft / 泰拉瑞亚 等），开箱即用；
2. **配置覆盖**：``game.games`` 里同 ``id`` 覆盖内置项，新 ``id`` 追加，
   用来加冷门游戏或改显示名；
3. **通用兜底**：认不出白名单、但前台分类确实是 ``gaming`` →
   退化成一条 ``generic`` 条目（名字取窗口标题），未知游戏也能进陪玩流程，
   **不会被静默丢掉**。

## 匹配规则

先按进程名、再按窗口标题（进程更硬）。两者都大小写不敏感，
支持 ``*`` 通配与子串两种写法。识别的是**当前前台窗口**，不看后台。

## 边界

只读进程名与窗口标题，不读内存、不注入、不注入键鼠。见 PRD §5.1。
"""
from __future__ import annotations

import fnmatch
import logging
import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

# 游戏类型（对齐 PRD §1.2 的目标场景，供 P0-3 文案池分型用）
TYPE_GACHA_OPENWORLD = "gacha_openworld"   # 二次元大世界（原神 / 星穹铁道 / 鸣潮）
TYPE_MOBA = "moba"                         # MOBA 对战（英雄联盟 / DOTA2）
TYPE_STEAM_SINGLE = "steam_single"         # Steam 单机（泰拉瑞亚 / 红警）
TYPE_UNKNOWN = "unknown"                   # 白名单外的游戏


@dataclass
class GameEntry:
    """一款游戏的白名单条目。"""

    id: str
    name: str
    type: str = TYPE_UNKNOWN
    process: list[str] = field(default_factory=list)   # 进程名模式（* 通配 / 子串）
    title: list[str] = field(default_factory=list)     # 窗口标题关键词
    generic: bool = False                              # 是否"未识别游戏的通用兜底"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "type": self.type,
            "process": list(self.process),
            "title": list(self.title),
            "generic": self.generic,
        }


# ── 内置表（开箱即用，不需要写配置）────────────────────────

DEFAULT_GAMES: list[GameEntry] = [
    GameEntry(
        id="wuthering_waves",
        name="鸣潮",
        type=TYPE_GACHA_OPENWORLD,
        # UE 打包常叫 Client-Win64-Shipping.exe，光看进程会误伤别的 UE 游戏，
        # 所以标题关键词是主力判据。
        process=["wuthering*", "client-win64-shipping.exe"],
        title=["鸣潮", "wuthering waves"],
    ),
    GameEntry(
        id="genshin",
        name="原神",
        type=TYPE_GACHA_OPENWORLD,
        process=["yuanshen.exe", "genshinimpact.exe"],
        title=["原神", "genshin impact"],
    ),
    GameEntry(
        id="star_rail",
        name="崩坏：星穹铁道",
        type=TYPE_GACHA_OPENWORLD,
        process=["starrail.exe"],
        title=["星穹铁道", "star rail"],
    ),
    GameEntry(
        id="lol",
        name="英雄联盟",
        type=TYPE_MOBA,
        process=["league of legends.exe", "leagueclient.exe"],
        title=["league of legends", "英雄联盟"],
    ),
    GameEntry(
        id="dota2",
        name="DOTA 2",
        type=TYPE_MOBA,
        process=["dota2.exe"],
        title=["dota 2"],
    ),
    GameEntry(
        id="minecraft",
        name="我的世界",
        type=TYPE_STEAM_SINGLE,
        # 原版 JVM 进程名无信息量，必须靠标题
        process=["minecraft*"],
        title=["minecraft", "我的世界"],
    ),
    GameEntry(
        id="terraria",
        name="泰拉瑞亚",
        type=TYPE_STEAM_SINGLE,
        process=["terraria.exe"],
        title=["terraria", "泰拉瑞亚"],
    ),
    GameEntry(
        id="onimusha",
        name="鬼武者",
        type=TYPE_STEAM_SINGLE,
        process=["onimusha*"],
        title=["鬼武者", "onimusha"],
    ),
]


# ── 匹配 ──────────────────────────────────────────────────

def _hit(value: str, pattern: str) -> bool:
    """单个模式命中判定：支持 ``*`` 通配，也支持纯子串。"""
    if not value or not pattern:
        return False
    p = pattern.strip().lower()
    if not p:
        return False
    if "*" in p or "?" in p:
        return fnmatch.fnmatch(value, p)
    return p in value


def match_game(process: str, title: str,
               games: Iterable[GameEntry]) -> Optional[GameEntry]:
    """匹配当前前台窗口属于哪款游戏；认不出返回 None。

    两轮：先全表比进程名（更硬），再全表比窗口标题。
    不是"逐条目先比进程再比标题"——否则一个标题通用的条目会抢走进程精确的条目。
    """
    low_p = (process or "").strip().lower()
    low_t = (title or "").strip().lower()
    entries = list(games)

    for g in entries:
        for pat in g.process:
            if _hit(low_p, pat):
                return g
    for g in entries:
        for kw in g.title:
            if _hit(low_t, kw):
                return g
    return None


def generic_game(title: str, process: str = "") -> GameEntry:
    """白名单外、但确实在玩游戏时的通用兜底条目。

    名字取窗口标题（去掉常见后缀），没有标题才退回进程名。
    """
    name = _clean_title(title) or (process or "").strip() or "未知游戏"
    return GameEntry(
        id="generic",
        name=name,
        type=TYPE_UNKNOWN,
        process=[],
        title=[],
        generic=True,
    )


_TITLE_NOISE = re.compile(
    r"\s*[-–—]\s*(steam|epic games|battle\.net|ubisoft connect)\s*$",
    re.IGNORECASE,
)


def _clean_title(title: str) -> str:
    """窗口标题 → 可展示的游戏名（去平台后缀、限长）。"""
    t = (title or "").strip()
    if not t:
        return ""
    t = _TITLE_NOISE.sub("", t).strip()
    return t[:40]


# ── 配置合并 ──────────────────────────────────────────────

def _entry_from_config(raw: dict) -> Optional[GameEntry]:
    """把配置里的一项变成 GameEntry；缺 id 的项直接丢弃（不猜）。"""
    if not isinstance(raw, dict):
        return None
    gid = str(raw.get("id") or "").strip()
    if not gid:
        logger.warning("game.games 里有一项缺 id，已忽略: %r", raw)
        return None
    proc = raw.get("process") or []
    titl = raw.get("title") or []
    if isinstance(proc, str):
        proc = [proc]
    if isinstance(titl, str):
        titl = [titl]
    return GameEntry(
        id=gid,
        name=str(raw.get("name") or gid),
        type=str(raw.get("type") or TYPE_UNKNOWN),
        process=[str(p) for p in proc if str(p).strip()],
        title=[str(t) for t in titl if str(t).strip()],
    )


def load_games(game_cfg: Optional[dict] = None) -> list[GameEntry]:
    """内置表 + 配置覆盖 → 最终白名单。

    Args:
        game_cfg: ``config.json`` 里的 ``game`` 块（可为 None）。

    Returns:
        合并后的列表。配置中的项**原地覆盖**同 id 的内置项（顺序不变），
        新 id 追加到末尾；``disabled`` 列表里的 id 被移除。
    """
    cfg = game_cfg or {}
    games: list[GameEntry] = [GameEntry(**{
        "id": g.id, "name": g.name, "type": g.type,
        "process": list(g.process), "title": list(g.title),
    }) for g in DEFAULT_GAMES]
    index = {g.id: g for g in games}

    extra = cfg.get("games")
    if isinstance(extra, list):
        for raw in extra:
            ent = _entry_from_config(raw)
            if ent is None:
                continue
            if ent.id in index:
                pos = games.index(index[ent.id])
                games[pos] = ent
                index[ent.id] = ent
            else:
                games.append(ent)
                index[ent.id] = ent

    disabled = cfg.get("disabled") or []
    if isinstance(disabled, str):
        disabled = [disabled]
    if disabled:
        drop = {str(d).strip() for d in disabled}
        games = [g for g in games if g.id not in drop]

    return games
