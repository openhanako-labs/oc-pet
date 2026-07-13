"""Hanako 联动模块 - 读取 Hanako 状态，实现跨应用互动

功能：
1. Hanako 窗口检测 - 识别主窗口位置和大小
2. Hanako 状态读取 - 读取当前活跃 agent、对话状态
3. 跨应用互动 - 桌宠可以"看到" Hanako 的对话并参与

数据来源：
- ~/.hanako/user/preferences.json - 主 agent 配置
- ~/.hanako/user/window-state.json - 窗口位置
- ~/.hanako/agents/<agent>/memory/ - 对话记忆
- ~/.hanako/agents/<agent>/sessions/ - 对话记录
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

HANAKO_HOME = Path.home() / ".hanako"


# ════════════════════════════════════════════════════════════
#  数据结构
# ════════════════════════════════════════════════════════════

@dataclass
class HanakoWindowState:
    """Hanako 窗口状态"""
    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0
    is_maximized: bool = False
    
    @property
    def center_x(self) -> int:
        return self.x + self.width // 2
    
    @property
    def center_y(self) -> int:
        return self.y + self.height // 2
    
    @property
    def right(self) -> int:
        return self.x + self.width
    
    @property
    def bottom(self) -> int:
        return self.y + self.height


@dataclass
class HanakoState:
    """Hanako 完整状态"""
    primary_agent: str = ""
    active_agents: list[str] = field(default_factory=list)
    window: HanakoWindowState = field(default_factory=HanakoWindowState)
    last_conversation: Optional[str] = None
    last_conversation_time: Optional[float] = None


# ════════════════════════════════════════════════════════════
#  Hanako 联动管理器
# ════════════════════════════════════════════════════════════

class HanakoBridge:
    """Hanako 联动管理器
    
    职责：
    1. 读取 Hanako 配置和状态
    2. 检测 Hanako 窗口位置
    3. 读取最近对话记录
    4. 提供跨应用互动接口
    """
    
    def __init__(self):
        self._state = HanakoState()
        self._last_check: float = 0
        self._check_interval: float = 5.0  # 5 秒检查一次
        
        # 初始化时读取一次
        self._load_state()
        
        logger.info("HanakoBridge initialized")
    
    @property
    def state(self) -> HanakoState:
        """获取当前状态"""
        return self._state
    
    @property
    def is_hanako_running(self) -> bool:
        """Hanako 是否正在运行（通过窗口状态判断）"""
        return self._state.window.width > 0 and self._state.window.height > 0
    
    def update(self):
        """更新状态（定期调用）"""
        now = time.time()
        if now - self._last_check < self._check_interval:
            return
        
        self._last_check = now
        self._load_state()
    
    def _load_state(self):
        """从文件加载 Hanako 状态"""
        try:
            # 读取主 agent 配置
            self._load_primary_agent()
            
            # 读取窗口状态
            self._load_window_state()
            
            # 读取最近对话
            self._load_recent_conversation()
            
        except Exception as e:
            logger.debug("Failed to load Hanako state: %s", e)
    
    def _load_primary_agent(self):
        """读取主 agent 配置"""
        prefs_path = HANAKO_HOME / "user" / "preferences.json"
        if not prefs_path.exists():
            return
        
        try:
            data = json.loads(prefs_path.read_text("utf-8"))
            self._state.primary_agent = data.get("primaryAgent", "")
            
            # 读取 agent 列表
            agents_dir = HANAKO_HOME / "agents"
            if agents_dir.exists():
                self._state.active_agents = [
                    d.name for d in agents_dir.iterdir() 
                    if d.is_dir() and not d.name.startswith(".")
                ]
        except Exception as e:
            logger.debug("Failed to load primary agent: %s", e)
    
    def _load_window_state(self):
        """读取窗口状态"""
        window_path = HANAKO_HOME / "user" / "window-state.json"
        if not window_path.exists():
            return
        
        try:
            data = json.loads(window_path.read_text("utf-8"))
            self._state.window = HanakoWindowState(
                x=data.get("x", 0),
                y=data.get("y", 0),
                width=data.get("width", 0),
                height=data.get("height", 0),
                is_maximized=data.get("isMaximized", False),
            )
        except Exception as e:
            logger.debug("Failed to load window state: %s", e)
    
    def _load_recent_conversation(self):
        """读取最近对话记录"""
        if not self._state.primary_agent:
            return
        
        sessions_dir = HANAKO_HOME / "agents" / self._state.primary_agent / "sessions"
        if not sessions_dir.exists():
            return
        
        try:
            # 找到最新的 session 文件
            session_files = sorted(
                sessions_dir.glob("*.jsonl"),
                key=lambda f: f.stat().st_mtime,
                reverse=True
            )
            
            if not session_files:
                return
            
            latest_session = session_files[0]
            
            # 读取最后几行
            lines = latest_session.read_text("utf-8").strip().split("\n")
            
            # 找到最后一条用户消息
            for line in reversed(lines):
                try:
                    entry = json.loads(line)
                    if entry.get("role") == "user":
                        content = entry.get("content", "")
                        if isinstance(content, list):
                            # 多模态消息，取文本部分
                            content = " ".join(
                                item.get("text", "") 
                                for item in content 
                                if isinstance(item, dict) and item.get("type") == "text"
                            )
                        if content:
                            self._state.last_conversation = content[:100]  # 只取前100字
                            self._state.last_conversation_time = entry.get("timestamp")
                            break
                except json.JSONDecodeError:
                    continue
                    
        except Exception as e:
            logger.debug("Failed to load recent conversation: %s", e)
    
    def get_window_position(self) -> tuple[int, int, int, int] | None:
        """获取 Hanako 窗口位置
        
        Returns:
            (x, y, width, height) 或 None
        """
        if not self.is_hanako_running:
            return None
        
        w = self._state.window
        return (w.x, w.y, w.width, w.height)
    
    def get_nearby_position(self, offset_x: int = 10, offset_y: int = 0) -> tuple[int, int] | None:
        """获取 Hanako 窗口旁边的推荐位置
        
        Args:
            offset_x: 水平偏移（正值向右）
            offset_y: 垂直偏移（正值向下）
        
        Returns:
            (x, y) 或 None
        """
        if not self.is_hanako_running:
            return None
        
        w = self._state.window
        return (w.right + offset_x, w.y + offset_y)
    
    def get_conversation_context(self) -> str:
        """获取对话上下文（供桌宠感知）"""
        parts = []
        
        if self._state.primary_agent:
            parts.append(f"当前活跃助手: {self._state.primary_agent}")
        
        if self._state.last_conversation:
            parts.append(f"最近对话: {self._state.last_conversation}")
        
        if self.is_hanako_running:
            parts.append("Hanako 正在运行")
        
        return " | ".join(parts) if parts else ""


# ════════════════════════════════════════════════════════════
#  便捷函数
# ════════════════════════════════════════════════════════════

_global_bridge: Optional[HanakoBridge] = None

def get_hanako_bridge() -> HanakoBridge:
    """获取全局 HanakoBridge 实例"""
    global _global_bridge
    if _global_bridge is None:
        _global_bridge = HanakoBridge()
    return _global_bridge
