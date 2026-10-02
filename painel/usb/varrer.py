# -*- coding: utf-8 -*-
"""
Varredura: manda a imagem em tamanhos crescentes, com o numero BEM GRANDE
escrito nela, e voce so precisa olhar e lembrar do ULTIMO numero que apareceu.

Ja sabemos que 96 desenha maior que 64, e que 480x320 nao desenha.
O limite esta no meio. Esta varredura acha ele.

    python varrer.py                  64 ate 448
    python varrer.py --de 96 --ate 256
    python varrer.py --segundos 3

Antes de rodar: DESPLUGUE E PLUGUE o cabo, para a tela comecar limpa.
"""

import argparse
import io
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PIL import Image, ImageDraw

import deck

LIMITE_JPEG = 0xFFFF

TAMANHOS = [64, 80, 96, 112, 128, 144, 160, 192, 224, 256, 288, 320, 384, 448]

# uma cor por tamanho, para dar para ver a troca mesmo de longe
CORES = [
    (200, 40, 40), (210, 110, 30), (200, 180, 30), (60, 170, 70),
    (40, 150, 170), (50, 100, 210), (120, 70, 200), (200, 50, 150),
    (90, 90, 90), (190, 90, 90), (90, 190, 120), (90, 120, 190),
    (190, 160, 60), (160, 60, 160),
]


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


def placa(lado, cor):
    """Quadrado colorido com o numero gigante e borda branca."""
    im = Image.new("RGB", (lado, lado), cor)
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, lado - 1, lado - 1], outline=(255, 255, 255),
                width=max(2, lado // 24))
    f = fonte(int(lado * 0.52))
    txt = str(lado)
    try:
        a, b, c, e = d.textbbox((0, 0), txt, font=f)
        pos = ((lado - (c - a)) / 2 - a, (lado - (e - b)) / 2 - b)
    except AttributeError:
        pos = (lado * 0.12, lado * 0.22)
    d.text(pos, txt, font=f, fill=(255, 255, 255))
    return im


def codificar(im):
    im = im.transpose(deck._ROT270)
    for q in (90, 80, 70, 60, 50, 40, 30, 20):
        buf = io.BytesIO()
        im.convert("RGB").save(buf, format="JPEG", quality=q)
        dados = buf.getvalue()
        if len(dados) <= LIMITE_JPEG:
            return dados, q
    return None, None


def principal():
    p = argparse.ArgumentParser(description="Acha o maior tamanho que o aparelho desenha.")
    p.add_argument("--de", type=int, default=64)
    p.add_argument("--ate", type=int, default=448)
    p.add_argument("--segundos", type=float, default=2.5)
    p.add_argument("--tecla", type=int, default=1)
    args = p.parse_args()

    tamanhos = [t for t in TAMANHOS if args.de <= t <= args.ate]
    if not tamanhos:
        print("\n  Nenhum tamanho nessa faixa.\n")
        return 2

    print()
    print("  VARREDURA DE TAMANHO")
    print("  ====================")
    print(f"  Vou mandar {len(tamanhos)} tamanhos, {args.segundos}s cada:")
    print(f"    {', '.join(str(t) for t in tamanhos)}")
    print()
    print("  OLHE O APARELHO o tempo todo. Cada quadrado tem o numero")
    print("  escrito dentro e uma cor diferente.")
    print()
    print("  >>> ANOTE O ULTIMO NUMERO QUE APARECEU DIREITO. <<<")
    print("  Quando a tela parar de mudar, travar ou embranquecer, acabou:")
    print("  o numero anterior e o limite do aparelho.")
    print()
    try:
        input("  Enter para comecar... ")
    except (EOFError, KeyboardInterrupt):
        return 0

    try:
        d = deck.Deck().abrir()
    except Exception as e:
        print(f"\n  Nao consegui abrir o aparelho: {e}\n")
        return 1

    maior_enviado = None
    try:
        d.iniciar()
        d.brilho(100)
        d.limpar_tudo()
        time.sleep(0.3)

        for i, lado in enumerate(tamanhos):
            jpeg, q = codificar(placa(lado, CORES[i % len(CORES)]))
            if jpeg is None:
                print(f"    {lado:4d} -> nao cabe em 64 KB, pulando")
                continue
            print(f"    {lado:4d} x {lado:<4d} -> {len(jpeg):6d} bytes (qualidade {q})")
            try:
                d.limpar_tudo()
                d.definir_jpeg(args.tecla - 1, jpeg)
                d.aplicar()
                maior_enviado = lado
            except Exception as e:
                print(f"         o aparelho recusou: {e}")
                print(f"         -> o limite de envio e abaixo de {lado}")
                break
            time.sleep(args.segundos)

        print()
        print("  Fim da varredura.")
        print(f"  Enviado sem erro ate: {maior_enviado}")
        print()
        print("  >>> Qual foi o ULTIMO numero que voce VIU na tela? <<<")
        print("  (pode ser menor que o ultimo enviado: o aparelho aceita o")
        print("   comando mas para de desenhar acima de um certo tamanho)")
        print()
        input("  Enter para limpar a tela e sair... ")
    except KeyboardInterrupt:
        pass
    finally:
        try:
            d.limpar_tudo()
        except Exception:
            pass
        d.fechar()
    return 0


if __name__ == "__main__":
    sys.exit(principal())
