# Cathedral

Cena de palco em tempo real — um corredor de cruzes de concreto sob um céu de
brasa — renderizada por raymarching e sincronizada com a música que estiver
tocando no PC. Arquivo único, sem instalação: `index.html`.

A versão anterior, com cinco cenas, ficou guardada em `scenes-v1.html`.

## Como rodar

1. Abra `index.html` no **Chrome** ou no **Edge**.
2. Clique em **Áudio do PC** e, na janela do navegador:
   - **Tela inteira** → marque **Compartilhar o áudio do sistema**; ou
   - **Aba** (Spotify Web, YouTube) → marque **Compartilhar áudio da aba**.
3. Dê play. `F` para tela cheia, `H` para esconder o painel.

O vídeo do compartilhamento é descartado — só o áudio é analisado, e nada sai
do seu PC. Se abrir o arquivo direto não funcionar, sirva em localhost:

```bash
cd sphere-visualizer
python3 -m http.server 8000     # abra http://localhost:8000
```

Também aceita **microfone** (pega o som das caixas) e **arquivo** MP3/WAV
arrastado para a janela — nesse caso a sincronia é exata, porque o próprio
visualizador toca a música.

| Sistema | Áudio do sistema inteiro | Observação |
|---|---|---|
| Windows | sim | "Tela inteira" + "Compartilhar o áudio do sistema" |
| Linux | só áudio de aba | ou escolha um *monitor* do PulseAudio como microfone |
| macOS | só áudio de aba | o macOS não libera o áudio do sistema; para tudo, use [BlackHole](https://github.com/ExistentialAudio/BlackHole) como microfone |

## Controles

| Tecla | Ação |
|---|---|
| `F` | tela cheia |
| `H` | esconde o painel |
| `N` | corta para o próximo plano |
| `R` | sorteia outra sequência de planos |
| `Espaço` | play/pause (modo arquivo) |

## A câmera não repete

Não há animação em laço. Um diretor monta a sequência plano a plano, em dez
tipos de movimento — travelling à frente, travelling lateral, grua, rasante,
arco orbital, sobrevoo, dolly zoom, passagem rente, tilt para as vigas e
recuo.

Três coisas garantem que nada se repita ao longo de cinco minutos ou de uma
hora:

- Os tipos saem de um **saco embaralhado**: nenhum volta antes de todos os dez
  aparecerem.
- Os parâmetros de cada plano (velocidade, altura, abertura da lente, foco,
  tremor, duração de 9 a 17 s) vêm de um **gerador semeado pelo índice do
  plano** — a combinação nunca se repete.
- A câmera **sempre avança** para um trecho novo do corredor (+30 a +85 por
  plano) e o cenário é gerado por hash da posição, então a arquitetura é outra
  a cada vez. Nunca se volta ao mesmo lugar.

O corte espera a próxima batida detectada, então a montagem cai no tempo da
música. O tremor de câmera na mão é uma soma de senos de frequências
incomensuráveis — não tem período, logo não cicla.

## O que faz a imagem não parecer desenho

O render é em vários passos, não um shader só:

1. **Cena** (HDR, RGBA16F) — raymarching com sombras suaves por *cone tracing*,
   oclusão ambiente, normal perturbada por ruído (relevo fino no concreto),
   céu como luz ambiente hemisférica, quique quente do chão, especular com
   Fresnel, quatro refletores de palco e **reflexo real no piso molhado** (um
   segundo raio marchado a partir da superfície).
2. **Volumetria** — a luz dispersa na poeira é integrada ao longo do raio com
   função de fase de Henyey-Greenstein, com *jitter* por pixel para não criar
   faixas. É daí que vêm os feixes e boa parte da profundidade.
3. **Bloom anamórfico** — recorte de altas luzes, dois níveis de borrão
   separável com raio horizontal maior, como lente de cinema.
4. **God rays** — borrão radial a partir do foco de brasa, com a geometria
   ocluindo naturalmente.
5. **Composição** — profundidade de campo por disco de Poisson, aberração
   cromática crescendo para as bordas, tonemap fílmico ACES, vinheta, grão
   mais forte nas sombras e *dithering* contra banding.

A paleta é de concreto sujo e brasa, sem cor saturada chapada, e o tonemap
ACES entrega o rolloff nas altas em vez de estourar em branco.

## Desempenho

O seletor **Qualidade** tem três níveis (passos de marcha, de sombra, de
volumetria e reflexo) e um modo **automático**: mede o tempo de quadro e sobe
ou desce nível e resolução interna sozinho, para segurar os 60 fps. Em
automático ele começa conservador e sobe se couber.

## Como a sincronia funciona

Dois analisadores a partir da mesma fonte:

- **visual** (FFT 4096, alisado) → bandas grave/médio/agudo com ganho
  automático, alimentando brasa, refletores e densidade de poeira.
- **rítmico** (FFT 2048, sem alisamento) → batida por **fluxo espectral** nos
  graves (30–180 Hz), medido em **amplitude linear**, não em dB: em dB um
  chimbau fraco parece quase tão forte quanto um bumbo e dispara falso.
  Dispara quando passa de `média + 1,8 × desvio-padrão` da última ~0,7 s.

A análise roda em cadência própria (`setInterval` de 16 ms), **separada do
render**: se o FPS cair, o ritmo continua sendo lido ~60×/s e o corte não sai
do compasso.

Medido com uma faixa de teste de 120 BPM: intervalos de 494–515 ms, sem perda
e sem disparo falso nos chimbaus.

## Limites

Não é a Sphere — é uma tela só. Daqui dá pra seguir para luzes reais (WLED por
UDP, DMX/Art-Net), saída em segunda tela ou projetor, e troca de plano por
controlador MIDI.
