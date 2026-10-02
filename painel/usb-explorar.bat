@echo off
setlocal
cd /d "%~dp0usb"
set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY ( where py >nul 2>nul && set "PY=py" )
if not defined PY ( echo  Python nao encontrado. & pause & exit /b 1 )
%PY% explorar.py %*
pause
