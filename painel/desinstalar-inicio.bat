@echo off
rem Tira o painel da inicializacao do Windows.
setlocal
set "ATALHO=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Painel do Aparelho.lnk"
if exist "%ATALHO%" (
  del "%ATALHO%"
  echo.
  echo  Tirado da inicializacao. O painel nao liga mais sozinho.
) else (
  echo.
  echo  Ele ja nao estava na inicializacao.
)
echo.
pause
