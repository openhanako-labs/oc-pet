"""动作指令联动 — 关键词检测 → 右键菜单动态高亮 + 特殊事件写入。

用法:
    linker = ActionLinker(character_id="yuexinmiao")

    # 在收到 LLM 回复时检测关键词
    linker.check(reply_text)

    # 获取当前高亮的动作
    highlighted = linker.highlighted_actions

    # 用户点击动作后写入特殊消息到 outbox
    linker.trigger_action(outbox_dir, action_id)
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


# ── 动作定义 ──────────────────────────────────────────────

@dataclass
class Action:
    """单个动作定义"""
    id: str              # 唯一标识
    label: str           # 右键菜单显示名
    emoji: str           # 菜单图标
    keywords: list[str]  # 触发关键词


# 默认动作列表
DEFAULT_ACTIONS: list[Action] = [
    Action("pet", "摸头", "🤚", ["摸摸", "摸头", "揉揉", "顺毛", "摸摸头"]),
    Action("highfive", "击掌", "✋", ["击掌", "give me five", "来个击掌"]),
    Action("tea", "一起喝茶", "🍵", ["喝茶", "咖啡", "喝一杯", "泡茶", "来杯"]),
    Action("badminton", "打羽毛球", "🏸", ["羽毛球", "运动", "打球", "比赛"]),
    Action("hug", "抱一个", "🤗", ["抱抱", "拥抱", "抱一个", "抱一下"]),
    Action("game", "打游戏", "🎮", ["游戏", "开黑", "打游戏", "联机", "组队"]),
    Action("watch", "一起看", "🎬", ["看剧", "追番", "看电影", "一起看", "刷视频"]),
    Action("walk", "出去走走", "🚶", ["散步", "走走", "出门", "透透气", "下楼"]),
]


# ── ActionLinker ──────────────────────────────────────────

@dataclass
class ActionLinker:
    """检测 LLM 回复中的关键词，动态高亮右键菜单动作项。

    每个动作项有高亮窗口（秒）：检测到关键词后亮 N 秒，超时自动灭。
    """

    character_id: str
    highlight_duration: float = 30.0  # 高亮持续时间（秒）
    enabled: bool = True  # 总开关

    actions: list[Action] = field(default_factory=lambda: list(DEFAULT_ACTIONS))
    _highlighted: dict[str, float] = field(default_factory=dict)  # action_id → 高亮过期时间戳

    # ── 公开接口 ──

    @property
    def highlighted_actions(self) -> set[str]:
        """当前高亮的动作 ID 集合（已过滤过期的）"""
        now = time.time()
        active = set()
        expired = []
        for aid, expires in self._highlighted.items():
            if expires > now:
                active.add(aid)
            else:
                expired.append(aid)
        # 清理过期的
        for aid in expired:
            del self._highlighted[aid]
        return active

    def check(self, reply_text: str) -> list[str]:
        """检测回复文本，匹配关键词，高亮对应动作。

        Args:
            reply_text: LLM 回复文本

        Returns:
            本次新激活的动作 ID 列表
        """
        if not self.enabled or not reply_text:
            return []

        activated: list[str] = []
        now = time.time()
        expires = now + self.highlight_duration

        for action in self.actions:
            if action.id in self._highlighted and self._highlighted[action.id] > now:
                continue  # 已在亮，跳过
            for kw in action.keywords:
                if kw.lower() in reply_text.lower():
                    self._highlighted[action.id] = expires
                    activated.append(action.id)
                    logger.debug("Action triggered: %s ← '%s'", action.id, kw)
                    break  # 一个动作最多匹配一次

        return activated

    def get_action(self, action_id: str) -> Action | None:
        """根据 ID 获取动作定义"""
        for a in self.actions:
            if a.id == action_id:
                return a
        return None

    def trigger_action(self, outbox_dir: Path, action_id: str) -> dict | None:
        """用户点击动作项 → 写入特殊消息到 outbox，供 Agent 处理。

        Args:
            outbox_dir: 桌宠 outbox 目录 (~/.hanako/plugins/hanako-desktop-companion/)
            action_id: 被点击的动作 ID

        Returns:
            写入的消息字典，如果写入失败返回 None
        """
        action = self.get_action(action_id)
        if not action:
            logger.warning("Unknown action: %s", action_id)
            return None

        msg = {
            "type": "action",
            "action": action.id,
            "label": action.label,
            "emoji": action.emoji,
            "character": self.character_id,
            "time": time.time(),
        }

        try:
            outbox_dir.mkdir(parents=True, exist_ok=True)
            outbox_file = outbox_dir / "outbox.json"
            msgs = json.loads(outbox_file.read_text("utf-8")) if outbox_file.exists() else []
            msgs.append(msg)
            outbox_file.write_text(json.dumps(msgs, ensure_ascii=False), "utf-8")

            # 写待处理标记
            (outbox_dir / ".pending").write_text("1", "utf-8")
            logger.info("Action triggered: %s", action.label)
            return msg
        except Exception as e:
            logger.warning("Failed to write action message: %s", e)
            return None

    def clear(self):
        """清除所有高亮"""
        self._highlighted.clear()