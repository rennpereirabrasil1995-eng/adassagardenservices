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


def _tentar(im, larg, alt, max_bytes):
    """Melhor qualidade que cabe no orcamento, para um tamanho fixo."""
    quadro = enquadrar(im, larg, alt).transpose(deck._ROT270)
    baixo, alto, melhor, melhor_q = 8, 95, None, 0
    for _ in range(10):
        q = (baixo + alto) // 2
        buf = io.BytesIO()
        quadro.save(buf, format="JPEG", quality=q)
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
    p.add_argument("--larg", type=int, default=320, help="largura (padrao 320)")
    p.add_argument("--alt", type=int, default=480, help="altura (padrao 480)")
    p.add_argument("--max-kb", type=float, default=5.0,
                   dest="max_kb", help="orcamento por imagem (padrao 5 KB: aguentou 10 trocas seguidas sem travar)")
    p.add_argument("--fps", type=int, default=5)
    p.add_argument("--nitidez", type=int, default=1,
                   help="qualidade JPEG minima (padrao 1: enche a tela). "
                        "Subir deixa mais nitido porem menor")
    p.add_argument("--segundos", type=float, default=6, help="tempo de cada imagem parada")
    p.add_argument("--atraso", type=float, default=0,
                   help="milissegundos entre os pedacos da imagem; "
                        "suba para 3 ou 5 se a imagem chegar rasgada")
    p.add_argument("--pausa", type=float, default=0.3,
                   help="descanso entre envios, em segundos (padrao 0.2)")
    p.add_argument("--reconectar", action="store_true",
                   help="fecha e reabre o aparelho antes de cada imagem; "
                        "mais lento, porem e o que aguenta trocar imagem grande")
    p.add_argument("--sem-limpar", action="store_true", dest="sem_limpar",
                   help="nao limpa a tela antes de cada imagem")
    p.add_argument("--brilho", type=int, default=100)
    p.add_argument("--tecla", type=int, default=1)
    args = p.parse_args()

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
    print("  Se o aparelho travar, despluga e pluga, e rode de novo com")
    print("  um orcamento menor:  --max-kb 8")
    print()

    try:
        d = deck.Deck().abrir()
    except Exception as e:
        print(f"  Nao consegui abrir o aparelho: {e}")
        print("  Feche o programa do fabricante e tente de novo.\n")
        return 1

    d.atraso_pacote = args.atraso / 1000.0
    print(f"  Atraso entre pedacos: {args.atraso} ms")
    print("  Conectado. Ctrl+C para parar.\n")
    try:
        d.reiniciar(args.brilho)
        while True:
            for caminho in caminhos:
                nome = os.path.basename(caminho)
                quadros = carregar_quadros(caminho, args.fps)
                if not quadros:
                    continue
                animada = len(quadros) > 1

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
