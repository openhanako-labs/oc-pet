@echo off
cd /d "%~dp0"
set PYTHON=C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe
set PYTHONW=C:\Users\Administrator\AppData\Local\Programs\Python\Python312\pythonw.exe

echo Starting pet + bridge...

start /min "" "%PYTHONW%" companion_bridge.py

timeout /t 1 /nobreak >nul

echo Starting pet window...
"%PYTHON%" main.py

pause