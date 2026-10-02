# -*- coding: utf-8 -*-
"""
Regua: manda uma imagem com as coordenadas escritas de 32 em 32 pixels,
para medir EXATAMENTE que pedaco da tela o aparelho usa.

Voce tira uma foto e eu leio ate que numero aparece em cada direcao.
Esse e o tamanho util do LCD.

    python mapear.py                 384x384
    python mapear.py --larg 320 --alt 400
    python mapear.py --passo 24
"""

import argparse
import io
import os
import sys

from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import deck
except ImportError:
    import os as _os
    print()
    print("  Nao achei o arquivo deck.py (o driver do aparelho).")
    print()
    print("  Rode dentro da pasta  painel/usb  do projeto:")
    print('    cd "%USERPROFILE%/adassagardenservices-ccr-c658d0db-inlhu7/painel/usb"')
    print("    python " + _os.path.basename(__file__))
    print()
    raise SystemExit(1)

LIMITE_JPEG = 0xFFFF


def fonte(tamanho):
    from PIL import ImageFont
    for nome in ("arialbd.ttf", "arial.ttf", "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(nome, tamanho)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=tamanho)
    except TypeError:
        return ImageFont.load_default()


def regua(larg, alt, passo=32):
    """
    Fundo escuro com linhas de grade numeradas.
    Faixa VERMELHA em cima (y=0) e AZUL na esquerda (x=0), para saber
    qual borda e qual depois do giro.
    """
    im = Image.new("RGB", (larg, alt), (12, 12, 20))
    d = ImageDraw.Draw(im)
    f = fonte(15)

    # quadriculado leve
    for x in range(0, larg, passo):
        d.line([x, 0, x, alt], fill=(45, 45, 70), width=1)
    for y in range(0, alt, passo):
        d.line([0, y, larg, y], fill=(45, 45, 70), width=1)

    # numeros do eixo X, em amarelo, na parte de cima
    for x in range(0, larg, passo * 2):
        d.line([x, 0, x, 18], fill=(255, 220, 40), width=2)
        d.text((x + 3, 20), str(x), font=f, fill=(255, 220, 40))

    # numeros do eixo Y, em verde, na esquerda
    for y in range(0, alt, passo * 2):
        d.line([0, y, 18, y], fill=(120, 255, 90), width=2)
        d.text((22, y + 2), str(y), font=f, fill=(120, 255, 90))

    # bordas de referencia
    d.rectangle([0, 0, larg - 1, 10], fill=(220, 40, 40))        # VERMELHA = topo
    d.rectangle([0, 0, 10, alt - 1], fill=(50, 110, 240))        # AZUL = esquerda
    d.rectangle([larg - 11, 0, larg - 1, alt - 1], fill=(255, 220, 40))   # AMARELA = direita
    d.rectangle([0, alt - 11, larg - 1, alt - 1], fill=(120, 255, 90))    # VERDE = baixo

    f2 = fonte(28)
    d.text((larg * 0.3, alt * 0.45), f"{larg}x{alt}", font=f2, fill=(255, 255, 255))
    return im


def codificar(im, girar=True):
    if girar:
        im = im.transpose(deck._ROT270)
    for q in (92, 85, 75, 65, 55, 45, 35, 25):
        buf = io.BytesIO()
        im.convert("RGB").save(buf, format="JPEG", quality=q)
        dados = buf.getvalue()
        if len(dados) <= LIMITE_JPEG:
            return dados, q
    return None, None


def principal():
    p = argparse.ArgumentParser(description="Mede a area util da tela.")
    p.add_argument("--larg", type=int, default=384)
    p.add_argument("--alt", type=int, default=384)
    p.add_argument("--passo", type=int, default=32)
    p.add_argument("--tecla", type=int, default=1)
    p.add_argument("--sem-giro", action="store_true")
    args = p.parse_args()

    im = regua(args.larg, args.alt, args.passo)
    jpeg, q = codificar(im, girar=not args.sem_giro)
    if jpeg is None:
        print("\n  Nao cabe em 64 KB nem na qualidade minima.\n")
        return 1

    print()
    print(f"  Regua de {args.larg}x{args.alt}, linhas de {args.passo} em {args.passo}")
    print(f"  JPEG: {len(jpeg)} bytes (qualidade {q})")
    print()
    print("  Bordas coloridas, para saber a orientacao depois do giro:")
    print("    VERMELHA = topo       AZUL  = esquerda")
    print("    AMARELA  = direita    VERDE = baixo")

    try:
        d = deck.Deck().abrir()
    except Exception as e:
        print(f"\n  Nao consegui abrir o aparelho: {e}\n")
        return 1

    try:
        d.iniciar()
        d.limpar_tudo()
        d.brilho(100)
        d.definir_jpeg(args.tecla - 1, jpeg)
        d.aplicar()
        print()
        print("  Mandado.")
        print()
        print("  >>> TIRE UMA FOTO DE PERTO, bem enquadrada na tela. <<<")
        print()
        print("  Preciso ler:")
        print("    - ate que numero AMARELO aparece (largura util)")
        print("    - ate que numero VERDE aparece (altura util)")
        print("    - quais das 4 bordas coloridas estao visiveis")
        print()
        input("  Enter para sair (a imagem fica na tela)... ")
    finally:
        d.fechar()
    return 0


if __name__ == "__main__":
    sys.exit(principal())
