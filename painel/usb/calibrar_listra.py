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
        # o separador NAO pode cair na fronteira de bloco, senao se mistura
        # com o efeito que estamos medindo: desenha duas linhas abaixo
        if i:
            d.line([0, y0 + 2, larg, y0 + 2], fill=(25, 25, 25))
            d.line([0, y0 + 3, larg, y0 + 3], fill=(25, 25, 25))
        d.text((8, y0 + 8), str(v), font=f, fill=(255, 255, 255))

    return im, altura


def aplicar(im, valores, altura, linhas=None, forca=25):
    """
    Escurece uma linha dentro de cada bloco.

    Sem `linhas`, cada faixa usa uma INTENSIDADE diferente, sempre na
    primeira linha do bloco. Com `linhas`, todas usam a mesma intensidade
    e cada faixa escurece uma LINHA diferente dentro do bloco - serve para
    descobrir em qual delas o aparelho deixa a emenda.
    """
    px = im.load()
    larg, alt = im.size
    n = len(linhas if linhas else valores)
    for y in range(alt):
        faixa = min(n - 1, y // altura)
        if linhas:
            if y % BLOCO != linhas[faixa] % BLOCO:
                continue
            v = forca
        else:
            if y % BLOCO != 0:
                continue
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
    p.add_argument("--valores", type=int, nargs="+", default=[0, 6, 12, 18, 24],
                   help="intensidades a testar (modo padrao)")
    p.add_argument("--linhas", action="store_true",
                   help="em vez das intensidades, varre QUAL linha do bloco "
                        "compensar: 0, 4, 8, 12 e 15")
    p.add_argument("--forca", type=int, default=25,
                   help="intensidade usada no modo --linhas")
    p.add_argument("--negativo", action="store_true", help="clareia em vez de escurecer")
    p.add_argument("--max-kb", type=float, default=4.0, dest="max_kb")
    p.add_argument("--atraso", type=float, default=5)
    p.add_argument("--tecla", type=int, default=1)
    args = p.parse_args()

    linhas = [0, 4, 8, 12, 15] if args.linhas else None
    rotulos = linhas if linhas else (
        [-v for v in args.valores] if args.negativo else args.valores)
    valores = rotulos

    im, altura = cartao(rotulos)
    im = aplicar(im, valores, altura, linhas, args.forca)
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
    if linhas:
        print(f"  Modo LINHA: cada faixa escurece uma linha diferente do bloco")
        print(f"  Linhas testadas, de cima para baixo: {linhas}")
        print(f"  Intensidade fixa: {args.forca}")
    else:
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
