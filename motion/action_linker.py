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
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# 串行化 outbox.json 读-改-写（进程内线程锁 + 原子写；B2-8）
_OUTBOX_LOCK = threading.Lock()


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


# 默认即时反应规则（当 pet.json 没有 immediate_reactions 字段时使用）
DEFAULT_IMMEDIATE_REACTIONS: list[dict] = [
    {"keywords": ["克里斯蒂娜", "kurisu", "红莉栖"], "action": "touch", "emotion": "cute", "intensity": 0.7},
    {"keywords": ["喜欢", "爱你", "想你"], "action": "happy", "emotion": "happy", "intensity": 0.8},
    {"keywords": ["讨厌", "烦死", "讨厌你"], "action": "angry", "emotion": "angry", "intensity": 0.6},
    {"keywords": ["晚安", "睡觉", "困了"], "action": "sleep", "emotion": "sad", "intensity": 0.5},
    {"keywords": ["摸摸", "摸头", "揉揉", "顺毛"], "action": "touch", "emotion": "cute", "intensity": 0.8},
    {"keywords": ["击掌", "give me five"], "action": "waving", "emotion": "happy", "intensity": 0.9},
    {"keywords": ["抱抱", "拥抱", "抱一个"], "action": "happy", "emotion": "happy", "intensity": 0.8},
    {"keywords": ["喝茶", "咖啡", "喝一杯"], "action": "thinking", "emotion": "thinking", "intensity": 0.6},
    {"keywords": ["游戏", "开黑", "打游戏"], "action": "happy", "emotion": "happy", "intensity": 0.7},
    {"keywords": ["散步", "走走", "出门"], "action": "walk", "emotion": "happy", "intensity": 0.6},
    {"keywords": ["难过", "伤心", "哭"], "action": "sad", "emotion": "sad", "intensity": 0.5},
    {"keywords": ["生气", "愤怒", "气死"], "action": "angry", "emotion": "angry", "intensity": 0.7},
    {"keywords": ["开心", "快乐", "高兴"], "action": "happy", "emotion": "happy", "intensity": 0.8},
    {"keywords": ["工作", "学习", "写代码"], "action": "thinking", "emotion": "thinking", "intensity": 0.5},
    {"keywords": ["累", "疲惫", "困"], "action": "sleep", "emotion": "sad", "intensity": 0.4},
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
    _last_reaction_time: dict[str, float] = field(default_factory=dict)  # action_id → 最后触发时间戳（冷却用）
    _immediate_reactions: list[dict] | None = field(default=None, repr=False)  # 缓存即时反应规则

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

    def check_user_input(self, user_text: str) -> list[dict]:
        """分析用户输入，返回即时反应列表。

        与 check() 不同：
        - check() 高亮菜单项（等用户点击）
        - check_user_input() 返回即时反应（桌宠立刻执行）

        规则从 pet.json 的 immediate_reactions 字段读取（可配置）。
        内置冷却机制：同一动作 2 秒内不重复触发。

        Args:
            user_text: 用户输入的文本

        Returns:
            即时反应列表，每个反应包含：
            - action: 动作 ID
            - emotion: 情绪
            - intensity: 强度 (0.5-1.0)
        """
        if not self.enabled or not user_text:
            return []

        reactions: list[dict] = []
        text_lower = user_text.lower()

        # 从 pet.json 加载即时反应规则（可配置，缓存）
        if not hasattr(self, '_immediate_reactions') or self._immediate_reactions is None:
            self._immediate_reactions = self._load_immediate_reactions()

        for rule in self._immediate_reactions:
            keywords = rule.get("keywords", [])
            action_id = rule.get("action", "touch")
            emotion = rule.get("emotion", "happy")
            intensity = rule.get("intensity", 0.7)
            
            # 检查是否有任何关键词匹配
            if any(kw.lower() in text_lower for kw in keywords):
                # P2: 冷却检查 — 同一动作 2 秒内不重复触发
                if self._is_in_cooldown(action_id):
                    logger.debug("即时反应冷却中: %s (跳过)", action_id)
                    continue
                
                reactions.append({
                    "action": action_id,
                    "emotion": emotion,
                    "intensity": intensity,
                })
                # 记录触发时间
                self._last_reaction_time[action_id] = time.time()
                logger.debug("即时反应: %s ← '%s' (emotion=%s)", 
                           action_id, user_text[:20], emotion)
                break  # 只取第一个匹配的反应

        return reactions

    def _is_in_cooldown(self, action_id: str, cooldown: float = 2.0) -> bool:
        """P2: 检查动作是否在冷却期内（防频繁触发）"""
        last_time = self._last_reaction_time.get(action_id, 0)
        return (time.time() - last_time) < cooldown

    def _load_immediate_reactions(self) -> list[dict]:
        """从 pet.json 加载即时反应规则（可配置）。

        如果 pet.json 没有 immediate_reactions 字段，使用默认规则。
        """
        try:
            # 查找 pet.json
            char_dir = Path(__file__).parent.parent / "characters" / self.character_id
            pet_json = char_dir / "pet.json"
            if pet_json.exists():
                data = json.loads(pet_json.read_text("utf-8"))
                reactions = data.get("immediate_reactions", [])
                if reactions:
                    logger.info("加载即时反应规则: %d 条 (from pet.json)", len(reactions))
                    return reactions
        except Exception as e:
            logger.debug("加载即时反应规则失败: %s", e)
        
        # 默认规则（向后兼容）
        return DEFAULT_IMMEDIATE_REACTIONS

    def get_action(self, action_id: str) -> Action | None:
        """根据 ID 获取动作定义"""
        for a in self.actions:
            if a.id == action_id:
                return a
        return None

    @staticmethod
    def _locked_append(outbox_file: Path, msg: dict) -> None:
        """串行化读-改-写 outbox.json + 原子写（B2-8）。

        原实现直接 read_text -> append -> write_text，多宠/双击并发时
        读-改-写不原子，会丢事件或写坏文件。这里：
        1) 进程内线程锁串行化（双击/同进程多线程）；
        2) tempfile + os.replace 原子写（跨进程不产生半截文件）。
        """
        with _OUTBOX_LOCK:
            msgs = []
            if outbox_file.exists():
                try:
                    loaded = json.loads(outbox_file.read_text("utf-8"))
                    if isinstance(loaded, list):
                        msgs = loaded
                except Exception:
                    msgs = []  # 文件损坏则丢弃旧内容，不阻塞写入
            msgs.append(msg)
            tmp_fd, tmp_path = tempfile.mkstemp(
                dir=str(outbox_file.parent), suffix=".outbox.tmp"
            )
            try:
                with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                    json.dump(msgs, f, ensure_ascii=False)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, outbox_file)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    logger.debug("action_linker: 非致命异常(已静默吞掉)", exc_info=True)
                raise

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
            self._locked_append(outbox_file, msg)

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
