# Descobre qual dispositivo USB e o macro pad, comparando a lista
# com o cabo conectado e desconectado.
# O que sumir da lista e o aparelho.

function Listar {
  Get-CimInstance Win32_PnPEntity -ErrorAction SilentlyContinue |
    Where-Object { $_.PNPDeviceID -match '^(USB|HID)' } |
    ForEach-Object {
      [pscustomobject]@{
        Id     = $_.PNPDeviceID
        Nome   = $_.Name
        Classe = $_.PNPClass
      }
    }
}

function VidPid($id) {
  if ($id -match 'VID_([0-9A-Fa-f]{4})&PID_([0-9A-Fa-f]{4})') {
    return "$($Matches[1].ToLower()):$($Matches[2].ToLower())"
  }
  return "????:????"
}

Clear-Host
Write-Host ""
Write-Host "  ACHAR O MACRO PAD" -ForegroundColor Cyan
Write-Host "  =================" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Passo 1 de 2" -ForegroundColor Yellow
Write-Host "  Deixe o aparelho CONECTADO no USB."
Read-Host "  Quando estiver conectado, aperte Enter" | Out-Null

$antes = Listar
Write-Host "  ... $($antes.Count) dispositivos vistos com ele conectado."
Write-Host ""
Write-Host "  Passo 2 de 2" -ForegroundColor Yellow
Write-Host "  Agora TIRE o cabo do aparelho e espere uns 3 segundos."
Read-Host "  Depois de tirar, aperte Enter" | Out-Null

Start-Sleep -Seconds 2
$depois = Listar
Write-Host "  ... $($depois.Count) dispositivos sem ele."
Write-Host ""

$idsDepois = $depois | ForEach-Object { $_.Id }
$sumiram = $antes | Where-Object { $idsDepois -notcontains $_.Id }

$saida = @()
$saida += "=== APARELHO ENCONTRADO ==="
$saida += "Windows: $([System.Environment]::OSVersion.Version)"
$saida += "Dispositivos: $($antes.Count) conectado / $($depois.Count) desconectado"
$saida += ""

if ($sumiram.Count -eq 0) {
  Write-Host "  Nada sumiu da lista." -ForegroundColor Red
  Write-Host "  Tente de novo esperando mais tempo depois de tirar o cabo," -ForegroundColor DarkGray
  Write-Host "  ou tire o cabo direto do computador (nao de um hub)." -ForegroundColor DarkGray
  $saida += "NENHUM dispositivo sumiu - repetir o teste."
} else {
  Write-Host "  ESTE E O APARELHO:" -ForegroundColor Green
  Write-Host ""
  foreach ($d in $sumiram) {
    $vp = VidPid $d.Id
    Write-Host ("    VID:PID = {0}" -f $vp) -ForegroundColor Green
    Write-Host ("    Nome    = {0}" -f $d.Nome)
    Write-Host ("    Classe  = {0}" -f $d.Classe)
    Write-Host ("    Id      = {0}" -f $d.Id) -ForegroundColor DarkGray
    Write-Host ""
    $saida += "VID:PID = $vp"
    $saida += "Nome    = $($d.Nome)"
    $saida += "Classe  = $($d.Classe)"
    $saida += "Id      = $($d.Id)"
    $saida += ""
  }
}

$arq = "$env:USERPROFILE\Desktop\aparelho.txt"
$saida | Out-File -FilePath $arq -Encoding utf8
Write-Host "  Tudo isso tambem foi salvo em:" -ForegroundColor Cyan
Write-Host "    $arq"
Write-Host ""
Write-Host "  Pode plugar o aparelho de volta."
Write-Host ""
Read-Host "  Aperte Enter para fechar" | Out-Null
