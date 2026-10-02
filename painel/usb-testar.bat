@echo off
setlocal
cd /d "%~dp0usb"

set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY (
  where py >nul 2>nul && set "PY=py"
)
if not defined PY (
  echo  Python nao encontrado. Instale em https://python.org marcando "Add Python to PATH".
  pause & exit /b 1
)

echo  Conferindo as bibliotecas...
%PY% -c "import hid, PIL" 2>nul
if errorlevel 1 (
  echo  Instalando hidapi e pillow...
  %PY% -m pip install --quiet hidapi pillow
)

%PY% testar.py
pause
