@echo off
cd /d "%~dp0"

REM ── 启动 WebSocket 服务器（后台线程）──
python -c "import asyncio, ws_server; asyncio.run(ws_server.main())" &
set WS_PID=%errorlevel%

REM 等待 WS 服务器就绪
timeout /t 2 /nobreak >nul

REM ── 启动桌宠 ──
python main.py
set MAIN_PID=%errorlevel%

REM ── 清理 ──
taskkill /f /im python.exe >nul 2>&1
if %MAIN_PID% neq 0 (
    echo.
    echo Main app failed. Make sure Python + PySide6 are installed:
    echo pip install PySide6 requests Pillow websockets
    pause
)
