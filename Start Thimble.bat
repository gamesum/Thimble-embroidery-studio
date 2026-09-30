@echo off
title Thimble - Embroidery Studio
cd /d "%~dp0"
echo.
echo   Thimble - Embroidery Studio
echo   Opening in your browser... (keep this window open while you work; close it to quit)
echo.
"%~dp0venv\Scripts\python.exe" "%~dp0app.py"
if errorlevel 1 (
  echo.
  echo   Something went wrong starting Thimble. See the message above.
  pause
)
