#!/usr/bin/env python3
"""OC 桌面宠物 - 月曦夜 & 奥菲莉娅"""
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

from pet import PetWindow


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("OC Desktop Pet")
    
    # Global font
    font = QFont("Microsoft YaHei UI", 10)
    app.setFont(font)
    
    window = PetWindow()
    window.show()
    
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
