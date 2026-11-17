@echo off
rem Pick3 Reels 9:16 MP4 Auto Renderer (Rule 9 Compliant: Pure ASCII)
chcp 65001 > nul
title Pick3 Reels Auto Renderer

cd /d "d:\Vanguard_V6\Pick3"

set PYTHON_EXE=C:\Users\Administrator\miniconda3\python.exe
if not exist "%PYTHON_EXE%" (
    set PYTHON_EXE=python
)

"%PYTHON_EXE%" render_reels.py %*
pause
