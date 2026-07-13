"""增强环境扫描器 — M2 环境感知模块

在 ForegroundWatcher 基础上提供：
- 窗口标题解析（应用名 + 文件名）
- 文件类型推断
- 应用类别识别
- 虚拟物件注册表

仅使用标准库，无外部依赖。
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ════════════════════════════════════════════════════════════
#  数据结构
# ════════════════════════════════════════════════════════════

@dataclass
class EnvironmentSnapshot:
    """环境快照 — 一次 scan() 的完整结果"""
    foreground_app: str = ""           # 前台应用名
    window_title: str = ""             # 完整窗口标题
    category: str = "other"            # writing / development / gaming / communication / media / other
    detected_files: list[str] = field(default_factory=list)   # 检测到的文件名
    file_types: dict[str, str] = field(default_factory=dict)  # 文件名 → 类型
    screen_description: str = ""       # 屏幕感知一句话（由 ScreenPerception 填充）
    time_context: dict = field(default_factory=dict)         # 时间上下文
    raw_title: str = ""                # 原始未解析标题（调试用）


# ════════════════════════════════════════════════════════════
#  窗口标题解析
# ════════════════════════════════════════════════════════════

# 常见分隔符模式
_TITLE_SEPARATORS = [
    # "filename.ext - AppName"  或  "filename.ext — AppName"
    r'^(.+?)\s+[-–—]\s+(.+)$',
    # "AppName - filename.ext"
    r'^(.+?)\s+[-–—]\s+(.+)$',
    # 竖线分隔："AppName | filename"
    r'^(.+?)\s*\|\s*(.+)$',
    # 冒号分隔（部分 macOS 风格）："AppName: filename"
    r'^(.+?):\s*(.+)$',
]

# 已知扩展名列表（用于判断哪边是文件名）
_KNOWN_EXTS = {
    '.py', '.js', '.ts', '.jsx', '.tsx', '.html', '.css', '.scss', '.json',
    '.md', '.txt', '.docx', '.pdf', '.xlsx', '.pptx', '.csv',
    '.png', '.jpg', '.jpeg', '.gif', '.psd', '.fig', '.svg',
    '.mp4', '.mov', '.avi', '.mkv', '.webm',
    '.zip', '.rar', '.7z', '.tar', '.gz',
}


def _has_known_ext(text: str) -> bool:
    """检查文本是否包含已知文件扩展名"""
    basename = text.strip().split('\\')[-1].split('/')[-1]
    for ext in _KNOWN_EXTS:
        if basename.endswith(ext):
            return True
    return False


def parse_window_title(title: str) -> tuple[str, list[str]]:
    """从窗口标题解析应用名和文件名列表

    支持格式：
        "main.py - VS Code"          → app="VS Code", files=["main.py"]
        "VS Code - main.py"          → app="VS Code", files=["main.py"]
        "Steam"                      → app="Steam", files=[]
        "Obsidian - 笔记.md"          → app="笔记.md", files=["笔记.md"]  (macOS 风格)
        "Notepad++ - test.py"        → app="Notepad++", files=["test.py"]

    解析策略：
        1. 按分隔符 split，取两段
        2. 含已知扩展名的那段视为文件名
        3. 不含扩展名的那段视为应用名
        4. 如果两边都有扩展名（罕见），优先选较短的为文件名
        5. 没有分隔符时，整串当应用名
    """
    if not title or not title.strip():
        return ("", [])

    title = title.strip()

    for pattern in _TITLE_SEPARATORS:
        m = re.match(pattern, title)
        if m:
            left = m.group(1).strip()
            right = m.group(2).strip()

            # 清理常见 UI 后缀
            def clean(s: str) -> str:
                for suffix in [' - 浏览器', ' - 应用', ' (Administrator)', ' (管理员)']:
                    s = s.replace(suffix, '')
                return s.strip()

            left = clean(left)
            right = clean(right)

            if not left or not right:
                continue

            left_has_ext = _has_known_ext(left)
            right_has_ext = _has_known_ext(right)

            if left_has_ext and not right_has_ext:
                return (right, [left])
            elif right_has_ext and not left_has_ext:
                return (left, [right])
            elif left_has_ext and right_has_ext:
                # 两边都有扩展名：短的通常是文件名
                if len(left) < len(right):
                    return (right, [left])
                return (left, [right])
            else:
                # 两边都没有扩展名，靠启发式判断
                # 应用名通常较长且不含常见文档扩展名
                # 这里假设右边是文件名（多数编辑器风格）
                return (left, [right])

    # 没有分隔符 → 整串当应用名
    return (title, [])


# ════════════════════════════════════════════════════════════
#  文件类型推断
# ════════════════════════════════════════════════════════════

FILE_TYPE_MAP: dict[str, str] = {
    # 代码
    ".py": "code", ".js": "code", ".ts": "code", ".jsx": "code", ".tsx": "code",
    ".html": "code", ".htm": "code", ".css": "code", ".scss": "code",
    ".json": "code", ".xml": "code", ".yaml": "code", ".yml": "code",
    ".md": "code", ".rst": "code", ".sh": "code", ".bat": "code",
    ".java": "code", ".c": "code", ".cpp": "code", ".cs": "code",
    ".go": "code", ".rs": "code", ".rb": "code", ".php": "code",
    ".vue": "code", ".svelte": "code",
    # 文档
    ".docx": "document", ".doc": "document", ".pdf": "document",
    ".txt": "document", ".rtf": "document", ".odt": "document",
    ".xlsx": "document", ".xls": "document", ".csv": "document",
    ".pptx": "document", ".ppt": "document",
    # 图片
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".gif": "image",
    ".psd": "image", ".fig": "image", ".svg": "image", ".bmp": "image",
    ".webp": "image", ".tiff": "image", ".ico": "image",
    # 视频
    ".mp4": "video", ".mov": "video", ".avi": "video",
    ".mkv": "video", ".webm": "video", ".flv": "video",
    # 音频
    ".mp3": "audio", ".wav": "audio", ".flac": "audio",
    ".aac": "audio", ".ogg": "audio",
    # 压缩包
    ".zip": "archive", ".rar": "archive", ".7z": "archive",
    ".tar": "archive", ".gz": "archive",
}


def infer_file_type(filename: str) -> str:
    """根据扩展名推断文件类型

    返回: code / document / image / video / audio / archive / unknown
    """
    if not filename:
        return "unknown"

    # 提取扩展名
    path_part = filename.split('\\')[-1].split('/')[-1]
    _, ext = Path(path_part).suffix.lower(), None

    # 正确获取扩展名
    dot_idx = path_part.rfind('.')
    if dot_idx >= 0:
        ext = path_part[dot_idx:].lower()

    return FILE_TYPE_MAP.get(ext, "unknown")


# ════════════════════════════════════════════════════════════
#  应用类别识别
# ════════════════════════════════════════════════════════════

# 应用名 → 类别 映射表（小写匹配）
APP_CATEGORY_MAP: dict[str, str] = {
    # Development
    "vs code": "development", "visual studio": "development", "pycharm": "development",
    "idea": "development", "android studio": "development", "eclipse": "development",
    "sublime": "development", "atom": "development", "notepad++": "development",
    "terminal": "development", "cmd": "development", "powershell": "development",
    "git bash": "development", "warp": "development", "hyper": "development",
    "cursor": "development", "zed": "development",

    # Writing
    "word": "writing", "onenote": "writing", "typora": "writing",
    "obsidian": "writing", "notion": "writing", "google docs": "writing",
    "wps": "writing", "pages": "writing", "libreoffice": "writing",
    "bear": "writing", "joplin": "writing", "logseq": "writing",

    # Gaming
    "steam": "gaming", "epic games": "gaming", "epic game launcher": "gaming",
    "minecraft": "gaming", "valorant": "gaming", "league": "gaming",
    "gog": "gaming", "battle.net": "gaming", "xbox": "gaming",
    "playstation": "gaming", "nintendo": "gaming", "roblox": "gaming",

    # Communication
    "wechat": "communication", "weixin": "communication", "微信": "communication",
    "qq": "communication", "telegram": "communication", "discord": "communication",
    "slack": "communication", "teams": "communication", "zoom": "communication",
    "skype": "communication", "dingtalk": "communication", "飞书": "communication",
    "企业微信": "communication", "whatsapp": "communication",

    # Media
    "spotify": "media", "vlc": "media", "photoshop": "media", "ps": "media",
    "premiere": "media", "after effects": "media", "audition": "media",
    "illustrator": "media", "lightroom": "media", "gimp": "media",
    "krita": "media", "blender": "media", "firefox": "media",
    "chrome": "media", "edge": "media", "safari": "media",
    "bilibili": "media", "youtube": "media", "netflix": "media",
    "music": "media", "网易云音乐": "media", "qq音乐": "media",
    "spotify": "media",

    # Design
    "figma": "design", "sketch": "design", "canva": "design",
    "affinity": "design", "inkscape": "design", "draw.io": "design",
}


def infer_app_category(app_name: str) -> str:
    """根据应用名推断类别

    返回: development / writing / gaming / communication / media / design / other
    """
    if not app_name:
        return "other"

    lower = app_name.lower()

    # 精确匹配优先
    for name, category in APP_CATEGORY_MAP.items():
        if name == lower:
            return category

    # 子串匹配
    for name, category in APP_CATEGORY_MAP.items():
        if name in lower:
            return category

    return "other"


# ════════════════════════════════════════════════════════════
#  增强环境扫描器
# ════════════════════════════════════════════════════════════

class EnhancedEnvironmentScanner:
    """增强环境扫描器

    职责：
    1. 解析窗口标题 → 应用名 + 文件名
    2. 推断文件类型
    3. 识别应用类别
    4. 生成 EnvironmentSnapshot

    用法：
        scanner = EnhancedEnvironmentScanner()
        snapshot = scanner.scan(window_title="main.py - VS Code")
        print(snapshot.category)  # "development"
        print(snapshot.file_types)  # {"main.py": "code"}
    """

    def __init__(self):
        self._file_type_map = FILE_TYPE_MAP.copy()
        self._app_category_map = APP_CATEGORY_MAP.copy()

    # ── 可扩展接口 ──

    def add_file_type(self, ext: str, category: str):
        """添加新的文件类型映射"""
        self._file_type_map[ext.lower()] = category

    def add_app_category(self, app_pattern: str, category: str):
        """添加新的应用类别映射"""
        self._app_category_map[app_pattern.lower()] = category

    # ── 核心方法 ──

    def parse_window_title(self, title: str) -> tuple[str, list[str]]:
        """从窗口标题解析应用名和文件名列表"""
        return parse_window_title(title)

    def infer_file_type(self, filename: str) -> str:
        """推断文件类型"""
        return infer_file_type(filename)

    def infer_app_category(self, app_name: str) -> str:
        """识别应用类别"""
        return infer_app_category(app_name)

    def scan(
        self,
        window_title: str = "",
        screen_description: str = "",
        time_context: dict | None = None,
    ) -> EnvironmentSnapshot:
        """生成完整环境快照

        Args:
            window_title: 当前前台窗口标题
            screen_description: 屏幕视觉感知描述（由 ScreenPerception 传入）
            time_context: 时间上下文（由 TimePerception 传入）

        Returns:
            EnvironmentSnapshot
        """
        app_name, files = self.parse_window_title(window_title)
        category = self.infer_app_category(app_name) if app_name else "other"

        file_types: dict[str, str] = {}
        for f in files:
            file_types[f] = self.infer_file_type(f)

        return EnvironmentSnapshot(
            foreground_app=app_name,
            window_title=window_title,
            category=category,
            detected_files=files,
            file_types=file_types,
            screen_description=screen_description,
            time_context=time_context or {},
            raw_title=window_title,
        )

    def get_observation(self, snapshot: EnvironmentSnapshot) -> str:
        """从快照生成一句自然语言观察（供叙事引擎使用）

        例: "用户在 VS Code 里编辑 main.py"
        例: "打开了 Steam，要打游戏了？"
        """
        parts = []

        if snapshot.detected_files:
            file_list = ", ".join(snapshot.detected_files)
            type_labels = {
                "code": "代码", "document": "文档", "image": "图片",
                "video": "视频", "audio": "音频", "archive": "压缩包",
            }
            type_desc = []
            for f in snapshot.detected_files:
                ft = snapshot.file_types.get(f, "unknown")
                label = type_labels.get(ft, ft)
                type_desc.append(f"{f}({label})")
            parts.append(f"看到你在看 {', '.join(type_desc)}")
        else:
            parts.append(f"你在用 {snapshot.foreground_app}")

        if snapshot.screen_description:
            parts.append(snapshot.screen_description)

        return "；".join(parts) if parts else ""


# ════════════════════════════════════════════════════════════
#  虚拟物件注册表
# ════════════════════════════════════════════════════════════

@dataclass
class VirtualObject:
    """虚拟物件 — 桌宠可以在桌面上放置的小物件"""
    emoji: str
    label: str
    position: tuple[int, int] = (0, 0)
    duration: float = 10.0
    created_at: float = field(default_factory=time.time)

    def is_expired(self) -> bool:
        return time.time() - self.created_at > self.duration


# 预定义物件库
DEFAULT_OBJECTS: dict[str, VirtualObject] = {
    "coffee": VirtualObject("☕", "一杯热咖啡", duration=15),
    "snack": VirtualObject("🍕", "零食时间！", duration=10),
    "book": VirtualObject("📖", "在看书呢", duration=20),
    "star": VirtualObject("⭐", "你真棒！", duration=8),
    "idea": VirtualObject("💡", "想到好主意了", duration=12),
    "game": VirtualObject("🎮", "打游戏去！", duration=10),
    "sleepy": VirtualObject("😴", "困了...", duration=15),
    "writing": VirtualObject("✏️", "在写东西", duration=20),
    "rain": VirtualObject("🌧️", "下雨了", duration=30),
    "music": VirtualObject("🎵", "在听音乐？", duration=15),
}


class VirtualObjectRegistry:
    """虚拟物件注册表

    管理所有桌宠放置的虚拟物件，支持创建、查询、过期清理。
    """

    def __init__(self):
        self._objects: list[VirtualObject] = []
        self._default_pool: dict[str, VirtualObject] = DEFAULT_OBJECTS.copy()

    def add(self, emoji: str, label: str, position: tuple[int, int] = (0, 0),
            duration: float = 10.0) -> VirtualObject:
        """添加一个虚拟物件"""
        obj = VirtualObject(emoji=emoji, label=label, position=position, duration=duration)
        self._objects.append(obj)
        return obj

    def add_from_default(self, key: str, position: tuple[int, int] = (0, 0)) -> VirtualObject | None:
        """从默认物件库添加"""
        template = self._default_pool.get(key)
        if template:
            return self.add(
                emoji=template.emoji,
                label=template.label,
                position=position,
                duration=template.duration,
            )
        return None

    def get_active(self) -> list[VirtualObject]:
        """获取所有未过期的物件"""
        return [o for o in self._objects if not o.is_expired()]

    def cleanup(self) -> int:
        """清理过期物件，返回移除数量"""
        before = len(self._objects)
        self._objects = [o for o in self._objects if not o.is_expired()]
        return before - len(self._objects)

    @property
    def count(self) -> int:
        return len(self._objects)

    @property
    def active_count(self) -> int:
        return len(self.get_active())
