@echo off
cd /d "%~dp0"
python bridge.py start --open
if errorlevel 1 pause
