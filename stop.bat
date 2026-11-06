@echo off
cd /d "%~dp0"
python bridge.py stop
if errorlevel 1 pause
