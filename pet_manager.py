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
PROJECT_DIR = Path(__file__).parent.resolve()
CHARACTERS_DIR = PROJECT_DIR / "characters"


class PetManager:
    """多桌宠管理器"""

    def __init__(self):
        self._windows: dict[str, object] = {}  # agent_id -> PetWindow
        self._config = self._load_config()
        self._config_mtime = 0  # P2: 配置热重载 — 上次加载的文件修改时间
        try:
            cfg_path = PROJECT_DIR / "config.json"
            if cfg_path.exists():
                self._config_mtime = cfg_path.stat().st_mtime
        except Exception:
            logger.debug("pet_manager: 非致命异常(已静默吞掉)", exc_info=True)
        # ── launch 失败兜底：每个 agent 只记一次 ERROR，避免日志刷屏 ──
        self._launch_error_logged: set[str] = set()
        # ── 自己的托盘图标（仅在「一个窗口都没有」时用来弹提示）──
        self._own_tray = None
        self._own_tray_menu = None
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
    
    def _reload_config(self) -> bool:
        """P2: 热重载配置（检查文件变更，自动更新缓存）
        
        Returns:
            True 如果配置有变更并已重载，False 如果无变更
        """
        cfg_path = PROJECT_DIR / "config.json"
        if not cfg_path.exists():
            return False
        
        try:
            # 读取文件修改时间
            current_mtime = cfg_path.stat().st_mtime
            last_mtime = getattr(self, '_config_mtime', 0)
            
            if current_mtime <= last_mtime:
                return False  # 无变更
            
            # 重新加载
            new_config = json.loads(cfg_path.read_text("utf-8"))
            
            # 比较关键字段（agents）
            old_agents = self._config.get("agents", [])
            new_agents = new_config.get("agents", [])
            
            if old_agents != new_agents:
                logger.info("Config hot reload: agents changed %s -> %s",
                            [a.get('id') for a in old_agents],
                            [a.get('id') for a in new_agents])
                self._config = new_config
                self._config_mtime = current_mtime
                return True
            
            # 其他字段变更（如 character、scale 等）
            if old_agents == new_agents:
                # 即使 agents 没变，也更新其他字段
                self._config = new_config
                self._config_mtime = current_mtime
                logger.debug("Config hot reload: other fields updated")
                return True
            
        except Exception as e:
            logger.warning("Config reload failed: %s", e)
        
        return False
    
    def _validate_enabled_agents(self) -> list[str]:
        """P2: 找出「已启用但没有模型资源」的角色，本次运行跳过它们。

        Returns:
            被跳过的 agent_id 列表（空列表 = 无需跳过）。

        注意：本方法**只改内存视图**，调用方**不得**把它写回 config.json。
        理由见 launch_all 里的注释：模型暂时不可用不等于永久关闭。
        """
        skipped: list[str] = []
        for agent in self._config.get("agents", []):
            if not agent.get("enabled", True):
                continue  # 已禁用的跳过

            agent_id = agent.get("id")
            if not agent_id:
                continue

            # 检查是否有精灵资源
            if not self._has_sprites(agent_id):
                logger.warning("Agent %s is enabled but has no sprites, skipping this run", agent_id)
                agent["enabled"] = False   # 仅改内存：让 enabled_agents 本次不返回它
                skipped.append(agent_id)

        return skipped
    
    def _has_any_characters(self) -> bool:
        """P2: 检查是否有可用的角色模型（characters/ 目录非空）"""
        if not CHARACTERS_DIR.exists():
            return False
        # 检查是否有子目录（每个角色一个目录）
        for item in CHARACTERS_DIR.iterdir():
            if item.is_dir():
                return True
        return False

    def _save_config(self):
        """保存 config.json"""
        cfg_path = PROJECT_DIR / "config.json"
        try:
            # 以磁盘最新内容为基础，只覆盖 PetManager 真正管理的 agents 字段，
            # 其余（如 config.py 的 dialog.agent_id）保留磁盘新值，
            # 避免用启动时的旧 self._config 整体覆盖把 dialog 冲掉。
            merged = {}
            try:
                if cfg_path.exists():
                    merged = json.loads(cfg_path.read_text("utf-8"))
            except Exception:
                logger.debug("pet_manager: 非致命异常(已静默吞掉)", exc_info=True)
            # 只覆盖 PetManager 管的字段（agents 等），不碰其他系统字段
            if "agents" in self._config:
                merged["agents"] = self._config["agents"]
            cfg_path.write_text(json.dumps(merged, indent=2, ensure_ascii=False), "utf-8")
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
                    logger.debug("pet_manager: 非致命异常(已静默吞掉)", exc_info=True)

            has_sprites = self._has_sprites(agent_id)
            discovered.append({
                "id": agent_id,
                "name": name,
                "has_sprites": has_sprites,
            })

        return discovered

    @staticmethod
    def _dir_has_payload(path) -> bool:
        """目录存在**且有内容**（非空）。

        2026-09-11：原判断只看 `path.exists()`，于是空目录也当有资源。
        实测：`characters/sample_live2d/live2d/` 是个空目录，却通过了
        `_has_sprites` → 被当作有效角色启用 → 启动时无模型可加载。
        """
        try:
            return path.is_dir() and any(path.iterdir())
        except Exception:
            return False

    def _has_sprites(self, agent_id: str) -> bool:
        """检查 agent 是否有精灵资源（包括 Live2D 模型）"""
        # 1. 检查 agent 自带的 pet/ 目录
        agent_pet = AGENTS_DIR / agent_id / "pet"
        if agent_pet.exists():
            # 检查是否有 frames/ 或 live2d/（要求非空）
            if self._dir_has_payload(agent_pet / "frames"):
                return True
            if self._dir_has_payload(agent_pet / "live2d"):
                return True
        # 2. 检查项目内置的 characters/ 目录
        char_dir = CHARACTERS_DIR / agent_id
        if char_dir.exists():
            # 检查是否有 frames/ 或 live2d/（要求非空）
            if self._dir_has_payload(char_dir / "frames"):
                return True
            if self._dir_has_payload(char_dir / "live2d"):
                return True
            # 2026-09-08: 支持直接检查 char_dir 下的 .model3.json（live2d zip 导入）
            # 注意：f.suffix 只返回最后一个扩展名（.json），需要用 f.name.endswith 检查
            try:
                files = [f.name for f in char_dir.iterdir() if f.is_file()]
                logger.info("[has_sprites] %s files: %s", agent_id, files)
                has_model3 = any(f.name.endswith('.model3.json') or f.name.endswith('.model.json') for f in char_dir.iterdir() if f.is_file())
                logger.info("[has_sprites] %s has_model3=%s", agent_id, has_model3)
                if has_model3:
                    return True
            except Exception as e:
                logger.warning("[has_sprites] %s iterdir failed: %s", agent_id, e)
            # 检查是否有 pet.json（可能配置了外部资源）
            if (char_dir / "pet.json").exists():
                return True
        else:
            logger.warning("[has_sprites] %s char_dir not exists: %s", agent_id, char_dir)
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
                logger.debug("pet_manager: 非致命异常(已静默吞掉)", exc_info=True)
            self._ws_client = None
            self._session_manager = None

    @property
    def ws_client(self):
        return self._ws_client

    @property
    def session_manager(self):
        return self._session_manager

    # ── 用户可见提示（系统托盘气泡）──

    def _fallback_tray_icon(self):
        """自绘一个占位托盘图标（借不到角色首帧时的兜底）。"""
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap

        px = QPixmap(64, 64)
        px.fill(QColor(0, 0, 0, 0))
        p = QPainter(px)
        try:
            p.setRenderHint(QPainter.Antialiasing, True)
            p.setBrush(QColor(46, 52, 64))
            p.setPen(Qt.NoPen)
            p.drawRoundedRect(4, 4, 56, 56, 14, 14)
            p.setPen(QColor(232, 180, 90))
            f = p.font()
            f.setPointSize(30)
            f.setBold(True)
            p.setFont(f)
            p.drawText(px.rect(), Qt.AlignCenter, "!")
        finally:
            p.end()
        return QIcon(px)

    def tray_icon(self):
        """拿一个能弹气泡的托盘图标，没有则返回 None。

        优先借已有桌宠窗口的托盘（那是角色首帧，好看）；一个窗口都没有时
        自建一个 —— 而那**恰好就是需要提示的时候**。
        """
        try:
            from PySide6.QtWidgets import QApplication, QSystemTrayIcon
        except Exception:
            return None
        if QApplication.instance() is None:
            return None

        for w in self._windows.values():
            t = getattr(w, "_tray", None)
            if isinstance(t, QSystemTrayIcon):
                return t

        if self._own_tray is None:
            from PySide6.QtWidgets import QMenu

            tray = QSystemTrayIcon(self._fallback_tray_icon())
            tray.setToolTip("OC Desktop Pet")
            # 常驻图标若没有菜单，用户就只能等程序退出才看得到它消失。
            # 给一条「忽略提示」让用户能主动收起。
            menu = QMenu()
            act = menu.addAction("忽略提示")
            act.triggered.connect(tray.hide)
            tray.setContextMenu(menu)
            tray.show()
            # 持有引用：局部变量出作用域后 Qt 不保证菜单还活着
            self._own_tray_menu = menu
            self._own_tray = tray
        return self._own_tray

    def notify_user(self, title: str, body: str) -> None:
        """把「桌宠为什么没起来」弹成系统托盘气泡。

        2026-09-11 新增。为何必须走托盘：桌宠启动失败时**没有窗口**，
        而没有窗口就没有托盘 —— 用户看到的是「点了启动，什么都没发生」。
        （实测：为了查出「0 enabled agents」这件事，我让用户重启了三次桌宠。）

        只在整个都没起来时才提示；部分失败会写日志但不弹，
        否则一个长期配置了缺失模型的用户每次开机都会被弹一次。

        提示失败绝不能反过来把启动搞崩，所以整段包起来。
        """
        try:
            from PySide6.QtWidgets import QSystemTrayIcon

            tray = self.tray_icon()
            if tray is None:
                logger.info("notify_user: 无托盘可用，仅日志 — %s: %s", title, body)
                return
            if not tray.supportsMessages():
                logger.info("notify_user: 托盘不支持气泡 — %s: %s", title, body)
                return
            tray.showMessage(title, body, QSystemTrayIcon.Warning, 8000)
            # 记一条：出问题时「到底告诉用户了什么」应当可查，
            # 否则只能看到 launch_all 的早退日志，猜不出界面有没有提示。
            logger.info("notify_user: 已提示用户 — %s: %s", title, body)
        except Exception:
            logger.debug("notify_user: 弹气泡失败（忽略）", exc_info=True)

    # ── 窗口管理 ──

    def launch_all(self):
        """启动所有 enabled 的桌宠窗口（自动启动 bridge）"""
        # P2: 热重载配置（检查文件变更，自动更新缓存）
        self._reload_config()
        logger.info("launch_all: config reloaded, agents=%s", self._config.get('agents', []))
        
        # P2: 检查是否有可用的角色模型
        if not self._has_any_characters():
            logger.warning("No character models found in %s, skipping pet launch", CHARACTERS_DIR)
            logger.warning("Please install a character package before launching pets")
            # 没有窗口 → 没有托盘 → 只是写日志的话用户完全看不到。
            self.notify_user(
                "桌宠未启动",
                f"没有找到任何角色目录，桌宠无法显示。\n请把角色包放到：{CHARACTERS_DIR}",
            )
            return
        
        # P2: 验证启用的角色是否有精灵资源，本次不启动无资源的
        #
        # 2026-09-11 修：原先这里会 `self._save_config()` 把 enabled=false
        # **持久化回 config.json**。后果：模型暂时不可用（换盘、下载中、目录
        # 被移动）会被记成「以后都不用」，而且一旦所有 agent 都被写回禁用，
        # 桌宠就静默不启动（实测：shizuku 目录不存在→写回禁用；而 miku 已是
        # false，于是 0 enabled agents，界面什么都不显示）。
        #
        # 不变量：**运行时启发式不得修改用户的持久配置。**
        # 本次在内存里跳过即可，下次启动重新判定——代价是重新扫一遍目录，
        # 比“把暂时不可用写成永久关闭”便宜得多。
        skipped = self._validate_enabled_agents()
        if skipped:
            logger.warning(
                "launch_all: 本次跳过 %d 个无模型资源的 agent（%s）——"
                "仅影响本次运行，不写回配置；补上模型后重启即可恢复",
                len(skipped), ", ".join(skipped),
            )
        
        # 记录要启动的 agents
        enabled = self.enabled_agents
        logger.info("launch_all: %d enabled agents: %s", len(enabled), [a['id'] for a in enabled])
        if len(enabled) == 0:
            logger.warning("launch_all: 没有启用的 agent，桌面宠物不会显示！请检查 config.json 里 agents[].enabled")
            logger.warning("launch_all: 可能的原因：1) 所有 agent 都是 enabled=false  2) agent 没有 sprites/model 被自动禁用")
            # 这里是「点了启动什么都没发生」的正脸：一个窗口都没建起来，
            # 所以必须主动告诉用户，否则桌面上不会有任何变化。
            if skipped:
                self.notify_user(
                    "桌宠未启动",
                    "已启用的角色缺少模型文件，本次跳过：" + "、".join(skipped)
                    + "。\n补上模型后重启即可（配置没有被改写）。",
                )
            else:
                self.notify_user(
                    "桌宠未启动",
                    "没有已启用的角色。请在设置里启用一个角色，"
                    "或确认 characters/ 下已放入角色包。",
                )
        
        # 确保 bridge 已启动
        if self._bridge_enabled and not self._bridge:
            self.start_bridge()
        for agent in enabled:
            logger.info("launch_all: launching agent %s", agent['id'])
            self.launch_window(agent["id"])

    def launch_window(self, agent_id: str):
        """启动单个桌宠窗口"""
        if agent_id in self._windows:
            logger.info("Window already exists for %s", agent_id)
            return

        # 2026-09-11: import 与配置查询一并搬进 try。
        #
        # 原实现在这里就 `from pet import PetWindow`，而它在 try **外面** ——
        # pet.py 导入失败（PySide6 缺失、改文件后的语法错）会让异常直接穿过
        # launch_window → launch_all → main，于是：
        #   · 后面的 agent 一个都不会启动
        #   · 用户什么都看不到（launcher 看门狗只会不断重启）
        # 而下面的 except 本来是专门用来“把失败变成可见提示”的。
        try:
            from pet import PetWindow

            # 查找精灵目录
            sprite_dir = self.get_sprite_dir(agent_id)

            # 获取 agent 配置
            agent_cfg = self._get_agent_cfg(agent_id)
            # scale 优先级：顶层 config.scale（滚轮/设置面板写入的权威值）> agent 级 > 1.0
            # （agents 列表里首次自动生成的 scale=1.0 是初始占位，滚轮缩放只写顶层；
            #   若顶层缺失回退 agent 级，再回退 1.0）
            _agent_scale = agent_cfg.get("scale")
            _global_scale = None
            try:
                from config import load_config
                _global_scale = load_config().get("scale")
            except Exception:
                _global_scale = None
            _scale = _global_scale if _global_scale is not None else (_agent_scale if _agent_scale is not None else 1.0)
            # 下限 0.3（与 pet.py 滚轮缩放一致），否则保存的 0.3 会在重启时被拉回 0.5
            _scale = max(0.3, min(3.0, float(_scale)))

            window = PetWindow(
                agent_id=agent_id,
                sprite_dir=sprite_dir,
                position=agent_cfg.get("position"),
                scale=_scale,
                agent_config=agent_cfg,  # 含 tts / dialog 等 agent 级覆盖（per-pet 独立配置）
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
            # 渲染器/窗口创建失败（最常见：Live2D GPU 初始化崩溃、Whisper 预加载 OOM、
            # 角色 pet.json 解析失败等）。PetWindow.__init__ 中途抛出时，部分构造的对象
            # 会被 GC，连带 QTimer 一起释放，所以这里不存在「定时器泄漏」，只需：
            #   1) 一次性 ERROR 日志（同一 agent 不再刷屏）
            #   2) 把失败冒到 QSystemTrayIcon 气泡给用户一个可见提示
            if agent_id not in self._launch_error_logged:
                self._launch_error_logged.add(agent_id)
                # logger.exception 自动捕获当前 sys.exc_info() 堆栈
                logger.exception("Failed to launch pet for %s", agent_id)
                # 2026-09-11: 统一走 notify_user —— 它自建托盘，所以
                # 「第一个桌宠就起不来」（一个窗口都没有）时同样能提示。
                # 原实现遍历 self._windows 借托盘，那个场景下必然借不到。
                self.notify_user("桌宠启动失败", f"{agent_id}: {e}")
            # 标记失败 → 任何后续 retry / 自动恢复都跳过（避免定时器反复尝试）
            # 注：PetWindow 内的 QTimer 已随部分构造对象一起 GC，无需手动 stop。

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
                logger.debug("pet_manager: 非致命异常(已静默吞掉)", exc_info=True)

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
        """更新 agent 配置（position, scale 等）

        位置这类高频写入走异步防抖保存，避免阻塞 GUI 线程（拖拽卡顿根因）。
        """
        for agent in self.agents:
            if agent["id"] == agent_id:
                agent.update(kwargs)
                try:
                    from config import async_config_saver
                    async_config_saver.schedule(self._config)
                except Exception:
                    self._save_config()
                return

    # ── M5: 多宠总览接口 ──

    def overview_rows(self) -> list[dict]:
        """返回所有 enabled_agents 的概览行。

        已启动的从 window 读状态；未启动的补一行 running=False。
        不存在的 agent（agents 里有但 AGENTS_DIR 无目录）不追加。
        """
        rows: list[dict] = []
        launched: set[str] = set(self._windows.keys())

        for agent in self.enabled_agents:
            aid = agent.get("id")
            if not aid:
                continue

            if aid in launched:
                w = self._windows[aid]
                muted = getattr(w, "_muted", False)
                passthrough = getattr(getattr(w, "_mousePassthrough", None), "__bool__", lambda: False)()
                visible = w.isVisible()
                rows.append({
                    "agent_id": aid,
                    "name": aid,
                    "running": True,
                    "muted": bool(muted),
                    "passthrough": bool(passthrough),
                    "visible": bool(visible),
                })
            else:
                rows.append({
                    "agent_id": aid,
                    "name": aid,
                    "running": False,
                    "muted": False,
                    "passthrough": False,
                    "visible": False,
                })

        return rows

    def set_muted(self, agent_id: str, muted: bool) -> None:
        """设置指定 agent 的静音状态。"""
        window = self._windows.get(agent_id)
        if window is None:
            return
        window._muted = muted
        if hasattr(window, "set_muted"):
            try:
                window.set_muted(muted)
            except Exception as e:
                logger.warning("set_muted(%s) call failed: %s", agent_id, e)
        # 若存在 TTS player，直接 set_volume(0) 模拟静音（不影响已播放音频）
        tts = getattr(window, "_tts_player", None)
        if tts is not None:
            try:
                if muted:
                    tts.set_volume(0.0)
                else:
                    tts.set_volume(0.8)
            except Exception as e:
                logger.warning("tts_player.set_volume failed: %s", e)

    def toggle_passthrough(self, agent_id: str) -> None:
        """切换鼠标穿透。"""
        window = self._windows.get(agent_id)
        if window is None:
            return
        if hasattr(window, "_toggle_passthrough"):
            try:
                window._toggle_passthrough()
            except Exception as e:
                logger.warning("_toggle_passthrough failed: %s", e)
        else:
            # 兜底：直接取反 _mousePassthrough
            passthrough = getattr(window, "_mousePassthrough", False)
            window._mousePassthrough = not passthrough
            window._apply_penetration()

    def set_visible(self, agent_id: str, visible: bool) -> None:
        """显示/隐藏窗口。"""
        window = self._windows.get(agent_id)
        if window is None:
            return
        if visible:
            window.show()
        else:
            window.hide()

    def hide_all(self) -> None:
        """隐藏所有已启动的窗口。"""
        for window in list(self._windows.values()):
            try:
                window.hide()
            except Exception as e:
                logger.warning("hide_all failed for %s: %s", window, e)

    def show_all(self) -> None:
        """显示所有已启动的窗口。"""
        for window in list(self._windows.values()):
            try:
                window.show()
            except Exception as e:
                logger.warning("show_all failed for %s: %s", window, e)
