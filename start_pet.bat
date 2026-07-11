@echo off
cd /d "%~dp0"
echo Starting OC Desktop Pet...

:: ??? venv
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" main.py
    goto :end
)

:: ????? Python
where python >nul 2>&1
if %errorlevel%==0 (
    python main.py
    goto :end
)

echo [ERROR] Python not found! Please install Python 3.10+ and add to PATH.
pause

:end
