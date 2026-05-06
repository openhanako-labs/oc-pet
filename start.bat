@echo off
cd /d "%~dp0"
python main.py
if %errorlevel% neq 0 (
    echo.
    echo Start failed. Make sure Python + PySide6 are installed:
    echo pip install PySide6 requests Pillow
    pause
)
