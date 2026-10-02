# Painel de Mídia

Programa para usar a tela do macro pad (soomfon e parecidos) como display:
imagens, GIFs e vídeos — tanto **uma mídia por tecla** quanto **tela cheia**.

O aparelho tem **um LCD só** por baixo e uma chapa de metal com **6 janelas
(3 colunas × 2 linhas)**. Cada tecla transparente mostra um pedaço dessa mesma
tela. Por isso o programa desenha uma grade 3×2 e tem um **modo de calibração**
pra você alinhar os quadrados exatamente atrás das janelinhas.

Não precisa instalar nada: só **Python 3.8+** (já vem no Raspberry Pi e no macOS;
no Windows, baixe em python.org marcando "Add Python to PATH").

---

## Baixar no seu computador

Os arquivos ficam no GitHub — antes de qualquer coisa, traga-os pro PC.

**Windows (PowerShell — tecle Win, digite "PowerShell", abra e cole):**

```powershell
cd $env:USERPROFILE\Desktop
iwr https://github.com/rennpereirabrasil1995-eng/adassagardenservices/archive/refs/heads/ccr-c658d0db-inlhu7.zip -OutFile painel.zip
Expand-Archive painel.zip -DestinationPath . -Force
explorer .\adassagardenservices-ccr-c658d0db-inlhu7\painel
```

Vai abrir a pasta `painel` na sua Área de Trabalho. É ali que ficam o
`iniciar.bat` e o `detectar.bat`.

**Ou pelo navegador:** baixe o ZIP [deste link](https://github.com/rennpereirabrasil1995-eng/adassagardenservices/archive/refs/heads/ccr-c658d0db-inlhu7.zip)
e extraia.

**Linux / macOS:**

```bash
git clone -b ccr-c658d0db-inlhu7 https://github.com/rennpereirabrasil1995-eng/adassagardenservices.git
cd adassagardenservices/painel
```

> No Windows use `iniciar.bat` e `detectar.bat` (dois cliques).
> O `.sh` é só para Linux e macOS.

## 0. Primeiro: descobrir como a tela está ligada

Rode isto **com o aparelho conectado**:

```bash
python3 detectar.py      # Windows: dois cliques em detectar.bat
```

Ele diz em qual dos dois casos você está:

- **Caso 1 — a tela entra como monitor** (USB-C DP Alt Mode ou adaptador tipo
  DisplayLink). É o caso que este programa atende: siga para o item 1.
- **Caso 2 — a tela é desenhada pelo próprio aparelho via USB HID** (estilo
  Stream Deck). O navegador **não** alcança essa tela: o PC manda as imagens
  de cada tecla pelo cabo USB, e é preciso um programa que fale HID.

  Para descobrir qual dispositivo USB é o pad, rode **`achar-aparelho.bat`**:
  ele compara a lista de dispositivos com o cabo conectado e desconectado — o
  que sumir é o aparelho. O resultado (o `VID:PID`) fica salvo em
  `Área de Trabalho\aparelho.txt`.
---

## Caso 2: a tela é desenhada pelo próprio aparelho (USB HID)

É o caso do **Soomfon CN002** (`VID 1500 : PID 3001`) e dos parentes da família
Ajazz AKP03 / Mirabox N3. O LCD **não** é um monitor: o PC manda a imagem pelo
cabo USB e o aparelho desenha.

### Instalar

```
pip install hidapi pillow
pip install opencv-python     (só para tocar MP4)
```

### Os dois modos

| Programa | O que faz |
|---|---|
| **`usb-cheio.bat`** | uma imagem ou vídeo ocupando **a tela inteira** |
| **`usb-rodar.bat`** | os 6 quadradinhos, um por tecla, controlados pelo celular |

Teste do aparelho: **`usb-testar.bat`** (acende as 6 teclas numeradas).

> **Feche o programa do fabricante antes**, inclusive o ícone perto do relógio.
> Dois programas não podem usar o aparelho ao mesmo tempo.

### O que descobrimos sobre este aparelho

O protocolo público destes aparelhos só documenta **6 teclas de 64×64**. Testando
no aparelho de verdade, descobrimos três coisas que não estão em lugar nenhum:

1. **O firmware desenha o JPEG no tamanho que receber.** Não fixa em 64×64 — é
   isso que torna a tela cheia possível.
2. **O limite é o peso do arquivo, não os pixels.** Até **5 KB** por imagem ele
   troca indefinidamente (10 de 10 trocas seguidas, confirmado). Em 5,5 e 6 KB
   trava a conexão USB, e aí só desplugando o cabo.
3. **A área útil é 320×480**, que depois do giro de 90° dá os 480×320 do painel.

### Quanto cabe em 4 KB

O aparelho sustenta **4 KB por imagem** rodando indefinidamente; acima disso
trava a conexão USB. Dentro desse orçamento:

| Conteúdo | Resultado em 320×480 (tela inteira) |
|---|---|
| Foto | qualidade 10 — granulado, porém **a tela toda** |
| Desenho gerado (`gerar.py`) | qualidade 20 a 44 — limpo e **a tela toda** |

O que tornou isso possível foi gravar o JPEG com **`optimize=True`**, que monta
a tabela de Huffman sob medida: **26% a menos de bytes sem perder nada da
imagem**. Antes dele, uma foto só alcançava 204×307, ou seja 41% da tela.

Duas coisas importam tanto quanto o peso:

- **`--atraso`**: milissegundos entre os blocos de 1 KB do envio. Sem isso a
  imagem chega rasgada, com faixas trocadas de lugar. O padrão é 5 ms; suba
  para 15 se ainda rasgar.
- **`--nitidez`**: a qualidade JPEG mínima. Baixo enche mais a tela e granula;
  alto deixa limpo porém menor.

Quando o aparelho recusa uma escrita, o programa reconecta e baixa o orçamento
sozinho, e segue rodando.

### Gerar imagens sob medida

`gerar.py` desenha direto no formato do painel, ocupando a tela inteira:

```
python gerar.py --texto "ADASSA" --subtexto "garden services" --cor 1
python gerar.py --texto "ABERTO" --estilo liso --cor 3
python gerar.py --listar          mostra os 5 estilos e as 7 cores
```

Os arquivos saem na pasta `midia`, prontos para o `cheio.py` mostrar.

### Ajustes

```
python cheio.py --segundos 10      troca de foto a cada 10s
python cheio.py --fps 8            vídeo mais fluido
python cheio.py --max-kb 4         mais conservador, se travar
python cheio.py --arquivo foto.jpg uma imagem só
```

Se travar: **despluga e pluga o cabo**, e rode com `--max-kb 4 --pausa 0.5`.
Nada do que o programa manda grava firmware; travar é só estado de exibição.

### Ferramentas de diagnóstico

Ficaram na pasta `usb/`, caso você queira investigar outro aparelho da família:

| Programa | Para quê |
|---|---|
| `repetir.py` | quantas imagens seguidas o aparelho aguenta, num peso dado |
| `limite.py` | acha o teto de peso do JPEG |
| `varrer.py` | acha o teto de tamanho em pixels |
| `ajustar.py` | descobre a proporção do painel |
| `grande.py`, `mapear.py` | alvos para medir a área útil |
| `explorar.py` | teclas extras, imagem grande e o comando MOD |

### Crédito do protocolo

O protocolo USB não é publicado pelo fabricante. Foi descoberto por engenharia
reversa pelo projeto [mirajazz](https://github.com/4ndv/mirajazz) e pelo plugin
[opendeck-akp03](https://github.com/4ndv/opendeck-akp03), de onde vêm os
parâmetros deste aparelho. `usb/deck.py` é uma reimplementação em Python da
parte necessária para desenhar; o resto (tamanho livre, limite de peso, área
útil) foi medido neste aparelho.

---

## Caso 1: a tela aparece como monitor

## 1. Ligar

**Linux / Raspberry Pi / macOS**

```bash
cd painel
./iniciar.sh
```

**Windows**: dê dois cliques em `iniciar.bat`.

Vai aparecer algo assim:

```
TELA (abra nessa tela e dê F11):  http://localhost:8080/
CONTROLE (celular/outro PC):      http://192.168.0.15:8080/painel
```

- **TELA** → é o que vai no display do aparelho.
- **CONTROLE** → abre no celular (mesmo wi-fi) ou em outra aba do PC.

Para mudar a porta: `./iniciar.sh 9000`.

---

## 2. Colocar a TELA no display do aparelho

Depende de como esse LCD está ligado. Os três casos:

### a) A tela aparece como **segundo monitor** do PC
É o caso mais comum nesses pads com LCD interno. Confira em
**Win + P** ou em Configurações → Sistema → Tela: se aparecer um monitor a mais
quando o pad está ligado, é este o caso.

**Windows — jeito automático:**

1. Dois cliques em `iniciar.bat` (deixe essa janela aberta).
2. Dois cliques em `abrir-tela.bat`.

Ele lista os monitores com resolução e posição, pergunta em qual abrir e já
sobe a tela em modo kiosk lá. O monitor do aparelho costuma ser **o de menor
resolução**. Errou? Rode de novo e escolha outro número.

Para abrir direto, sem perguntar: `abrir-tela.bat 2` (monitor 2).

**Na mão:** abra `http://localhost:8080/` no Chrome, arraste a janela pra tela
do aparelho e aperte **F11**.

**Linux:**

```bash
./deploy/kiosk.sh http://localhost:8080/ 1920,0
```

Troque `1920,0` pela posição do segundo monitor (1920 = largura do principal).

### b) O painel roda **Android**
Abra o navegador do aparelho em `http://IP_DO_PC:8080/` e mande "adicionar à tela
inicial" / modo tela cheia. Vale também usar um app de kiosk (ex.: Fully Kiosk).

### c) A tela está ligada por **HDMI** num Raspberry Pi
Rode o servidor no próprio Pi e use `deploy/kiosk.sh` no autostart (item 6).

---

## 3. Mandar as imagens e vídeos

No **controle** (`/painel`), em "Enviar imagens e vídeos": toque e escolha, ou
arraste os arquivos. Dá pra mandar vários de uma vez, direto do celular.

Formatos que o navegador toca sem dor de cabeça:

| Tipo   | Use                              | Evite              |
|--------|----------------------------------|--------------------|
| Imagem | JPG, PNG, GIF, WEBP, AVIF        | —                  |
| Vídeo  | **MP4 (H.264)**, WEBM            | MKV, AVI, MOV      |

O painel marca com ⚠ o que o navegador provavelmente não vai conseguir tocar.
Nesse caso, converta pra MP4.

---

## 4. Calibrar a máscara (faça isso uma vez)

1. No controle, escolha o modo **Calibrar**. A tela do aparelho mostra 6
   quadrados azuis numerados de 1 a 6.
2. Em "Calibragem da máscara", mexa nos controles até cada quadrado ficar
   exatamente atrás de uma janelinha da chapa:
   - **Largura / Altura da janela** — tamanho de cada quadrado
   - **Espaço entre colunas / linhas** — distância entre eles
   - **Mover tudo ↔ / ↕** — desloca o bloco inteiro
   - **Girar a tela** — se o LCD está montado de lado (90° / 180° / 270°)
   - **Espelhar** — se a imagem aparece invertida
3. Volte pro modo **Grade**. Pronto, fica salvo.

> Os valores que já vêm (150×150 px, 24 px de espaço) são só um chute inicial —
> o certo depende da resolução do seu LCD e de onde a chapa está.

---

## 5. Usar

**Modo Grade** — cada janelinha com sua mídia.
Toque numa das 6 teclas no controle e escolha a imagem/vídeo. Ainda dá pra
ajustar **zoom** e **deslocamento** (pra centralizar o que importa dentro do
quadradinho) e escrever um **texto** embaixo.

**Modo Tela cheia** — o LCD inteiro vira um player: passa as fotos no tempo que
você escolher e toca os vídeos em loop. Dá pra marcar quais arquivos entram,
ordem aleatória e som ligado/desligado.

A tela se atualiza sozinha em ~2 segundos depois de qualquer mudança — não
precisa mexer no aparelho. O botão **Recarregar telas** força o recarregamento.

---

## 6. Ligar sozinho quando o aparelho liga (Raspberry Pi / Linux)

```bash
# 1) o servidor
sudo cp deploy/painel.service /etc/systemd/system/
sudo nano /etc/systemd/system/painel.service   # confira User= e os caminhos
sudo systemctl enable --now painel

# 2) a tela em kiosk
mkdir -p ~/.config/autostart
cat > ~/.config/autostart/painel-tela.desktop <<'DESKTOP'
[Desktop Entry]
Type=Application
Name=Painel Tela
Exec=/home/pi/painel/deploy/kiosk.sh http://localhost:8080/ 1920,0
X-GNOME-Autostart-enabled=true
DESKTOP
```

No Windows, um atalho pro `iniciar.bat` na pasta
`shell:startup` resolve o servidor; pro navegador, crie outro atalho com
`chrome.exe --kiosk http://localhost:8080/`.

---

## Atalhos na tela

| Tecla / gesto     | O que faz              |
|-------------------|------------------------|
| **F** ou 2 cliques| Entra/sai de tela cheia|
| **R**             | Recarrega              |

---

## Detalhes técnicos

- Os arquivos ficam em `painel/midia/`. Dá pra copiar direto pra lá, sem usar
  o controle — o programa percebe sozinho.
- As configurações ficam em `painel/midia/_config.json`.
- Vídeo é servido com suporte a *Range*, então funciona em iOS/Safari também.
- O servidor é feito pra **rede local**. Não tem senha: não exponha na internet.
- Trocar a pasta de mídia: `python3 server.py --midia /caminho/da/pasta`.
