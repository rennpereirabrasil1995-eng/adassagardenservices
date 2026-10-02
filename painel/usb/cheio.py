# -*- coding: utf-8 -*-
"""
TELA CHEIA: mostra imagem, GIF ou video ocupando o painel inteiro.

Como funciona: em vez de mandar 6 quadradinhos de 64x64, manda UMA imagem
grande (384x384 por padrao) para a primeira tecla. O firmware desenha ela
no tamanho recebido, cobrindo quase todo o LCD.

O limite nao e o tamanho em pixels, e o PESO do arquivo: acima de uns 13 KB
o aparelho trava. Por isso tudo aqui respeita um orcamento de bytes, e a
qualidade do JPEG e reduzida automaticamente ate caber. Se mesmo assim uma
escrita falhar, o programa reconecta e baixa o orcamento sozinho.

    python cheio.py                      passa tudo que esta na pasta midia
    python cheio.py --arquivo foto.jpg   um arquivo so
    python cheio.py --fps 6              mais quadros por segundo no video
    python cheio.py --lado 320           imagem menor (mais leve, mais rapida)
    python cheio.py --max-kb 12          orcamento maior (mais nitido, mais risco)
"""

import argparse
import io
import os
import sys
import time

from PIL import Image, ImageSequence

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import deck
except ImportError:
    import os as _os
    _alvo = _os.path.join(_os.path.expanduser("~"),
                          "adassagardenservices-ccr-c658d0db-inlhu7", "painel", "usb")
    print()
    print("  Nao achei o deck.py (o driver do aparelho).")
    print()
    print("  Cole as duas linhas:")
    print('    cd "' + _alvo + '"')
    print("    python " + _os.path.basename(__file__))
    print()
    raise SystemExit(1)

try:
    import cv2
except ImportError:
    cv2 = None

PASTA_PADRAO = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "midia")
EXT_IMAGEM = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".avif", ".gif"}
EXT_VIDEO = {".mp4", ".webm", ".m4v", ".ogv", ".mov", ".mkv", ".avi"}


# --------------------------------------------------------------------------- #

def enquadrar(im, larg_alvo, alt_alvo):
    """Corta no centro e redimensiona para larg_alvo x alt_alvo."""
    im = im.convert("RGB")
    larg, alt = im.size
    escala = max(larg_alvo / larg, alt_alvo / alt)
    nova = (max(1, int(larg * escala)), max(1, int(alt * escala)))
    im = im.resize(nova, deck._LANCZOS)
    x = (nova[0] - larg_alvo) // 2
    y = (nova[1] - alt_alvo) // 2
    return im.crop((x, y, x + larg_alvo, y + alt_alvo))


OTIMIZAR = True   # trocado pelo --sem-otimizar
MARCADORES = 0    # trocado pelo --marcadores; 0 = nenhum
SUBAMOSTRAGEM = 2 # 2 = 4:2:0 (blocos de 16x16), 0 = 4:4:4 (blocos de 8x8)
COMPENSAR = 0     # trocado pelo --compensar
#
# A subamostragem muda o TAMANHO do bloco do JPEG. Se as listras mudarem
# de espacamento ao trocar isso, elas estao mesmo nas fronteiras de bloco.
#
# Marcador de reinicio e um ponto de sincronia que o JPEG carrega de
# tantas em tantas linhas de blocos. Decodificador simples que perca o
# passo volta a acertar no marcador seguinte, em vez de arrastar o erro
# pelo resto da imagem - que e a cara das listras horizontais.


def compensar_blocos(im, bloco, intensidade):
    """
    O decodificador do aparelho clareia uma linha a cada linha de blocos,
    o que aparece como listras regulares. Aqui a mesma linha e escurecida
    de proposito, para que uma coisa cancele a outra.

    `intensidade` positiva escurece, negativa clareia. A imagem ja vem
    girada, entao as linhas aqui sao as mesmas que aparecem na tela.
    """
    if not intensidade:
        return im
    px = im.load()
    larg, alt = im.size
    for y in range(0, alt, bloco):
        for x in range(larg):
            r, g, b = px[x, y]
            px[x, y] = (max(0, min(255, r - intensidade)),
                        max(0, min(255, g - intensidade)),
                        max(0, min(255, b - intensidade)))
    return im


def _tentar(im, larg, alt, max_bytes):
    """Melhor qualidade que cabe no orcamento, para um tamanho fixo."""
    quadro = enquadrar(im, larg, alt).transpose(deck._ROT270)
    if COMPENSAR:
        quadro = compensar_blocos(quadro.copy(), 16 if SUBAMOSTRAGEM else 8,
                                  COMPENSAR)
    # desce ate 1: numa tela deste tamanho, encher o painel costuma
    # valer mais que a nitidez, e o --nitidez existe para quem discordar
    baixo, alto, melhor, melhor_q = 1, 95, None, 0
    for _ in range(10):
        q = (baixo + alto) // 2
        buf = io.BytesIO()
        extra = {"restart_marker_rows": MARCADORES} if MARCADORES else {}
        extra["subsampling"] = SUBAMOSTRAGEM
        quadro.save(buf, format="JPEG", quality=q, optimize=OTIMIZAR, **extra)
        dados = buf.getvalue()
        if len(dados) <= max_bytes:
            melhor, melhor_q = dados, q
            baixo = q + 1
        else:
            alto = q - 1
        if baixo > alto:
            break
    return melhor, melhor_q


def jpeg_no_orcamento(im, larg, alt, max_bytes, nitidez=35):
    """
    Escolhe o MAIOR tamanho que ainda cabe no orcamento com qualidade
    razoavel, mantendo a proporcao pedida. Numa tela pequena, 256 com
    qualidade 40 fica melhor que 384 toda esfarelada em qualidade 13.

    Devolve (dados, larg_usada, alt_usada, qualidade).
    """
    escada = [(larg, alt)]
    fator = 1.0
    while min(larg * fator, alt * fator) > 96:
        fator *= 0.8
        escada.append((max(1, int(larg * fator)), max(1, int(alt * fator))))

    reserva = None
    for w, h in escada:
        dados, q = _tentar(im, w, h, max_bytes)
        if dados is None:
            continue
        if reserva is None:
            reserva = (dados, w, h, q)
        if q >= nitidez:
            return dados, w, h, q
    if reserva:
        return reserva
    w = max(64, int(larg * 0.25))
    h = max(64, int(alt * 0.25))
    dados, q = _tentar(im, w, h, max_bytes)
    return dados, w, h, q


def carregar_quadros(caminho, fps):
    """Lista de PIL.Image da midia, ja na taxa pedida."""
    ext = os.path.splitext(caminho)[1].lower()

    if ext in EXT_VIDEO:
        if cv2 is None:
            print(f"  ! {os.path.basename(caminho)}: video precisa do opencv")
            print("    pip install opencv-python")
            return []
        cap = cv2.VideoCapture(caminho)
        if not cap.isOpened():
            return []
        fps_video = cap.get(cv2.CAP_PROP_FPS) or fps
        passo = max(1, round(fps_video / fps))
        quadros, i = [], 0
        while len(quadros) < 600:
            ok, bgr = cap.read()
            if not ok:
                break
            if i % passo == 0:
                quadros.append(Image.fromarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)))
            i += 1
        cap.release()
        return quadros

    try:
        im = Image.open(caminho)
    except Exception as e:
        print(f"  ! {os.path.basename(caminho)}: {e}")
        return []

    if getattr(im, "is_animated", False):
        quadros = []
        for q in ImageSequence.Iterator(im):
            repete = max(1, round((q.info.get("duration", 100) or 100) / 1000 * fps))
            quadros.extend([q.convert("RGB")] * repete)
            if len(quadros) >= 600:
                break
        return quadros[:600]
    return [im.convert("RGB")]


# --------------------------------------------------------------------------- #

def principal():
    p = argparse.ArgumentParser(description="Imagem e video em tela cheia no aparelho.")
    p.add_argument("--arquivo", help="um arquivo so (senao passa a pasta inteira)")
    p.add_argument("--midia", default=PASTA_PADRAO)
    p.add_argument("--lado", type=int, help="tamanho quadrado (atalho para --larg e --alt)")
    p.add_argument("--larg", type=int, default=256,
                   help="largura (padrao 256: 320 trava ao trocar de imagem)")
    p.add_argument("--alt", type=int, default=384,
                   help="altura (padrao 384)")
    p.add_argument("--max-kb", type=float, default=4.0,
                   dest="max_kb", help="orcamento por imagem (padrao 4 KB: e o peso que roda indefinidamente com fotos de verdade)")
    p.add_argument("--fps", type=int, default=5)
    p.add_argument("--nitidez", type=int, default=1,
                   help="qualidade JPEG minima (padrao 1: enche a tela). "
                        "Subir deixa mais nitido porem menor")
    p.add_argument("--segundos", type=float, default=6, help="tempo de cada imagem parada")
    p.add_argument("--atraso", type=float, default=5,
                   help="milissegundos entre os pedacos da imagem (padrao 5); "
                        "suba para 15 se a imagem chegar rasgada")
    p.add_argument("--pausa", type=float, default=0.3,
                   help="descanso entre envios, em segundos (padrao 0.2)")
    p.add_argument("--reconectar", action="store_true",
                   help="fecha e reabre o aparelho antes de cada imagem; "
                        "mais lento, porem e o que aguenta trocar imagem grande")
    p.add_argument("--compensar", type=int, default=12,
                   help="escurece a linha de cada fronteira de bloco para "
                        "cancelar a listra (padrao 12). Negativo clareia")
    p.add_argument("--blocos", type=int, default=16, choices=[8, 16],
                   help="tamanho do bloco do JPEG: 16 (padrao) ou 8. "
                        "Se as listras mudarem de espacamento, sao de bloco")
    p.add_argument("--marcadores", type=int, default=0,
                   help="marcadores de reinicio a cada N linhas de blocos. "
                        "Tente 1 ou 2 se a imagem vier com listras")
    p.add_argument("--sem-otimizar", action="store_true", dest="sem_otimizar",
                   help="grava o JPEG com a tabela de Huffman PADRAO. "
                        "Gasta mais bytes, mas firmware simples costuma "
                        "entender melhor")
    p.add_argument("--sem-limpar", action="store_true", dest="sem_limpar",
                   help="nao limpa a tela antes de cada imagem")
    p.add_argument("--brilho", type=int, default=100)
    p.add_argument("--tecla", type=int, default=1)
    args = p.parse_args()

    global OTIMIZAR, MARCADORES
    OTIMIZAR = not args.sem_otimizar
    MARCADORES = max(0, args.marcadores)
    global SUBAMOSTRAGEM
    SUBAMOSTRAGEM = 2 if args.blocos == 16 else 0
    global COMPENSAR
    COMPENSAR = args.compensar
    max_bytes = int(args.max_kb * 1024)
    larg_alvo = args.lado or args.larg
    alt_alvo = args.lado or args.alt
    pasta = os.path.abspath(args.midia)

    if args.arquivo:
        caminhos = [args.arquivo if os.path.isabs(args.arquivo)
                    else os.path.join(pasta, args.arquivo)]
    else:
        if not os.path.isdir(pasta):
            print(f"\n  Pasta nao encontrada: {pasta}\n")
            return 1
        caminhos = [
            os.path.join(pasta, n) for n in sorted(os.listdir(pasta))
            if not n.startswith((".", "_"))
            and os.path.splitext(n)[1].lower() in (EXT_IMAGEM | EXT_VIDEO)
            and os.path.isfile(os.path.join(pasta, n))
        ]

    caminhos = [c for c in caminhos if os.path.isfile(c)]
    if not caminhos:
        print(f"\n  Nenhuma imagem ou video em {pasta}\n")
        print("  Ponha os arquivos la, ou mande pelo celular com o iniciar.bat.\n")
        return 1

    print()
    print("  TELA CHEIA")
    print("  ==========")
    print(f"  Imagem:    ate {larg_alvo}x{alt_alvo}")
    print(f"  Orcamento: {args.max_kb:.1f} KB por quadro")
    print(f"  Nitidez:   minimo {args.nitidez} "
          f"(baixo = enche a tela, alto = mais nitido porem menor)")
    print(f"  Arquivos:  {len(caminhos)}")
    print()
    print("  Se travar ou a imagem chegar rasgada, aumente o atraso entre os")
    print("  pedacos e baixe o orcamento:  --atraso 15 --max-kb 4")
    print()

    try:
        d = deck.Deck().abrir()
    except Exception as e:
        print(f"  Nao consegui abrir o aparelho: {e}")
        print("  Feche o programa do fabricante e tente de novo.\n")
        return 1

    d.atraso_pacote = args.atraso / 1000.0
    print(f"  Atraso entre pedacos: {args.atraso} ms")
    print(f"  Tabela JPEG: {'padrao' if args.sem_otimizar else 'otimizada'}")
    print(f"  Marcadores de reinicio: {args.marcadores or 'nenhum'}")
    print(f"  Bloco do JPEG: {args.blocos}x{args.blocos}")
    print(f"  Compensacao de listra: {args.compensar or 'nenhuma'}")
    print("  Conectado. Ctrl+C para parar.\n")
    preparados = {}   # caminho+orcamento -> lista de jpegs ja prontos
    try:
        d.reiniciar(args.brilho)
        while True:
            for caminho in caminhos:
                nome = os.path.basename(caminho)
                quadros = carregar_quadros(caminho, args.fps)
                if not quadros:
                    continue
                animada = len(quadros) > 1

                chave = (caminho, max_bytes, larg_alvo, alt_alvo, args.nitidez)
                if chave in preparados:
                    pacote, w_us, h_us, qual = preparados[chave]
                    print(f"  {nome}: {len(pacote)} quadro(s) (ja preparado), "
                          f"{w_us}x{h_us} q{qual}")
                    inicio = time.time()
                    i = 0
                    while True:
                        try:
                            if args.reconectar and i > 0:
                                d.reconectar(espera=1.2)
                            if not args.sem_limpar:
                                d.limpar_tudo()
                            d.definir_jpeg(args.tecla - 1, pacote[i % len(pacote)])
                            d.aplicar()
                            if args.pausa > 0:
                                time.sleep(args.pausa)
                        except Exception as e:
                            print(f"    o aparelho recusou: {e}")
                            try:
                                d.reconectar(espera=3.0)
                                max_bytes = int(max_bytes * 0.8)
                                preparados.clear()
                                print(f"    reconectado; baixando o orcamento para "
                                      f"{max_bytes/1024:.1f} KB")
                            except Exception:
                                print("    nao voltou. Despluga e pluga o cabo.\n")
                                return 1
                            break
                        i += 1
                        if animada:
                            if i >= len(pacote) and len(caminhos) > 1:
                                break
                            time.sleep(1.0 / args.fps)
                        else:
                            time.sleep(args.segundos)
                            break
                    continue

                pacote = []
                for q in quadros:
                    dados, w_us, h_us, qual = jpeg_no_orcamento(
                        q, larg_alvo, alt_alvo, max_bytes, args.nitidez)
                    if dados is None:
                        print(f"  ! {nome}: nao consegui caber no orcamento")
                        break
                    pacote.append(dados)
                if not pacote:
                    continue

                preparados[chave] = (pacote, w_us, h_us, qual)
                media = sum(len(x) for x in pacote) / len(pacote)
                print(f"  {nome}: {len(pacote)} quadro(s), "
                      f"{media/1024:.1f} KB cada, {w_us}x{h_us} q{qual}")

                inicio = time.time()
                i = 0
                while True:
                    try:
                        if args.reconectar and (i > 0 or caminho != caminhos[0]):
                            d.reconectar(espera=1.2)
                        if not args.sem_limpar:
                            d.limpar_tudo()
                        d.definir_jpeg(args.tecla - 1, pacote[i % len(pacote)])
                        d.aplicar()
                        if args.pausa > 0:
                            time.sleep(args.pausa)
                    except Exception as e:
                        print(f"    o aparelho recusou: {e}")
                        try:
                            d.reconectar(espera=3.0)
                            max_bytes = int(max_bytes * 0.8)
                            print(f"    reconectado; baixando o orcamento para "
                                  f"{max_bytes/1024:.1f} KB")
                        except Exception:
                            print("    nao voltou. Despluga e pluga o cabo.\n")
                            return 1
                        break
                    i += 1
                    if animada:
                        if i >= len(pacote) and len(caminhos) > 1:
                            break
                        time.sleep(1.0 / args.fps)
                    else:
                        time.sleep(args.segundos)
                        break
    except KeyboardInterrupt:
        print("\n  Parando...")
    finally:
        try:
            d.limpar_tudo()
        except Exception:
            pass
        d.fechar()
    return 0


if __name__ == "__main__":
    sys.exit(principal())
