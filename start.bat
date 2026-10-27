@echo off
cd /d "%~dp0"
python -m whale_friend
if errorlevel 1 pause
