# Abre a TELA do painel em tela cheia no monitor do aparelho.
#   .\abrir-tela.ps1          -> lista os monitores e pergunta
#   .\abrir-tela.ps1 2        -> abre direto no monitor 2
#   .\abrir-tela.ps1 2 9000   -> monitor 2, servidor na porta 9000
param(
  [int]$Monitor = 0,
  [int]$Porta = 8080
)

Add-Type -AssemblyName System.Windows.Forms
$telas = [System.Windows.Forms.Screen]::AllScreens

Write-Host ""
Write-Host "  MONITORES ENCONTRADOS" -ForegroundColor Cyan
Write-Host "  ---------------------"
for ($i = 0; $i -lt $telas.Count; $i++) {
  $t = $telas[$i]
  $b = $t.Bounds
  $marca = if ($t.Primary) { "  (principal)" } else { "" }
  Write-Host ("  [{0}]  {1}x{2}  na posicao {3},{4}   {5}{6}" -f `
    ($i + 1), $b.Width, $b.Height, $b.X, $b.Y, $t.DeviceName, $marca)
}
Write-Host ""

if ($Monitor -lt 1 -or $Monitor -gt $telas.Count) {
  Write-Host "  O monitor do aparelho normalmente e o de resolucao menor." -ForegroundColor DarkGray
  $resp = Read-Host "  Em qual monitor abrir a tela? (1-$($telas.Count))"
  $Monitor = [int]$resp
}
if ($Monitor -lt 1 -or $Monitor -gt $telas.Count) {
  Write-Host "  Numero invalido." -ForegroundColor Red
  exit 1
}

$alvo = $telas[$Monitor - 1].Bounds
$url  = "http://localhost:$Porta/"

# o servidor esta no ar?
try {
  Invoke-WebRequest -Uri "$url" -TimeoutSec 3 -UseBasicParsing | Out-Null
} catch {
  Write-Host "  O servidor nao respondeu em $url" -ForegroundColor Yellow
  Write-Host "  Abra o iniciar.bat primeiro (deixe aquela janela aberta)." -ForegroundColor Yellow
  Write-Host ""
  Read-Host "  Aperte Enter para fechar"
  exit 1
}

$navegadores = @(
  "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
  "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
  "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe",
  "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe",
  "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe"
)
$exe = $navegadores | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $exe) {
  Write-Host "  Nao achei Chrome nem Edge instalado." -ForegroundColor Red
  Read-Host "  Aperte Enter para fechar"
  exit 1
}

# perfil separado: sem isso o navegador so abre uma aba na janela existente
# e ignora a posicao e o modo kiosk.
$perfil = "$env:LOCALAPPDATA\painel-kiosk"

$parametros = @(
  "--user-data-dir=$perfil",
  "--kiosk", $url,
  "--window-position=$($alvo.X),$($alvo.Y)",
  "--window-size=$($alvo.Width),$($alvo.Height)",
  "--autoplay-policy=no-user-gesture-required",
  "--noerrdialogs", "--disable-infobars",
  "--disable-session-crashed-bubble",
  "--check-for-update-interval=31536000"
)

Write-Host ("  Abrindo no monitor {0} ({1}x{2}) com {3}..." -f `
  $Monitor, $alvo.Width, $alvo.Height, (Split-Path $exe -Leaf)) -ForegroundColor Green
Write-Host "  Para fechar: Alt+F4 nessa janela."
Write-Host ""
Start-Process -FilePath $exe -ArgumentList $parametros
