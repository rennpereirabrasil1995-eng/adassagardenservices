@echo off
rem Liga o painel inteiro: o servidor (para mandar fotos pelo celular)
rem e o desenho no aparelho.
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

rem 1) o servidor, numa janela propria minimizada.
rem    E ele que serve a pagina /painel para o celular.
start "Servidor do Painel" /min %PY% "%~dp0..\server.py"

rem 2) o desenho no aparelho. O aparelho demora a aparecer depois que o
rem    Windows sobe, entao tenta cinco vezes com 10 segundos de intervalo.
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
