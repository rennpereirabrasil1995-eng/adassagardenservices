@echo off
rem Roda o painel. Chamado pelo atalho da inicializacao do Windows.
rem Para mudar o que aparece, edite a ultima linha deste arquivo.
setlocal
cd /d "%~dp0"

set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY (
  where py >nul 2>nul && set "PY=py"
)
if not defined PY (
  echo  Python nao encontrado. Instale em https://python.org
  echo  marcando "Add Python to PATH".
  pause
  exit /b 1
)

%PY% -c "import hid, PIL" 2>nul
if errorlevel 1 %PY% -m pip install --quiet hidapi pillow

rem O aparelho demora um pouco para aparecer depois que o Windows sobe.
rem Tenta cinco vezes, esperando 10 segundos entre elas.
set TENTATIVA=0
:tentar
set /a TENTATIVA+=1
%PY% cheio.py
if errorlevel 1 (
  if %TENTATIVA% lss 5 (
    timeout /t 10 /nobreak >nul
    goto tentar
  )
)
