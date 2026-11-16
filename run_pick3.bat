@echo off
rem Pick3 Mobile Web Launcher (Rule 9 Compliant: Pure ASCII only)
chcp 65001 > nul
title Pick3 - 10s Restaurant Picker

cd /d "d:\Vanguard_V6\Pick3"

set PYTHON_EXE=C:\Users\Administrator\miniconda3\python.exe
if not exist "%PYTHON_EXE%" (
    set PYTHON_EXE=python
)

echo Starting Pick3 server on http://127.0.0.1:8088 ...
"%PYTHON_EXE%" -m uvicorn app:app --host 0.0.0.0 --port 8088
pause
