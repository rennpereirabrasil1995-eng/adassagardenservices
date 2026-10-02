# -*- coding: utf-8 -*-
"""
Acerta a PROPORCAO da imagem com a do painel.

A imagem quadrada desenha certo, mas sobra tela dos lados, porque o painel
nao e quadrado. Este programa manda um retangulo liso, com borda grossa e
cantos marcados, no tamanho que voce pedir. Voce vai ajustando ate a borda
encostar nas quatro beiradas da tela.

A imagem e lisa de proposito: fica leve (uns 2 KB) e nunca passa do limite
do aparelho, que trava acima de uns 6 KB.

    python ajustar.py --larg 480 --alt 320
    python ajustar.py --larg 400 --alt 300
    python ajustar.py --serie            testa varias proporcoes comuns

Quando a borda encostar nos 4 lados, me diga os numeros.
"""

import argparse
import io
import os
import sys
import time

from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import deck
except ImportError:
    import os as _os
    _alvo = _os.path.join(_os.path.expanduser("~"),
                          "adassagardenservices-ccr-c658d0db-inlhu7", "painel", "usb")
    print()
    print("  Nao achei o deck.py (o driver do aparelho).")
    print('    cd "' + _alvo + '"')
    print("    python " + _os.path.basename(__file__))
    print()
    raise SystemExit(1)

# proporcoes comuns em LCD pequeno
SERIE = [
    (320, 480), (300, 400), (272, 480), (384, 384),
]


def alvo(larg, alt):
    """
    Alvo LEVE: fundo liso com quatro cantos coloridos e marcas no meio
    de cada lado. Sem borda continua e sem texto grande, porque linha
    de alto contraste atravessando a imagem custa milhares de bytes no
    JPEG, e o orcamento do aparelho e de uns 6 KB.

    Se os quatro cantos coloridos estiverem encostados nas quinas da
    tela, a proporcao esta certa.
    """
    im = Image.new("RGB", (larg, alt), (20, 40, 90))
    d = ImageDraw.Draw(im)

    # cantos: cada um de uma cor, bem grandes para enxergar de longe
    c = max(26, min(larg, alt) // 5)
    for x0, y0, cor in (
        (0, 0, (255, 60, 60)),                 # vermelho = cima-esquerda
        (larg - c, 0, (255, 220, 40)),         # amarelo  = cima-direita
        (0, alt - c, (60, 140, 255)),          # azul     = baixo-esquerda
        (larg - c, alt - c, (120, 255, 90)),   # verde    = baixo-direita
    ):
        d.rectangle([x0, y0, x0 + c - 1, y0 + c - 1], fill=cor)

    # marca no meio de cada lado, para ver se cortou pelo meio
    m = max(10, c // 3)
    d.rectangle([larg // 2 - m, 0, larg // 2 + m, m], fill=(255, 255, 255))
    d.rectangle([larg // 2 - m, alt - m, larg // 2 + m, alt - 1], fill=(255, 255, 255))
    d.rectangle([0, alt // 2 - m, m, alt // 2 + m], fill=(255, 255, 255))
    d.rectangle([larg - m, alt // 2 - m, larg - 1, alt // 2 + m], fill=(255, 255, 255))
    return im


def codificar(im, max_bytes=6 * 1024):
    girada = im.transpose(deck._ROT270)
    for q in (92, 85, 75, 65, 55, 45, 35, 25, 15):
        buf = io.BytesIO()
        girada.save(buf, format="JPEG", quality=q)
        dados = buf.getvalue()
        if len(dados) <= max_bytes:
            return dados, q
    return None, 0


def principal():
    p = argparse.ArgumentParser(description="Acerta a proporcao com a do painel.")
    p.add_argument("--larg", type=int, default=480)
    p.add_argument("--alt", type=int, default=320)
    p.add_argument("--serie", action="store_true", help="testa varias proporcoes")
    p.add_argument("--segundos", type=float, default=5.0)
    p.add_argument("--max-kb", type=float, default=5.0, dest="max_kb")
    p.add_argument("--tecla", type=int, default=1)
    args = p.parse_args()

    max_bytes = int(args.max_kb * 1024)
    pares = SERIE if args.serie else [(args.larg, args.alt)]

    print()
    print("  AJUSTE DE PROPORCAO")
    print("  ===================")
    print("  A borda branca tem que encostar nos 4 lados da tela.")
    print("  Cantos: vermelho = cima-esquerda, amarelo = cima-direita,")
    print("          azul = baixo-esquerda,    verde = baixo-direita.")
    print()

    try:
        d = deck.Deck().abrir()
    except Exception as e:
        print(f"  Nao consegui abrir o aparelho: {e}\n")
        return 1

    try:
        d.iniciar()
        d.brilho(100)
        for larg, alt in pares:
            dados, q = codificar(alvo(larg, alt), max_bytes)
            if dados is None:
                print(f"    {larg}x{alt}: nao coube no orcamento, pulando")
                continue
            print(f"    {larg}x{alt} -> {len(dados)} bytes (qualidade {q})")
            try:
                d.definir_jpeg(args.tecla - 1, dados)
                d.aplicar()
            except Exception as e:
                print(f"      o aparelho recusou: {e}")
                try:
                    d.reconectar()
                    print("      reconectado.")
                except Exception:
                    print("      Despluga e pluga o cabo.\n")
                    return 1
                continue
            if len(pares) > 1:
                time.sleep(args.segundos)

        print()
        if len(pares) > 1:
            print("  >>> Qual proporcao encostou melhor nos 4 lados? <<<")
        else:
            print("  >>> A borda encostou nos 4 lados? <<<")
            print("      sobra do lado direito  -> aumente --larg")
            print("      sobra em baixo         -> aumente --alt")
            print("      cortou a direita       -> diminua --larg")
            print("      cortou em baixo        -> diminua --alt")
        print()
        input("  Enter para sair (a imagem fica na tela)... ")
    finally:
        d.fechar()
    return 0


if __name__ == "__main__":
    sys.exit(principal())
