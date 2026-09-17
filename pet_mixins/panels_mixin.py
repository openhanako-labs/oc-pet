"""PanelsMixin — 独立面板窗口（聊天面板 / 记忆面板 / 角色卡）+ 多宠打招呼。

由 PetWindow 多重继承（pet.py 类定义中加入）。方法体内访问
``self._agent_id`` / ``self._current_char`` / ``self._manage_menu`` /
``self._pet_manager`` 等均由 PetWindow 提供（鸭子类型）。

## 这一组是干什么的

桌宠的**附属窗口**——它们不是桌宠本体，是挂在桌宠上的工具面板：

| 面板 | 用途 | 入口 |
|---|---|---|
| ChatPanel | 完整聊天记录 + 输入 | 右键菜单「💬 聊天面板」 |
| MemoryPanel | 记忆浏览 | 右键菜单「🧠 记忆」 |
| CharacterCard | 角色卡（性格/介绍） | 右键菜单「🪪 角色卡」 |

以及多宠社交（P3）：另一只桌宠上线 → 打招呼。

## 为什么单独一个 mixin

- 这三类面板共享同一套创建模式（Qt.Tool 窗口 + resize + 失败置 None）
- 它们都是**可选的**——创建失败只记日志，桌宠本体照常工作
- 原本散在 `pet.py` 的对话/动画接线之间

## 依赖约束

`_init_neko_panels` 依赖 `self._manage_menu`（右键菜单已构建），
因此必须在 `_setup_menu()` 之后调用——顺序由 `__init__` 的 `_init_*`
调用序列保证（`_init_neko_t05` 排在 `_init_visual_startup` 之后）。

搬家自 pet.py（2026-09-17，技术债①）。行为零变化。
"""
from __future__ import annotations

import logging

from PySide6.QtCore import Qt

logger = logging.getLogger(__name__)


class PanelsMixin:
    """附属面板窗口（聊天 / 记忆 / 角色卡）+ 多宠打招呼。"""

    def _init_neko_panels(self):
        """创建 ChatPanel / MemoryPanel / CharacterCard 窗口并接线（P0-6/P0-7/P0-8/P1-7）。"""
        theme = getattr(self, "_ui_theme", "dark") or "dark"
        agent_name = ""
        try:
            from config import CHARACTER_INFO
            agent_name = (CHARACTER_INFO.get(self._current_char, {}) or {}).get("name", "")
        except Exception:
            agent_name = ""
        self._agent_display_name = agent_name or self._current_char

        # ── ChatPanel（P0-6/P0-7）──
        try:
            from ui.chat_panel import ChatPanel
            self._chat_panel = ChatPanel(
                theme=theme if theme in ("light", "dark") else "dark",
                agent_name=self._agent_display_name,
                parent=None,
            )
            self._chat_panel.setWindowFlags(self._chat_panel.windowFlags() | Qt.Tool)
            self._chat_panel.resize(380, 520)
            self._chat_panel.message_submitted.connect(self._on_chat_panel_submit)
            self._chat_panel.close_requested.connect(self._close_chat_panel)
            logger.info("T05 chat panel ready")
        except Exception as e:
            logger.warning("T05 chat panel 初始化失败: %s", e)
            self._chat_panel = None

        # ── MemoryPanel（P0-8）──
        try:
            from ui.memory_panel import MemoryPanel
            self._memory_panel = MemoryPanel(
                agent_id=self._agent_id,
                theme=theme if theme in ("light", "dark") else "dark",
                parent=None,
            )
            self._memory_panel.setWindowFlags(self._memory_panel.windowFlags() | Qt.Tool)
            self._memory_panel.resize(400, 520)
            logger.info("T05 memory panel ready")
        except Exception as e:
            logger.warning("T05 memory panel 初始化失败: %s", e)
            self._memory_panel = None

        # ── CharacterCard（P1-7 角色卡）──
        try:
            from ui.character_card import CharacterCard
            self._character_card = CharacterCard(
                agent_id=self._agent_id,
                character_id=self._current_char,
                theme=theme if theme in ("light", "dark") else "dark",
                parent=None,
            )
            self._character_card.setWindowFlags(
                self._character_card.windowFlags() | Qt.Tool,
            )
            self._character_card.resize(360, 460)
            logger.info("P1-7 character card ready")
        except Exception as e:
            logger.warning("P1-7 character card 初始化失败: %s", e)
            self._character_card = None

        # 右键菜单「管理」组入口（活动流旁）
        try:
            if hasattr(self, "_manage_menu") and self._manage_menu is not None:
                if self._chat_panel is not None:
                    self._manage_menu.addAction("💬 聊天面板", self._toggle_chat_panel)
                if self._memory_panel is not None:
                    self._manage_menu.addAction("🧠 记忆", self._toggle_memory_panel)
                if self._character_card is not None:
                    self._manage_menu.addAction("🪪 角色卡", self._toggle_character_card)
        except Exception as e:
            logger.debug("T05 菜单入口注入失败: %s", e)

    # ── P3：多宠打招呼 ─────────────────────────────────────

    def _init_multi_pet_greeting(self):
        """订阅 MultiPetBridge 的 pet_enter 事件：另一只桌宠上线 → 打招呼。

        用户点名的 P3 需求："两只看不见彼此但会互相打招呼"。
        注册顺序注意：bridge.register_pet 广播 pet_enter 时，先注册的宠会收到
        后注册宠的 enter；本窗口自己 enter 时不响应（source 是自己）。
        """
        try:
            mgr = getattr(self, "_pet_manager", None)
            if mgr is None:
                return
            bridge = getattr(mgr, "bridge", None)
            if bridge is None or not hasattr(bridge, "subscribe"):
                return
            bridge.subscribe(
                "pet_enter",
                self._on_other_pet_enter,
                agent_id=self._agent_id,
            )
            logger.info("P3 多宠打招呼已订阅 (agent=%s)", self._agent_id)
        except Exception as e:
            logger.warning("P3 多宠打招呼订阅失败（非致命）: %s", e)
