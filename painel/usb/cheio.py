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

def quadrar(im, lado):
    """Corta no centro e redimensiona para um quadrado de `lado`."""
    im = im.convert("RGB")
    larg, alt = im.size
    escala = max(lado / larg, lado / alt)
    nova = (max(1, int(larg * escala)), max(1, int(alt * escala)))
    im = im.resize(nova, deck._LANCZOS)
    x = (nova[0] - lado) // 2
    y = (nova[1] - lado) // 2
    return im.crop((x, y, x + lado, y + lado))


def _tentar(im, lado, max_bytes):
    """Melhor qualidade que cabe no orcamento, para um tamanho fixo."""
    quadro = quadrar(im, lado).transpose(deck._ROT270)
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


def jpeg_no_orcamento(im, lado, max_bytes, nitidez=35):
    """
    Escolhe o MAIOR tamanho que ainda cabe no orcamento com qualidade
    razoavel. Numa tela pequena, uma imagem de 256 com qualidade 40 fica
    melhor que uma de 384 com qualidade 13, toda esfarelada.

    `nitidez` e a qualidade JPEG minima aceitavel. Se nenhum tamanho
    alcancar isso, fica com o maior que couber, seja la com que qualidade.

    Devolve (dados, lado_usado, qualidade).
    """
    escada = [lado]
    passo = lado
    while passo > 96:
        passo = int(passo * 0.8)
        escada.append(passo)

    reserva = None
    for tentativa in escada:
        dados, q = _tentar(im, tentativa, max_bytes)
        if dados is None:
            continue
        if reserva is None:
            reserva = (dados, tentativa, q)   # o maior que coube, de todo jeito
        if q >= nitidez:
            return dados, tentativa, q
    if reserva:
        return reserva
    # ultimo recurso: bem pequeno, qualidade minima
    dados, q = _tentar(im, 96, max_bytes)
    return dados, 96, q


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
    p.add_argument("--lado", type=int, default=384)
    p.add_argument("--max-kb", type=float, default=6.0,
                   dest="max_kb", help="orcamento por imagem (padrao 6 KB)")
    p.add_argument("--fps", type=int, default=5)
    p.add_argument("--nitidez", type=int, default=35,
                   help="qualidade JPEG minima aceitavel (padrao 35)")
    p.add_argument("--segundos", type=float, default=6, help="tempo de cada imagem parada")
    p.add_argument("--brilho", type=int, default=100)
    p.add_argument("--tecla", type=int, default=1)
    args = p.parse_args()

    max_bytes = int(args.max_kb * 1024)
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
    print(f"  Imagem:    ate {args.lado}x{args.lado}")
    print(f"  Orcamento: {args.max_kb:.1f} KB por quadro")
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
                    dados, lado_usado, qual = jpeg_no_orcamento(q, args.lado, max_bytes,
                                                           args.nitidez)
                    if dados is None:
                        print(f"  ! {nome}: nao consegui caber no orcamento")
                        break
                    pacote.append(dados)
                if not pacote:
                    continue

                media = sum(len(x) for x in pacote) / len(pacote)
                print(f"  {nome}: {len(pacote)} quadro(s), "
                      f"{media/1024:.1f} KB cada, {lado_usado}x{lado_usado} q{qual}")

                inicio = time.time()
                i = 0
                while True:
                    try:
                        d.definir_jpeg(args.tecla - 1, pacote[i % len(pacote)])
                        d.aplicar()
                    except Exception as e:
                        print(f"    o aparelho recusou: {e}")
                        try:
                            d.reconectar()
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
