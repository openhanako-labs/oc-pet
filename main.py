#!/usr/bin/env python3
"""OC 桌面宠物 - 月曦夜 & 奥菲莉娅

多桌宠模式：每个 Hanako agent 可以独立运行一个桌宠窗口。
"""
import sys
import os
import logging

# ── 沙盒模式快捷开关 ──
if "--sandbox" in sys.argv:
    # 移除参数，委托给 sandbox_runner
    sys.argv.remove("--sandbox")
    from sandbox_runner import apply_patches, run_interactive
    logging.basicConfig(level=logging.INFO, format='[%(levelname)s] %(name)s: %(message)s')
    apply_patches()
    run_interactive()
    sys.exit(0)

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QFont

# ── 主题系统（必须在 QApplication 创建后、其他 UI 之前） ──
from ui.theme import init_default

# ── 日志 ──
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

# Add project root to path
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from pet_manager import PetManager


def main():
    # 检查沙盒标志
    app = QApplication(sys.argv)
    app.setApplicationName("OC Desktop Pet")

    # 初始化主题系统（在所有 UI 创建前）
    theme_mgr = init_default(app)
    theme_mgr.apply_initial()
    logging.info("初始主题：%s", theme_mgr.current)

    # 根据 config 加载 theme_mode（auto/light/dark）
    from config import load_config, save_config
    cfg = load_config()
    theme_mode = cfg.get("theme_mode", "auto")
    if theme_mode in ("auto", "light", "dark"):
        theme_mgr.set_mode(theme_mode)
        logging.info("从 config 加载主题模式：%s", theme_mode)

    # Global font
    font = QFont("Microsoft YaHei UI", 10)
    app.setFont(font)

    # 清除旧的 response.json，避免启动时播放上次的回复
    try:
        from paths import RESPONSE_FILE
        if RESPONSE_FILE.exists():
            RESPONSE_FILE.unlink()
            logging.info("Cleared old response.json")
    except Exception:
        pass

    manager = PetManager()

    # 如果 config 里没有 agents 列表（首次运行），自动添加
    if not manager.agents:
        from pathlib import Path

        # 1. 优先用月薪喵
        yuexinmiao = Path(__file__).parent / "characters" / "yuexinmiao"
        if yuexinmiao.exists():
            manager._config.setdefault("agents", []).append({
                "id": "yuexinmiao",
                "enabled": True,
                "position": {"x": -1, "y": -1},
                "scale": 1.0,
                "builtin": True,
            })
            manager._save_config()
        else:
            # 2. 扫描 Hanako agents
            discovered = manager.discover_agents()
            for agent in discovered:
                if agent["id"] == "yuexinmiao":
                    manager.add_agent("yuexinmiao")
                    break
            if not manager.agents:
                for agent in discovered:
                    if agent.get("has_sprites"):
                        manager.add_agent(agent["id"])
                        break

    manager.launch_all()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
