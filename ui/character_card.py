"""角色卡展示面板 — N.E.K.O. 设计语言（P1-7）

UI重构: 继承 PanelWidget 基类，统一标题栏、刷新按钮、关闭按钮

展示当前桌宠角色的「档案卡」：
- 头像：优先角色目录下的 avatar/portrait 图片（圆形裁切），否则首字渐变圆
- 名字：pet.json ``name`` → identity.md 标题 → agent_id
- 身份简介：identity.md 首段 → description.md → pet.json ``description``
- 性格标签：pet.json ``personality/tags/traits`` → identity.md ``## 性格`` 条目
- 记忆统计：事件 / 场景 / 事实 / 反思条数（复用 P0/P1 记忆文件接口，
  文件缺失一律显示 0，绝不崩溃）

数据源（与 HanakoContext 同一读取逻辑，防御式）：
1. 内置角色：``<project>/characters/<agent_id>/``（pet.json / identity.md /
   description.md / avatar.*）
2. Hanako 角色：``~/.hanako/agents/<agent_id>/``（identity.md / description.md）
3. 都取不到 → 纯占位（名字回退 agent_id，其余占位文案）

样式：QWidget + QSS（``ui/theme/neko.qss`` 的 CharacterCard 段），
light/dark 双主题经 ``data-theme`` 动态属性切换，配色全部来自 neko_palette。
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from ui.panel_widget import PanelWidget

from hanako_home import hanako_home

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CHARACTERS_DIR = PROJECT_ROOT / "characters"
DEFAULT_MEMORY_DIR = Path.home() / ".oc-pet" / "memory"
DEFAULT_HANAKO_HOME = hanako_home()

AVATAR_NAMES = ("avatar.png", "avatar.jpg", "avatar.jpeg",
                "portrait.png", "icon.png")
MAX_INTRO_CHARS = 240          # 简介截断
MAX_PERSONALITY_TAGS = 8       # 性格标签上限
TAGS_PER_ROW = 3               # 每行标签数（超出自动换行）

# 记忆统计四类：(key, 显示名, 卡片 accent kind)
STAT_DEFS = (
    ("events", "事件", "event"),
    ("scenes", "场景", "scene"),
    ("facts", "事实", "fact"),
    ("reflections", "反思", "reflection"),
)


# ── 数据读取（纯函数，便于单测）──────────────────────────────────

def _read_text(path: Path) -> str:
    """安全读取文件文本；不存在/读取失败返回空串。"""
    try:
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        logger.warning("角色卡读文件失败 %s: %s", path, exc)
    return ""


def _load_json(path: Path) -> dict:
    """安全读取 JSON 对象；缺失/损坏返回空 dict。"""
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        logger.warning("角色卡读 JSON 失败 %s: %s", path, exc)
    return {}


def _parse_identity(text: str) -> tuple[str, str, list[str]]:
    """从 identity.md 解析 (名称, 简介, 性格标签)。

    - 名称：第一个 ``# 标题``
    - 简介：第一个 ``## 小节`` 之前的首段正文（跨行合并）
    - 性格：``## 性格``（或 个性/character/personality）小节下的 ``- 条目``
    """
    name = ""
    intro = ""
    personality: list[str] = []
    lines = [ln.rstrip() for ln in text.splitlines()]

    # 标题（# xxx）
    for ln in lines:
        m = re.match(r"^#\s+(.+?)\s*$", ln)
        if m:
            name = m.group(1).strip()
            break

    # 简介：第一个 ## 之前的正文段
    body: list[str] = []
    for ln in lines:
        if ln.startswith("##"):
            break
        if ln.startswith("#"):
            continue
        if ln.strip():
            body.append(ln.strip())
    if body:
        intro = " ".join(body)

    # 性格小节条目
    in_section = False
    for ln in lines:
        if ln.startswith("##"):
            in_section = bool(re.match(
                r"^##\s*(性格|个性|character|personality)\s*$", ln,
                re.IGNORECASE,
            ))
            continue
        if in_section:
            m = re.match(r"^[-*•]\s+(.+?)\s*$", ln)
            if m:
                tag = m.group(1).strip()
                if tag:
                    personality.append(tag)
    return name, intro, personality


def read_character_profile(agent_id: str,
                           characters_dir: str | Path | None = None,
                           hanako_home: str | Path | None = None) -> dict:
    """读取角色卡数据。

    返回::

        {"name": str, "intro": str, "personality": list[str],
         "avatar_path": str, "source": "builtin" | "hanako" | "none"}
    """
    agent_id = (agent_id or "default").strip()
    char_dir = Path(characters_dir) if characters_dir else DEFAULT_CHARACTERS_DIR
    hanako_dir = Path(hanako_home) if hanako_home else DEFAULT_HANAKO_HOME

    agent_dir = char_dir / agent_id
    source = "builtin" if agent_dir.is_dir() else "none"
    if not agent_dir.is_dir():
        agent_dir = hanako_dir / "agents" / agent_id
        source = "hanako" if agent_dir.is_dir() else "none"

    name = ""
    intro = ""
    desc = ""
    personality: list[str] = []
    avatar_path = ""

    pet = _load_json(agent_dir / "pet.json")
    if pet:
        name = str(pet.get("name", "") or "").strip()
        desc = str(pet.get("description", "") or "").strip()
        for key in ("personality", "tags", "traits"):
            raw = pet.get(key)
            if isinstance(raw, list):
                tags = [str(x).strip() for x in raw if str(x).strip()]
                if tags:
                    personality = tags[:MAX_PERSONALITY_TAGS]
                    break

    identity = _read_text(agent_dir / "identity.md")
    if identity:
        i_name, i_intro, i_personality = _parse_identity(identity)
        name = name or i_name
        intro = intro or i_intro
        if not personality:
            personality = i_personality[:MAX_PERSONALITY_TAGS]

    desc_md = _read_text(agent_dir / "description.md")
    if desc_md:
        intro = intro or desc_md
    intro = intro or desc or ""
    intro = re.sub(r"\s+", " ", intro).strip()[:MAX_INTRO_CHARS]
    if not name:
        name = agent_id

    for fname in AVATAR_NAMES:
        candidate = agent_dir / fname
        if candidate.is_file():
            avatar_path = str(candidate)
            break

    return {
        "name": name,
        "intro": intro,
        "personality": personality,
        "avatar_path": avatar_path,
        "source": source,
    }


def read_memory_stats(agent_id: str,
                      memory_dir: str | Path | None = None,
                      hanako_home: str | Path | None = None) -> dict:
    """读取当前记忆统计条数。

    返回::

        {"events": int, "scenes": int, "facts": int, "reflections": int}

    取不到的项一律 0（占位），任何文件损坏/缺失都不抛异常。
    """
    d = Path(memory_dir) if memory_dir else DEFAULT_MEMORY_DIR
    hanako_dir = Path(hanako_home) if hanako_home else DEFAULT_HANAKO_HOME
    agent_id = (agent_id or "default").strip()

    # 事件：<agent_id>_events.jsonl 非空行数
    events = 0
    epath = d / f"{agent_id}_events.jsonl"
    try:
        if epath.exists():
            events = sum(
                1 for ln in epath.read_text(encoding="utf-8").splitlines()
                if ln.strip()
            )
    except OSError:
        events = 0

    # 场景：<agent_id>_scenes.json 的 scenes 条数
    scenes = 0
    spath = d / f"{agent_id}_scenes.json"
    data = _load_json(spath)
    scenes_list = data.get("scenes") if isinstance(data, dict) else None
    if isinstance(scenes_list, list):
        scenes = len(scenes_list)

    # 事实：优先 <agent_id>_facts.json（P1-2 FactStore 未来格式）→
    # 其次 Hanako memory/facts.md 条目行数 → 最后陪伴摘要派生事实数
    facts = 0
    fpath = d / f"{agent_id}_facts.json"
    try:
        if fpath.exists():
            fdata = json.loads(fpath.read_text(encoding="utf-8"))
            if isinstance(fdata, list):
                facts = len(fdata)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        logger.debug("character_card: 非致命异常(已静默吞掉)", exc_info=True)
    if facts == 0:
        facts_md = hanako_dir / "agents" / agent_id / "memory" / "facts.md"
        try:
            if facts_md.exists():
                facts = sum(
                    1 for ln in facts_md.read_text(encoding="utf-8").splitlines()
                    if ln.strip() and not ln.strip().startswith("#")
                )
        except OSError:
            facts = 0
    if facts == 0:
        try:
            from ui.memory_panel import read_facts
            facts = len(read_facts(agent_id, d))
        except Exception:
            facts = 0

    # 反思：<agent_id>_reflections.json（P1-3 未来格式）条目数
    reflections = 0
    rpath = d / f"{agent_id}_reflections.json"
    try:
        if rpath.exists():
            rdata = json.loads(rpath.read_text(encoding="utf-8"))
            if isinstance(rdata, list):
                reflections = len(rdata)
            elif isinstance(rdata, dict):
                reflections = len(rdata.get("reflections", []) or [])
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        reflections = 0

    return {
        "events": events,
        "scenes": scenes,
        "facts": facts,
        "reflections": reflections,
    }


# ── 绘制工具 ─────────────────────────────────────────────

def _rounded_pixmap(pixmap: QPixmap, w: int, h: int) -> QPixmap:
    """把图片按中心裁切成圆形头像（w×h）。"""
    scaled = pixmap.scaled(
        w, h, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation,
    )
    x = (scaled.width() - w) // 2
    y = (scaled.height() - h) // 2
    scaled = scaled.copy(x, y, w, h)
    out = QPixmap(w, h)
    out.fill(Qt.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.Antialiasing)
    path = QPainterPath()
    path.addEllipse(0, 0, w, h)
    p.setClipPath(path)
    p.drawPixmap(0, 0, scaled)
    p.end()
    return out


def _clear_layout(layout) -> None:
    """递归清空布局内所有子项（widget / 子布局）。"""
    while layout.count():
        item = layout.takeAt(0)
        w = item.widget()
        if w is not None:
            w.deleteLater()
        elif item.layout() is not None:
            _clear_layout(item.layout())


# ── 面板 ─────────────────────────────────────────────────

class CharacterCard(PanelWidget):
    """角色卡展示面板：头像 / 名字 / 简介 / 性格标签 / 记忆统计。
    
    继承 PanelWidget，统一标题栏、刷新按钮、关闭按钮。
    
    - ``set_agent(agent_id, character_id=None)`` 切换数据源并刷新
    - ``reload()`` 重新读取档案与记忆统计
    - ``set_theme("light"|"dark")`` 切换主题
    """

    def __init__(self, agent_id: str = "default", character_id: str | None = None,
                 memory_dir: str | Path | None = None,
                 characters_dir: str | Path | None = None,
                 hanako_home: str | Path | None = None,
                 theme: str = "light", parent: QWidget | None = None):
        super().__init__("角色卡", parent, show_refresh=True, show_close=True,
                        min_size=(360, 460), max_size=(600, 800))
        self._agent_id = (agent_id or "default").strip()
        self._character_id = (character_id or agent_id or "default").strip()
        self._memory_dir = Path(memory_dir) if memory_dir else DEFAULT_MEMORY_DIR
        self._characters_dir = Path(characters_dir) if characters_dir else DEFAULT_CHARACTERS_DIR
        self._hanako_home = Path(hanako_home) if hanako_home else DEFAULT_HANAKO_HOME
        self._theme = theme
        self._profile: dict = {}
        self._stats: dict = {"events": 0, "scenes": 0, "facts": 0, "reflections": 0}
        self._stat_boxes: dict[str, tuple[QLabel, QLabel]] = {}
        
        # 填充内容区域
        self._build_ui()
        
        # 刷新按钮连接
        self.refresh_requested.connect(self.reload)
        
        # 加载数据
        self.reload()

    # ── UI 构建 ──

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = QFrame(self)
        header.setObjectName("cardHeader")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(14, 8, 14, 8)
        header_layout.setSpacing(8)
        self._title_label = QLabel(self._title_text(), header)
        self._title_label.setObjectName("cardTitle")
        self._refresh_button = QPushButton("刷新", header)
        self._refresh_button.setObjectName("headerButton")
        self._refresh_button.setCursor(Qt.PointingHandCursor)
        self._refresh_button.clicked.connect(self.reload)
        header_layout.addWidget(self._title_label)
        header_layout.addStretch(1)
        header_layout.addWidget(self._refresh_button)
        root.addWidget(header)

        self._scroll = QScrollArea(self)
        self._scroll.setObjectName("cardScroll")
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        container = QWidget(self._scroll)
        container.setObjectName("cardContent")
        self._content_layout = QVBoxLayout(container)
        self._content_layout.setContentsMargins(16, 16, 16, 16)
        self._content_layout.setSpacing(10)
        self._scroll.setWidget(container)
        root.addWidget(self._scroll, stretch=1)

        # 头像 + 名字行
        top = QHBoxLayout()
        top.setSpacing(12)
        self._avatar_label = QLabel(container)
        self._avatar_label.setObjectName("cardAvatar")
        self._avatar_label.setFixedSize(64, 64)
        self._avatar_label.setAlignment(Qt.AlignCenter)
        top.addWidget(self._avatar_label)
        name_col = QVBoxLayout()
        name_col.setSpacing(2)
        self._name_label = QLabel(container)
        self._name_label.setObjectName("cardName")
        name_col.addWidget(self._name_label)
        self._sub_label = QLabel(container)
        self._sub_label.setObjectName("cardSub")
        name_col.addWidget(self._sub_label)
        name_col.addStretch(1)
        top.addLayout(name_col, stretch=1)
        self._content_layout.addLayout(top)

        # 身份简介
        self._intro_label = QLabel(container)
        self._intro_label.setObjectName("cardIntro")
        self._intro_label.setWordWrap(True)
        self._intro_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._content_layout.addWidget(self._intro_label)

        # 性格标签
        self._tags_layout = QVBoxLayout()
        self._tags_layout.setSpacing(4)
        self._content_layout.addLayout(self._tags_layout)

        # 记忆统计四格
        stats_row = QHBoxLayout()
        stats_row.setSpacing(8)
        for key, caption, kind in STAT_DEFS:
            box = QFrame(container)
            box.setObjectName("cardStatBox")
            box.setProperty("stat-kind", kind)
            box_layout = QVBoxLayout(box)
            box_layout.setContentsMargins(8, 8, 8, 8)
            box_layout.setSpacing(0)
            num = QLabel("0", box)
            num.setObjectName("cardStatNum")
            num.setAlignment(Qt.AlignCenter)
            cap = QLabel(caption, box)
            cap.setObjectName("cardStatLabel")
            cap.setAlignment(Qt.AlignCenter)
            box_layout.addWidget(num)
            box_layout.addWidget(cap)
            stats_row.addWidget(box, stretch=1)
            self._stat_boxes[key] = (num, cap)
        self._content_layout.addLayout(stats_row)

        # 空占位（角色档案缺失时显示）
        self._empty_label = QLabel(
            "还没有角色档案…在 characters/ 或 ~/.hanako/agents/ 放上 "
            "identity.md / description.md / pet.json 吧", container,
        )
        self._empty_label.setObjectName("cardEmpty")
        self._empty_label.setAlignment(Qt.AlignCenter)
        self._empty_label.setWordWrap(True)
        self._empty_label.hide()
        self._content_layout.addWidget(self._empty_label)
        self._content_layout.addStretch(1)

    def _title_text(self) -> str:
        return f"角色卡 · {self._character_id}"

    def _apply_qss(self) -> None:
        """应用 neko.qss 到面板子树（只影响本面板，不污染全局）。"""
        try:
            from ui.chat_panel import NEKO_QSS_PATH
            if NEKO_QSS_PATH.exists():
                self.setStyleSheet(NEKO_QSS_PATH.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("角色卡样式应用失败: %s", exc)

    def _connect_theme_manager(self) -> None:
        """订阅全局 ThemeManager（若已初始化；否则保持构造时主题）。"""
        try:
            from ui.theme import get_default
            mgr = get_default()
            if mgr is not None:
                self.set_theme(mgr.current)
                mgr.theme_changed.connect(self.set_theme)
        except Exception as exc:
            logger.debug("ThemeManager 未初始化，角色卡保持默认主题: %s", exc)

    # ── 数据加载 ──

    def set_agent(self, agent_id: str, character_id: str | None = None) -> None:
        """切换数据源：agent（记忆统计）+ 角色（档案）并刷新。"""
        self._agent_id = (agent_id or "default").strip()
        if character_id:
            self._character_id = character_id.strip()
        else:
            self._character_id = self._agent_id
        self._title_label.setText(self._title_text())
        self.reload()

    def reload(self) -> None:
        """重新读取角色档案与记忆统计并渲染（主线程调用）。"""
        self._profile = read_character_profile(
            self._character_id, self._characters_dir, self._hanako_home,
        )
        self._stats = read_memory_stats(
            self._agent_id, self._memory_dir, self._hanako_home,
        )
        self._render()

    # ── 渲染 ──

    def _render(self) -> None:
        prof = self._profile
        self._name_label.setText(prof.get("name") or self._character_id)

        has_profile = bool(prof.get("intro") or prof.get("personality"))
        if not has_profile:
            self._intro_label.hide()
            self._clear_tags()
            self._empty_label.show()
            self._render_avatar("")
        else:
            self._empty_label.hide()
            self._intro_label.setText(
                prof.get("intro") or "这位角色还没有写简介～",
            )
            self._intro_label.show()
            self._render_tags(prof.get("personality") or [])
            self._render_avatar(prof.get("avatar_path") or "")

        source = prof.get("source", "none")
        source_label = {
            "builtin": "内置角色",
            "hanako": "Hanako 角色",
            "none": "无档案文件",
        }.get(source, source)
        self._sub_label.setText(f"{self._character_id} · {source_label}")

        for key, (num, _cap) in self._stat_boxes.items():
            num.setText(str(int(self._stats.get(key, 0) or 0)))

    def _render_avatar(self, avatar_path: str) -> None:
        """头像：优先图片（圆形裁切）；否则首字渐变圆（QSS 上色）。"""
        if avatar_path:
            pix = QPixmap(avatar_path)
            if not pix.isNull():
                self._avatar_label.setPixmap(_rounded_pixmap(pix, 64, 64))
                self._set_avatar_image_prop(True)
                return
        text = (self._profile.get("name") or self._character_id or "?").strip()[:1]
        self._avatar_label.setText(text or "?")
        self._avatar_label.setPixmap(QPixmap())
        self._set_avatar_image_prop(False)

    def _set_avatar_image_prop(self, is_image: bool) -> None:
        self._avatar_label.setProperty("avatar-image", "true" if is_image else "false")
        style = self._avatar_label.style()
        style.unpolish(self._avatar_label)
        style.polish(self._avatar_label)
        self._avatar_label.update()

    def _clear_tags(self) -> None:
        _clear_layout(self._tags_layout)

    def _render_tags(self, tags: list[str]) -> None:
        """渲染性格标签 chips，每行 TAGS_PER_ROW 个，超出自动换行。"""
        self._clear_tags()
        if not tags:
            return
        row: QHBoxLayout | None = None
        count = 0
        for tag in tags:
            if row is None or count >= TAGS_PER_ROW:
                row = QHBoxLayout()
                row.setSpacing(6)
                self._tags_layout.addLayout(row)
                count = 0
            chip = QLabel(f"#{tag}", self)
            chip.setObjectName("cardTag")
            row.addWidget(chip)
            count += 1
        self._tags_layout.addStretch(1)

    # ── 主题 ──

    def set_theme(self, theme: str) -> None:
        """切换主题（重新应用 QSS，刷新后代选择器）。"""
        if theme not in ("light", "dark"):
            return
        if theme == self._theme:
            return
        self._theme = theme
        self.setProperty("data-theme", theme)
        self._apply_qss()
        self.update()

    @property
    def theme(self) -> str:
        return self._theme

    @property
    def profile(self) -> dict:
        return dict(self._profile)

    @property
    def stats(self) -> dict:
        return dict(self._stats)
