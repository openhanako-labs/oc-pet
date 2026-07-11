#!/usr/bin/env python3
"""OC 桌面宠物 - 月曦夜 & 奥菲莉娅

多桌宠模式：每个 Hanako agent 可以独立运行一个桌宠窗口。
"""
import sys
import os
import logging
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QFont

# ── 日志 ──
logging.basicConfig(
    level=logging.INFO,
    format='[%(levelname)s] %(name)s: %(message)s'
)

# Add project root to path
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from pet_manager import PetManager


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("OC Desktop Pet")

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
        discovered = manager.discover_agents()
        # 优先用 ophelia（Hanako agent）
        for agent in discovered:
            if agent["id"] == "ophelia":
                manager.add_agent("ophelia")
                break
        # 没有 ophelia，用第一个有精灵的 Hanako agent
        if not manager.agents:
            for agent in discovered:
                if agent.get("has_sprites"):
                    manager.add_agent(agent["id"])
                    break
        # 还是没有，用内置 default 角色
        if not manager.agents:
            from pathlib import Path
            default_char = Path(__file__).parent / "characters" / "default"
            if default_char.exists():
                manager._config.setdefault("agents", []).append({
                    "id": "default",
                    "enabled": True,
                    "position": {"x": -1, "y": -1},
                    "scale": 1.0,
                    "builtin": True,
                })
                manager._save_config()

    manager.launch_all()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
