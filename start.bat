@echo off
chcp 65001 > nul
title ERLI Monitor
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
echo.
echo  Zapusk ERLI Monitor...
echo.
"C:\Users\Victus\AppData\Local\Python\bin\python.exe" app.py
echo.
echo  Server ostanovlen.
pause
