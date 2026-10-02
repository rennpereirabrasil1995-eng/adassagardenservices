# -*- coding: utf-8 -*-
"""
Acha de uma vez o valor de compensacao que apaga as listras.

Em vez de testar um valor por vez e comparar fotos, manda UMA imagem
dividida em faixas horizontais, cada uma com uma compensacao diferente e
o numero escrito nela. Voce olha e diz qual faixa ficou mais lisa.

    python calibrar_listra.py
    python calibrar_listra.py --valores 0 10 20 30 40
    python calibrar_listra.py --negativo      testa clarear em vez de escurecer
"""

import argparse
import io
import os
import sys

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import deck
except ImportError:
    print("\n  Rode dentro da pasta painel/usb, onde esta o deck.py.\n")
    raise SystemExit(1)

LARG, ALT = 256, 384
BLOCO = 16


def fonte(t):
    for n in ("arialbd.ttf", "arial.ttf", "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(n, t)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=t)
    except TypeError:
        return ImageFont.load_default()


def cartao(valores, larg=LARG, alt=ALT, base=(110, 150, 110)):
    """
    Faixas horizontais de cinza-esverdeado, cada uma com uma compensacao
    diferente aplicada nas fronteiras de bloco. Tom medio e liso de
    proposito: e onde a listra mais aparece.
    """
    im = Image.new("RGB", (larg, alt), base)
    d = ImageDraw.Draw(im)
    f = fonte(26)

    n = len(valores)
    # cada faixa comeca numa fronteira de bloco, para o efeito ficar limpo
    altura = (alt // n // BLOCO) * BLOCO
    if altura < BLOCO * 2:
        altura = BLOCO * 2

    for i, v in enumerate(valores):
        y0 = i * altura
        y1 = min(alt, y0 + altura)
        if y0 >= alt:
            break
        # separador fino entre as faixas
        d.line([0, y0, larg, y0], fill=(30, 30, 30))
        d.text((8, y0 + 6), str(v), font=f, fill=(255, 255, 255))

    return im, altura


def aplicar(im, valores, altura):
    """Escurece a linha de cada fronteira de bloco, com a forca da faixa."""
    px = im.load()
    larg, alt = im.size
    for y in range(0, alt, BLOCO):
        faixa = min(len(valores) - 1, y // altura)
        v = valores[faixa]
        if not v:
            continue
        for x in range(larg):
            r, g, b = px[x, y]
            px[x, y] = (max(0, min(255, r - v)),
                        max(0, min(255, g - v)),
                        max(0, min(255, b - v)))
    return im


def principal():
    p = argparse.ArgumentParser(description="Acha a compensacao que apaga as listras.")
    p.add_argument("--valores", type=int, nargs="+", default=[0, 15, 30, 45, 60])
    p.add_argument("--negativo", action="store_true", help="clareia em vez de escurecer")
    p.add_argument("--max-kb", type=float, default=4.0, dest="max_kb")
    p.add_argument("--atraso", type=float, default=5)
    p.add_argument("--tecla", type=int, default=1)
    args = p.parse_args()

    valores = [-v for v in args.valores] if args.negativo else args.valores

    im, altura = cartao(valores)
    im = aplicar(im, valores, altura)
    girada = im.transpose(deck._ROT270)

    dados = None
    for q in (92, 85, 75, 65, 55, 45, 35, 25, 15):
        buf = io.BytesIO()
        girada.save(buf, format="JPEG", quality=q, optimize=True, subsampling=2)
        if len(buf.getvalue()) <= int(args.max_kb * 1024):
            dados, qual = buf.getvalue(), q
            break
    if dados is None:
        print("\n  Nao coube no orcamento.\n")
        return 1

    print()
    print("  CALIBRACAO DA LISTRA")
    print("  ====================")
    print(f"  Faixas, de cima para baixo: {valores}")
    print(f"  Cada faixa tem {altura} pixels de altura")
    print(f"  {len(dados)} bytes, qualidade {qual}")
    print()

    try:
        d = deck.Deck().abrir()
    except Exception as e:
        print(f"  Nao consegui abrir o aparelho: {e}\n")
        return 1

    try:
        d.atraso_pacote = args.atraso / 1000.0
        d.iniciar()
        d.brilho(100)
        d.limpar_tudo()
        d.definir_jpeg(args.tecla - 1, dados)
        d.aplicar()
        print("  Mandado. OLHE O APARELHO.")
        print()
        print("  Cada faixa tem o numero da compensacao escrito nela.")
        print("  >>> Qual faixa ficou MAIS LISA, com menos listra? <<<")
        print()
        print("  Se a faixa 0 ja for a mais lisa, escurecer nao ajuda:")
        print("  rode de novo com --negativo para testar clarear.")
        print()
        input("  Enter para sair (a imagem fica na tela)... ")
    finally:
        d.fechar()
    return 0


if __name__ == "__main__":
    sys.exit(principal())
