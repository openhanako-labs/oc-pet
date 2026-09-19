"""前景窗口检测 — 检测用户当前使用的应用，映射到桌宠情绪/气泡。

纯 ctypes，零外部依赖。每 2 秒轮询一次，前台切换时触发回调。

用法:
    watcher = ForegroundWatcher()
    watcher.on_change = lambda app, category: print(f"切换到 {app} ({category})")
    watcher.start()
"""
from __future__ import annotations

import ctypes
import fnmatch
import logging
import time
from ctypes import wintypes
from pathlib import Path

logger = logging.getLogger(__name__)


# ── Windows API ───────────────────────────────────────────

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
psapi = ctypes.windll.psapi


def _get_foreground_window_title() -> str:
    """获取当前前台窗口标题"""
    hwnd = user32.GetForegroundWindow()
    length = user32.GetWindowTextLengthW(hwnd)
    if length == 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value or ""


def _get_foreground_window_rect() -> tuple[int, int, int, int] | None:
    """获取当前前台窗口的位置和大小
    
    Returns:
        (x, y, width, height) 或 None
    """
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    
    x = rect.left
    y = rect.top
    width = rect.right - rect.left
    height = rect.bottom - rect.top
    
    return (x, y, width, height)


def _get_foreground_process_name() -> str:
    """获取当前前台窗口所属进程名（如 Obsidian.exe）"""
    hwnd = user32.GetForegroundWindow()
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    try:
        handle = kernel32.OpenProcess(
            0x0400 | 0x0010,  # PROCESS_QUERY_INFORMATION | PROCESS_VM_READ
            False,
            pid.value,
        )
        if not handle:
            return ""
        buf = ctypes.create_unicode_buffer(260)
        size = wintypes.DWORD(260)
        if psapi.GetModuleBaseNameW(handle, None, buf, size):
            return buf.value or ""
    finally:
        if handle:
            kernel32.CloseHandle(handle)
    return ""


# ── 应用分类映射 ──────────────────────────────────────────

# 进程名 → 分类
APP_CATEGORY_MAP: dict[str, str] = {
    # 写作
    "Obsidian.exe": "writing",
    "Typora.exe": "writing",
    "Notion.exe": "writing",
    "Word.exe": "writing",
    "WINWORD.EXE": "writing",
    "Scrivener.exe": "writing",
    # 开发
    "devenv.exe": "development",
    "Code.exe": "development",
    "cursor.exe": "development",
    "idea64.exe": "development",
    "pycharm64.exe": "development",
    "sublime_text.exe": "development",
    "notepad++.exe": "development",
    "WindowsTerminal.exe": "development",
    "Terminal.exe": "development",
    # 浏览器
    "chrome.exe": "browsing",
    "msedge.exe": "browsing",
    "firefox.exe": "browsing",
    "brave.exe": "browsing",
    # 游戏
    "steam.exe": "gaming",
    "GenshinImpact.exe": "gaming",
    "YuanShen.exe": "gaming",
    "StarRail.exe": "gaming",
    # 通讯
    "WeChat.exe": "communication",
    "Telegram.exe": "communication",
    "QQ.exe": "communication",
    "DingTalk.exe": "communication",
    "Feishu.exe": "communication",
    "slack.exe": "communication",
    "Discord.exe": "communication",
    # 娱乐
    "Spotify.exe": "entertainment",
    "bilibili.exe": "entertainment",
    "vlc.exe": "entertainment",
    "mpc-hc64.exe": "entertainment",
    "Netflix.exe": "entertainment",
}


def classify_app(process_name: str) -> str:
    """根据进程名分类应用"""
    direct = APP_CATEGORY_MAP.get(process_name, "other")
    if direct != "other":
        return direct
    # P6: 模糊匹配兜底——不依赖精确 exe 名，覆盖鸣潮/星穹/崩坏/原神等中英文进程名
    low = str(process_name).lower()
    GAME_KEYWORDS = (
        "wuthering", "鸣潮", "starrail", "星穹", "genshin", "yuanshen",
        "原神", "崩坏", "bh3", "honkai", "zzz", "绝区零", "minecraft",
        "dota", "lol", "valorant", "apex", "csgo", "cs2", "elden", "steam",
    )
    if any(k in low for k in GAME_KEYWORDS):
        return "gaming"
    return "other"


# ── 顶层窗口枚举（给「游戏还在跑吗」用）────────────────────

_EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def list_visible_windows() -> list[dict] | None:
    """枚举所有**可见的**顶层窗口。

    Returns:
        ``[{"process": 进程名, "title": 标题}]``；
        **整体枚举失败时返回 None**（与「没找到」区分开——调用方需要
        知道“是没这个进程”还是“探测本身失败了”）。

    用于判断“游戏切到后台后进程是否还在”。纯 ctypes，零依赖。
    提不到进程名（权限不足 / 已退出）的窗口其 ``process`` 为空串。
    """
    out: list[dict] = []

    def _cb(hwnd, _lparam):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            out.append({"process": _process_name_of_pid(pid.value), "title": buf.value or ""})
        except Exception:  # noqa: BLE001 — 单个窗口出错不影响整体枚举
            pass
        return True

    try:
        ok = user32.EnumWindows(_EnumWindowsProc(_cb), 0)
    except Exception as e:  # noqa: BLE001
        logger.debug("EnumWindows 失败: %s", e)
        return None
    if not ok and not out:
        return None
    return out


def _process_name_of_pid(pid: int) -> str:
    """PID → 进程名（提不到返回空串）。"""
    if not pid:
        return ""
    handle = kernel32.OpenProcess(0x0400 | 0x0010, False, pid)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(260)
        size = wintypes.DWORD(260)
        if psapi.GetModuleBaseNameW(handle, None, buf, size):
            return buf.value or ""
    except Exception:  # noqa: BLE001
        pass
    finally:
        kernel32.CloseHandle(handle)
    return ""


def find_process_windows(patterns) -> list[dict] | None:
    """找出进程名命中任一 pattern 的可见窗口。

    Args:
        patterns: 进程名模式序列（支持 ``*`` 通配与子串，大小写不敏感）。

    Returns:
        命中窗口列表；**整体枚举失败返回 None**（调用方按“未知”处理，
        不要当成“进程不存在”）。

    注意：提不到进程名的窗口不参与匹配（权限不足时会漏判，
    所以调用方在拿到空列表时仍应区分“枚举成功但没命中”）。
    """
    wins = list_visible_windows()
    if wins is None:
        return None
    pats = [str(p).strip().lower() for p in (patterns or []) if str(p).strip()]
    if not pats:
        return []
    hits: list[dict] = []
    for w in wins:
        name = str(w.get("process") or "").strip().lower()
        if not name:
            continue
        for p in pats:
            if ("*" in p or "?" in p):
                if fnmatch.fnmatch(name, p):
                    hits.append(w)
                    break
            elif p in name:
                hits.append(w)
                break
    return hits


# ── 情绪映射 ─────────────────────────────────────────────

CATEGORY_EMOTIONS: dict[str, dict] = {
    "writing":       {"mood": "happy",   "text": "{msg}"},
    "development":   {"mood": "working", "text": "{msg}"},
    "browsing":      {"mood": "cute",    "text": "{msg}"},
    "gaming":        {"mood": "happy",   "text": "{msg}"},
    "communication": {"mood": "idle",    "text": "{msg}"},
    "entertainment": {"mood": "cute",    "text": "{msg}"},
    "other":         {"mood": "idle",    "text": None},
    "uncategorized": {"mood": "idle",    "text": None},
}

CATEGORY_MESSAGES: dict[str, list[str]] = {
    "writing":       ["在写东西呢～", "在码字呀", "思绪在流淌呢"],
    "development":   ["在写代码呢", "敲代码中——", "啊，这个 bug 我看到了"],
    "browsing":      ["在网上逛呢～", "在看什么好东西？"],
    "gaming":        ["在冒险呀！", "这波操作我可以看一天", "上啊！"],
    "communication": ["在聊天呐", "跟谁说话呢——"],
    "entertainment": ["在看视频呢～", "这个我看过！", "好看吗？"],
}


# ── ForegroundWatcher ─────────────────────────────────────

class ForegroundWatcher:
    """前台窗口轮询器 — 检测应用切换，触发回调。

    回调签名: on_change(app_name: str, category: str, title: str)
      - app_name: 进程名（如 "Obsidian.exe"）
      - category: 分类（如 "writing"）
      - title: 窗口标题
    """

    def __init__(self):
        self._last_app: str = ""
        self._last_category: str = ""
        self._last_title: str = ""
        self._started: bool = False

        # 回调（外部设置）
        self.on_change: callable = lambda app, cat, title: None

        # 应用切换冷却（避免频繁切换时的轰炸）
        self._cooldown_until: float = 0
        self.cooldown_seconds: float = 5.0

        # ── P1 意图分类输入 ──
        # 当前分类的持续时长（分钟）：同一 category 不变则累加
        self._fg_since: float = time.time()
        self._fg_category: str = ""
        # 5 分钟窗口内的切窗次数（用于"频繁切窗→可能卡住"判定）
        self._switch_times: list[float] = []

    @property
    def fg_duration_min(self) -> float:
        """当前前台分类的持续时长（分钟）"""
        return max(0.0, (time.time() - self._fg_since) / 60.0)

    @property
    def window_switches_5min(self) -> int:
        """最近 5 分钟内切换前台窗口的次数"""
        now = time.time()
        return sum(1 for t in self._switch_times if now - t <= 300)

    def start(self):
        """开始轮询（由 QTimer 驱动，每 2 秒 tick）"""
        self._started = True

    @property
    def last_app(self) -> str:
        """最近一次识别的前台进程名"""
        return self._last_app

    @property
    def last_category(self) -> str:
        """最近一次识别的前台窗口分类"""
        return self._last_category

    def stop(self):
        self._started = False

    def tick(self) -> dict | None:
        """每次调用检测一次前台变化。由 pet.py 的 QTimer 周期性调用。

        Returns:
            如果前台变了，返回 {"app": 进程名, "category": 分类, "title": 窗口标题}
            否则返回 None
        """
        if not self._started:
            return None

        now = time.time()

        app = _get_foreground_process_name()
        title = _get_foreground_window_title()
        category = classify_app(app)

        # 没变化
        if app == self._last_app and title == self._last_title:
            return None

        # 冷却中：记录新状态但不触发回调（避免切换轰炸），
        # 这样冷却结束后能正确对比，不会把冷却期内的切换误判为"没变"。
        if now < self._cooldown_until:
            self._last_app = app
            self._last_category = category
            self._last_title = title
            return None

        self._last_app = app
        self._last_category = category
        self._last_title = title
        self._cooldown_until = now + self.cooldown_seconds

        # ── P1: 更新意图分类输入 ──
        # 分类变化 → 重置持续时长；同一分类延续 → 保持
        if category != self._fg_category:
            self._fg_category = category
            self._fg_since = now
        # 记录切窗（5 分钟窗口，懒清理）
        self._switch_times.append(now)
        self._switch_times = [t for t in self._switch_times if now - t <= 300]

        info = {"app": app, "category": category, "title": title}
        logger.debug("Foreground changed: %s (%s)", app, category)
        self.on_change(app, category, title)
        return info

    def get_emotion(self, category: str) -> dict | None:
        """根据分类获取情绪和文案模板。

        Returns:
            {"mood": 情绪名, "text": 文案模板} 或 None（不显示气泡时）
        """
        import random
        cfg = CATEGORY_EMOTIONS.get(category)
        if not cfg:
            return None

        text_template = cfg.get("text")
        if text_template is None:
            return None

        msgs = CATEGORY_MESSAGES.get(category, [])
        msg = random.choice(msgs) if msgs else text_template
        return {"mood": cfg["mood"], "text": msg}