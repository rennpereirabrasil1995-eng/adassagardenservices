@echo off
rem Libera a porta 8080 no firewall do Windows, para o celular alcancar
rem a pagina do painel. Precisa ser executado como ADMINISTRADOR:
rem botao direito neste arquivo -> "Executar como administrador".

net session >nul 2>&1
if errorlevel 1 (
  echo.
  echo  Este arquivo precisa ser executado como ADMINISTRADOR.
  echo.
  echo  Feche esta janela, clique com o botao DIREITO neste arquivo
  echo  e escolha "Executar como administrador".
  echo.
  pause
  exit /b 1
)

netsh advfirewall firewall delete rule name="Painel do Aparelho" >nul 2>&1
netsh advfirewall firewall add rule name="Painel do Aparelho" dir=in action=allow protocol=TCP localport=8080

echo.
echo  PRONTO. A porta 8080 esta liberada.
echo.
echo  Agora o celular consegue abrir a pagina do painel, desde que
echo  esteja no MESMO wi-fi que este computador.
echo.
pause
