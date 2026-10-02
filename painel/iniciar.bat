@echo off
setlocal
cd /d "%~dp0"

rem porta: usa a que vier como parametro, senao 8080
set "PORTA=%~1"
if "%PORTA%"=="" set "PORTA=8080"

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

echo  Ligando o painel na porta %PORTA%...
echo.
%PY% server.py --porta %PORTA%
echo.
pause
