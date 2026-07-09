@echo off
cd /d "%~dp0"
set PYTHON=C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe
echo Starting pet (with built-in conversation engine)...
"%PYTHON%" main.py
pause