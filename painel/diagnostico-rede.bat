@echo off
rem Descobre por que o celular nao abre a pagina do painel.
setlocal
cd /d "%~dp0"

echo.
echo  DIAGNOSTICO DA PAGINA DO PAINEL
echo  ===============================
echo.

echo  1) O servidor esta rodando?
netstat -ano | findstr ":8080" | findstr "LISTENING" >nul
if errorlevel 1 (
  echo     NAO. Ninguem esta escutando na porta 8080.
  echo     -^> Abra o iniciar.bat e deixe a janela aberta.
) else (
  echo     SIM, tem alguem escutando na porta 8080.
)
echo.

echo  2) Enderecos deste computador na rede:
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /C:"IPv4"') do echo       http://%%a:8080/painel
echo.

echo  3) Regra de firewall para a porta 8080:
netsh advfirewall firewall show rule name="Painel do Aparelho" >nul 2>&1
if errorlevel 1 (
  echo     NAO existe.
  echo     -^> Botao direito em liberar-firewall.bat, "Executar como administrador".
) else (
  echo     Existe.
)
echo.

echo  4) A pagina responde aqui no proprio PC?
curl -s -o nul -w "     HTTP %%{http_code}" http://localhost:8080/painel 2>nul
if errorlevel 1 echo      Nao respondeu.
echo.
echo.
echo  Lembre: o celular tem que estar no MESMO wi-fi que este computador.
echo.
pause
