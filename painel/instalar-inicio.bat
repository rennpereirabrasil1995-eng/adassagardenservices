@echo off
rem Faz o painel ligar sozinho quando o Windows inicia.
setlocal

set "ALVO=%~dp0usb\painel-auto.bat"
set "ATALHO=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Painel do Aparelho.lnk"

if not exist "%ALVO%" (
  echo.
  echo  Nao achei o painel-auto.bat. Rode este arquivo de dentro
  echo  da pasta "painel" do projeto.
  echo.
  pause
  exit /b 1
)

powershell -NoProfile -Command ^
  "$s = (New-Object -ComObject WScript.Shell).CreateShortcut('%ATALHO%');" ^
  "$s.TargetPath = '%ALVO%';" ^
  "$s.WorkingDirectory = '%~dp0usb';" ^
  "$s.WindowStyle = 7;" ^
  "$s.Description = 'Mostra as imagens no macro pad';" ^
  "$s.Save()"

if exist "%ATALHO%" goto pronto
echo.
echo  Nao consegui criar o atalho.
echo.
pause
exit /b 1

:pronto
echo.
echo  PRONTO.
echo.
echo  O painel vai ligar sozinho toda vez que o Windows iniciar,
echo  com a janela minimizada.
echo.
echo  Para desligar isso depois: desinstalar-inicio.bat
echo.
echo  Lembre de FECHAR o programa da Soomfon, inclusive o icone
echo  perto do relogio - os dois nao podem usar o aparelho juntos.
echo.
pause
