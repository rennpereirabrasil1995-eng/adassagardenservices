# Sphere Visualizer

Visualizador de palco em tempo real, inspirado nos shows da Sphere de Las Vegas.
Ele escuta o áudio que está tocando no seu PC e reage a ele: graves empurram a
geometria, agudos acendem os lasers e cada batida dispara flashes e trocas.

Arquivo único, sem instalação e sem dependências: `index.html`.

## Como rodar

1. Abra `index.html` no **Chrome** ou no **Edge** (dois cliques no arquivo já serve).
2. Clique em **Áudio do PC** e, na janela do navegador:
   - **Tela inteira** → marque **Compartilhar o áudio do sistema**; ou
   - **Aba** (Spotify Web, YouTube, Deezer) → marque **Compartilhar áudio da aba**.
3. Dê play na música. `F` para tela cheia, `H` para esconder o painel.

O vídeo do compartilhamento é descartado — só o áudio é analisado, e nada sai
do seu PC.

Se o botão **Áudio do PC** não funcionar abrindo o arquivo direto, sirva a pasta
em localhost (também é contexto seguro):

```bash
cd sphere-visualizer
python3 -m http.server 8000     # ou: npx serve .
# abra http://localhost:8000
```

### Outras fontes

- **Microfone** — funciona com o som saindo das caixas. É o plano B mais simples.
- **Arquivo** — escolha ou arraste um MP3/WAV para a janela. Aí o próprio
  visualizador toca a música, então a sincronia é perfeita.

### Por sistema operacional

| Sistema | Áudio do sistema inteiro | Observação |
|---|---|---|
| Windows | sim | "Tela inteira" + "Compartilhar o áudio do sistema" |
| Linux | só áudio de aba | ou escolha um *monitor* do PulseAudio como microfone |
| macOS | só áudio de aba | o macOS não libera o áudio do sistema; para tudo, use [BlackHole](https://github.com/ExistentialAudio/BlackHole) e selecione-o como microfone |

## Controles

| Tecla | Ação |
|---|---|
| `1`–`5` | escolhe a cena |
| `←` `→` | cena anterior / próxima |
| `A` | troca de cena sozinho a cada 48 batidas |
| `F` | tela cheia |
| `H` | esconde o painel |
| `Espaço` | play/pause (modo arquivo) |

O controle **Ganho** ajusta a sensibilidade; **Resolução** abaixa a resolução de
render se a placa de vídeo sofrer em 4K (as cenas 1 e 2 são as mais pesadas).

## Cenas

| # | Nome | O que é |
|---|---|---|
| 1 | Cathedral | corredor de cruzes de concreto com céu em chamas ao fundo (raymarching) |
| 2 | LED Dome | cúpula de painéis de LED que se deslocam e somem nas batidas |
| 3 | Storm | cúpula de pontos de luz, raios disparados pelo ritmo e lasers de palco |
| 4 | Lasers | leque de feixes volumétricos com névoa e strobo |
| 5 | Inferno | monólitos em cruz contra um céu de brasa, com fagulhas subindo |

## Como a sincronia funciona

O áudio entra num `AudioContext` e se divide em dois analisadores:

- **visual** (FFT 4096, alisado) → bandas grave / médio / agudo, que viram os
  uniforms `BASS`, `MID`, `HIGH` e `LVL` dos shaders. Um ganho automático
  acompanha o pico recente, então música baixa e música alta reagem igual.
- **rítmico** (FFT 2048, sem alisamento) → detecção de batida por **fluxo
  espectral**: a cada quadro soma-se só o que *subiu* nos graves (30–180 Hz)
  desde o quadro anterior, e dispara-se quando esse valor passa de
  `média + 1,8 × desvio-padrão` da última ~0,7 s.

Dois detalhes que mudam o resultado na prática:

- O fluxo é medido em **amplitude linear**, não em dB. Em dB um chimbau fraco
  parece quase tão forte quanto um bumbo e dispara falso; em escala linear, não.
- A análise roda em cadência própria (`setInterval` de 16 ms), **separada do
  render**. Se o FPS cair, o ritmo continua sendo lido ~60×/s e nada sai do
  compasso.

Medido com uma faixa de teste de 120 BPM: batidas detectadas com intervalos de
494–515 ms, sem perda e sem disparo falso nos chimbaus.

## Limites

Não é a Sphere: é uma tela só, sem 16K, sem domo e sem piro. O que dá pra levar
adiante a partir daqui:

- mandar as bandas e as batidas para luzes de verdade (WLED por UDP, DMX/Art-Net);
- saída em segunda tela / projetor, que já funciona hoje com `F` na janela certa;
- entrada MIDI para trocar de cena no controlador em vez do teclado.
