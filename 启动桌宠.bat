@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 正在启动 OC 桌面宠物...
python main.py
if %errorlevel% neq 0 (
    echo.
    echo 启动失败，请确认 Python 和 PySide6 已安装。
    echo 首次使用请先安装依赖：pip install PySide6 requests Pillow
    pause
)
