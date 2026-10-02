@echo off
setlocal
cd /d "%~dp0"

set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY (
  where py >nul 2>nul && set "PY=py"
)
if not defined PY (
  echo.
  echo  Python nao encontrado.
  echo  Instale em https://python.org e marque "Add Python to PATH".
  echo.
  pause
  exit /b 1
)

%PY% detectar.py
