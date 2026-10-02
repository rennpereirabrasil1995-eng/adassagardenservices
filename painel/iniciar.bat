@echo off
cd /d "%~dp0"
python server.py --porta %1
pause
