"""多桌宠管理器 — 扫描 Hanako agents，管理多个 PetWindow 实例

每个 agent 可以独立启用/禁用桌宠，各自有独立窗口、对话引擎、配置。

精灵来源优先级：
  1. ~/.hanako/agents/<agent>/pet/  (用户自定义)
  2. <project>/characters/<agent>/  (项目内置)
  3. 默认占位符（首字母圆圈）

用法:
    manager = PetManager()
    manager.launch_all()  # 启动所有 enabled 的桌宠
    manager.add_agent("glados")  # 新增一个
    manager.remove_agent("rebecca")  # 移除一个
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

HANAKO_HOME = Path.home() / ".hanako"
AGENTS_DIR = HANAKO_HOME / "agents"
PROJECT_DIR = Path(__file__).parent
CHARACTERS_DIR = PROJECT_DIR / "characters"


class PetManager:
    """多桌宠管理器"""

    def __init__(self):
        self._windows: dict[str, object] = {}  # agent_id -> PetWindow
        self._config = self._load_config()
        # ── M4: MultiPetBridge ──
        self._bridge = None
        self._bridge_enabled = True  # 可通过配置开关
        # ── Hanako WS 客户端（共享） ──
        self._ws_client = None
        self._session_manager = None
        self._init_hanako_ws()

    @property
    def agents(self) -> list[dict]:
        """返回所有配置的 agent 列表"""
        return self._config.get("agents", [])

    @property
    def bridge(self):
        """M4: 暴露 bridge 引用（供外部访问）"""
        return self._bridge

    @property
    def bridge_enabled(self) -> bool:
        """M4: bridge 是否启用"""
        return self._bridge_enabled

    @bridge_enabled.setter
    def bridge_enabled(self, value: bool):
        self._bridge_enabled = value
        if not value and self._bridge:
            self._bridge.stop()
            self._bridge = None

    @property
    def enabled_agents(self) -> list[dict]:
        """返回所有启用的 agent"""
        return [a for a in self.agents if a.get("enabled", True)]

    @property
    def windows(self) -> dict:
        return self._windows

    # ── 配置 ──

    def _load_config(self) -> dict:
        """加载 config.json"""
        cfg_path = PROJECT_DIR / "config.json"
        if cfg_path.exists():
            try:
                return json.loads(cfg_path.read_text("utf-8"))
            except Exception as e:
                logger.warning("Failed to load config: %s", e)
        return {}

    def _save_config(self):
        """保存 config.json"""
        cfg_path = PROJECT_DIR / "config.json"
        try:
            cfg_path.write_text(json.dumps(self._config, indent=2, ensure_ascii=False), "utf-8")
        except Exception as e:
            logger.warning("Failed to save config: %s", e)

    # ── M4: Bridge 生命周期 ──

    def start_bridge(self):
        """启动 MultiPetBridge（后台事件调度线程）"""
        if self._bridge:
            logger.info("MultiPetBridge already running")
            return
        if not self._bridge_enabled:
            return
        try:
            from core.multi_pet_bridge import MultiPetBridge
            self._bridge = MultiPetBridge(pet_manager=self)
            self._bridge.start()
            logger.info("MultiPetBridge started")
        except Exception as e:
            logger.error("Failed to start MultiPetBridge: %s", e)

    def stop_bridge(self):
        """停止 MultiPetBridge"""
        if self._bridge:
            self._bridge.stop()
            self._bridge = None
            logger.info("MultiPetBridge stopped")

    def restart_bridge(self):
        """重启 bridge（先停后启）"""
        self.stop_bridge()
        self.start_bridge()

    # ── Agent 发现 ──

    def discover_agents(self) -> list[dict]:
        """扫描 ~/.hanako/agents/ 发现所有可用 agent

        Returns:
            [{"id": "yuexinmiao", "name": "月薪喵", "has_sprites": True, "enabled": True}, ...]
        """
        if not AGENTS_DIR.exists():
            return []

        discovered = []
        for agent_dir in sorted(AGENTS_DIR.iterdir()):
            if not agent_dir.is_dir():
                continue
            agent_id = agent_dir.name
            # 读取 agent 名称
            name = agent_id
            desc_file = agent_dir / "description.md"
            if desc_file.exists():
                try:
                    text = desc_file.read_text("utf-8").strip()
                    # 第一行通常是名字
                    for line in text.splitlines():
                        line = line.strip().lstrip("#").strip()
                        if line and len(line) < 30:
                            name = line
                            break
                except Exception:
                    pass

            has_sprites = self._has_sprites(agent_id)
            discovered.append({
                "id": agent_id,
                "name": name,
                "has_sprites": has_sprites,
            })

        return discovered

    def _has_sprites(self, agent_id: str) -> bool:
        """检查 agent 是否有精灵资源"""
        # 1. 检查 agent 自带的 pet/ 目录
        agent_pet = AGENTS_DIR / agent_id / "pet" / "frames"
        if agent_pet.exists() and any(agent_pet.iterdir()):
            return True
        # 2. 检查项目内置的 characters/ 目录
        char_dir = CHARACTERS_DIR / agent_id
        if char_dir.exists():
            frames = char_dir / "frames"
            pet_json = char_dir / "pet.json"
            if frames.exists() or pet_json.exists():
                return True
        return False

    def get_sprite_dir(self, agent_id: str) -> Optional[str]:
        """获取 agent 的精灵目录路径

        优先级：agent 自带 pet/ → 项目内置 characters/ → None
        """
        # 0. 检查是否是内置角色（直接从 characters/ 读取）
        for agent in self.agents:
            if agent["id"] == agent_id and agent.get("builtin"):
                char_dir = CHARACTERS_DIR / agent_id
                if char_dir.exists():
                    return str(char_dir)

        # 1. agent 自带
        agent_pet = AGENTS_DIR / agent_id / "pet"
        if agent_pet.exists():
            frames = agent_pet / "frames"
            pet_json = agent_pet / "pet.json"
            if frames.exists() or pet_json.exists():
                return str(agent_pet)

        # 2. 项目内置
        char_dir = CHARACTERS_DIR / agent_id
        if char_dir.exists():
            frames = char_dir / "frames"
            pet_json = char_dir / "pet.json"
            if frames.exists() or pet_json.exists():
                return str(char_dir)

        return None

    # ── Agent 管理 ──

    def add_agent(self, agent_id: str, position: dict = None) -> bool:
        """新增一个 agent 到桌宠列表"""
        # 检查是否已存在
        if any(a["id"] == agent_id for a in self.agents):
            logger.info("Agent %s already in list", agent_id)
            return False

        # 检查 agent 是否存在
        if not (AGENTS_DIR / agent_id).exists():
            logger.warning("Agent %s not found in %s", agent_id, AGENTS_DIR)
            return False

        new_agent = {
            "id": agent_id,
            "enabled": True,
            "position": position or {"x": -1, "y": -1},
            "scale": 1.0,
        }
        self._config.setdefault("agents", []).append(new_agent)
        self._save_config()
        logger.info("Added agent: %s", agent_id)
        return True

    def remove_agent(self, agent_id: str) -> bool:
        """从列表移除 agent（不删除 Hanako agent 本身）"""
        agents = self._config.get("agents", [])
        before = len(agents)
        self._config["agents"] = [a for a in agents if a["id"] != agent_id]
        if len(self._config["agents"]) < before:
            self._save_config()
            # 关闭对应窗口
            self.close_window(agent_id)
            logger.info("Removed agent: %s", agent_id)
            return True
        return False

    def set_enabled(self, agent_id: str, enabled: bool):
        """启用/禁用 agent 的桌宠"""
        for agent in self.agents:
            if agent["id"] == agent_id:
                agent["enabled"] = enabled
                self._save_config()
                if enabled:
                    self.launch_window(agent_id)
                else:
                    self.close_window(agent_id)
                return

    def _init_hanako_ws(self):
        """初始化共享 Hanako WS 客户端"""
        try:
            from env_config import get_hanako_config
            from core.hanako_ws_client import HanakoWSClient
            from core.hanako_session_manager import HanakoSessionManager

            cfg = get_hanako_config()
            if cfg["transport_mode"] == "direct":
                logger.info("Hanako transport mode=direct, skip WS client")
                return

            self._ws_client = HanakoWSClient(cfg["base_url"], cfg["api_token"])
            self._session_manager = HanakoSessionManager(
                self._ws_client, cfg["base_url"], cfg["api_token"],
                reply_timeout=cfg["reply_timeout"]
            )
            self._ws_client.start()
            logger.info("Hanako WS client started | mode=%s", cfg["transport_mode"])
        except Exception as e:
            logger.warning("Hanako WS init failed: %s (will fallback to direct LLM)", e)
            self._ws_client = None
            self._session_manager = None

    def _shutdown_hanako_ws(self):
        """关闭 Hanako WS 客户端"""
        if self._ws_client:
            try:
                self._ws_client.stop(timeout=3)
            except Exception:
                pass
            self._ws_client = None
            self._session_manager = None

    @property
    def ws_client(self):
        return self._ws_client

    @property
    def session_manager(self):
        return self._session_manager

    # ── 窗口管理 ──

    def launch_all(self):
        """启动所有 enabled 的桌宠窗口（自动启动 bridge）"""
        # 确保 bridge 已启动
        if self._bridge_enabled and not self._bridge:
            self.start_bridge()
        for agent in self.enabled_agents:
            self.launch_window(agent["id"])

    def launch_window(self, agent_id: str):
        """启动单个桌宠窗口"""
        if agent_id in self._windows:
            logger.info("Window already exists for %s", agent_id)
            return

        from pet import PetWindow

        # 查找精灵目录
        sprite_dir = self.get_sprite_dir(agent_id)

        # 获取 agent 配置
        agent_cfg = self._get_agent_cfg(agent_id)

        try:
            window = PetWindow(
                agent_id=agent_id,
                sprite_dir=sprite_dir,
                position=agent_cfg.get("position"),
                scale=agent_cfg.get("scale", 1.0),
                on_position_change=lambda x, y, aid=agent_id: self.update_agent_cfg(aid, position={"x": x, "y": y}),
                pet_manager=self,
            )
            window.show()
            self._windows[agent_id] = window
            logger.info("Launched pet window for %s (sprites: %s)",
                        agent_id, sprite_dir or "default")

            # ── 注入 Hanako WS 客户端 ──
            if self._ws_client and self._session_manager:
                try:
                    if hasattr(window, 'set_hanako_ws'):
                        window.set_hanako_ws(self._ws_client, self._session_manager)
                        logger.info("Injected Hanako WS into %s", agent_id)
                except Exception as e:
                    logger.warning("Failed to inject Hanako WS into %s: %s", agent_id, e)

            # ── M4: 注册桌宠到桥接器 ──
            if self._bridge and self._bridge_enabled:
                try:
                    self._bridge.register_pet(agent_id, window)
                    logger.info("Registered pet '%s' to MultiPetBridge", agent_id)
                except Exception as e:
                    logger.warning("Failed to register pet '%s' to bridge: %s", agent_id, e)
        except Exception as e:
            logger.error("Failed to launch pet for %s: %s", agent_id, e)

    def close_window(self, agent_id: str):
        """关闭单个桌宠窗口"""
        # ── M4: 先从桥接器注销 ──
        if self._bridge and self._bridge_enabled:
            try:
                self._bridge.unregister_pet(agent_id)
                logger.info("Unregistered pet '%s' from MultiPetBridge", agent_id)
            except Exception as e:
                logger.warning("Failed to unregister pet '%s' from bridge: %s", agent_id, e)

        window = self._windows.pop(agent_id, None)
        if window:
            try:
                window.close()
            except Exception:
                pass

    def close_all(self):
        """关闭所有桌宠窗口（并停止 bridge）"""
        for agent_id in list(self._windows.keys()):
            self.close_window(agent_id)
        # 全部关闭后停止 bridge
        if self._bridge_enabled:
            self.stop_bridge()

    def _get_agent_cfg(self, agent_id: str) -> dict:
        """从 config.json 获取 agent 的桌宠配置"""
        for agent in self.agents:
            if agent["id"] == agent_id:
                return agent
        return {}

    def update_agent_cfg(self, agent_id: str, **kwargs):
        """更新 agent 配置（position, scale 等）"""
        for agent in self.agents:
            if agent["id"] == agent_id:
                agent.update(kwargs)
                self._save_config()
                return
