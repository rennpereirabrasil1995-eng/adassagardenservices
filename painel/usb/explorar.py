# -*- coding: utf-8 -*-
"""
Explorador: tenta descobrir se da para usar MAIS do que os 6 quadradinhos.

O protocolo conhecido so sabe mandar "a imagem da tecla N". Mas tres coisas
nunca foram documentadas, e cada uma pode abrir mais area de tela:

  A) teclas alem da 6  - o aparelho diz ter 9 teclas. As 3 extras desenham
                         em algum lugar da tela?
  B) imagem maior      - e se o JPEG for de 200x200 em vez de 64x64? O
                         firmware corta, estica, ou desenha maior?
  C) comando MOD       - troca o "modo" do aparelho. Ninguem sabe o que faz.
                         Pode mudar o leiaute da tela.

Rode e vá olhando o aparelho. Nada aqui grava nada: no maximo a tela fica
estranha, e ai e so despluga e pluga de novo.

    python explorar.py
    python explorar.py --so A
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PIL import Image, ImageDraw

import deck

try:
    from testar import fonte
except ImportError:
    def fonte(t):
        from PIL import ImageFont
        return ImageFont.load_default()


def placa(texto, cor, lado=128):
    im = Image.new("RGB", (lado, lado), cor)
    d = ImageDraw.Draw(im)
    f = fonte(int(lado * 0.45))
    d.text((lado * 0.12, lado * 0.25), str(texto), font=f, fill=(255, 255, 255))
    d.rectangle([0, 0, lado - 1, lado - 1], outline=(255, 255, 255), width=4)
    return im


def pausa(msg):
    try:
        input(f"  {msg} ")
    except (EOFError, KeyboardInterrupt):
        raise SystemExit(0)


# --------------------------------------------------------------------------- #

def teste_a(d):
    print()
    print("  TESTE A - as 9 teclas")
    print("  ---------------------")
    print("  Vou acender UMA tecla de cada vez, da 1 ate a 9.")
    print("  As 6 primeiras voce ja conhece. Olhe se as teclas 7, 8 e 9")
    print("  desenham em algum canto novo da tela.")
    pausa("Enter para comecar...")

    for i in range(9):
        d.limpar_tudo()
        d.definir_imagem(i, placa(i + 1, (200, 30, 30)))
        d.aplicar()
        print(f"    tecla {i + 1} ... (olhe a tela)")
        time.sleep(1.8)

    d.limpar_tudo()
    print()
    print("  >>> As teclas 7, 8 e 9 apareceram em algum lugar? <<<")
    pausa("Enter para continuar...")


def teste_b(d):
    print()
    print("  TESTE B - imagem maior que 64x64")
    print("  --------------------------------")
    print("  Vou mandar a MESMA imagem na tecla 1, em tamanhos crescentes.")
    print("  Se o quadradinho crescer na tela, da para usar mais area!")
    print("  Se ficar igual, borrado ou picotado, o firmware fixa em 64.")
    pausa("Enter para comecar...")

    for lado in (64, 96, 128, 200, 256):
        d.limpar_tudo()
        im = placa(lado, (30, 120, 200), lado=max(128, lado))
        # manda sem reduzir para 64: e esse o ponto do teste
        import io
        im = im.convert("RGB").resize((lado, lado), deck._LANCZOS)
        im = im.transpose(deck._ROT270)
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=90)
        jpeg = buf.getvalue()
        print(f"    mandando {lado}x{lado} ({len(jpeg)} bytes)...")
        try:
            d.definir_jpeg(0, jpeg)
            d.aplicar()
        except Exception as e:
            print(f"      recusou: {e}")
        time.sleep(2.5)

    d.limpar_tudo()
    print()
    print("  >>> O quadrado mudou de tamanho na tela? Em qual deles? <<<")
    pausa("Enter para continuar...")


def teste_c(d):
    print()
    print("  TESTE C - comando MOD (modo do aparelho)")
    print("  ----------------------------------------")
    print("  Vou trocar o modo de 0 a 9 e redesenhar as 6 teclas.")
    print("  Olhe se o leiaute da tela muda: quadrados maiores, em outro")
    print("  lugar, tela inteira, qualquer coisa diferente.")
    pausa("Enter para comecar...")

    for modo in range(10):
        print(f"    modo {modo} ...")
        try:
            d._comando(deck.CRT, 0x00, 0x00, b"MOD", 0x00, 0x00, 0x30 + modo)
            time.sleep(0.4)
            for i in range(6):
                d.definir_imagem(i, placa(i + 1, (40, 150, 60)))
            d.aplicar()
        except Exception as e:
            print(f"      erro: {e}")
        time.sleep(2.0)

    print("    voltando para o modo 0...")
    try:
        d._comando(deck.CRT, 0x00, 0x00, b"MOD", 0x00, 0x00, 0x30)
        d.limpar_tudo()
    except Exception:
        pass
    print()
    print("  >>> Algum modo mudou o leiaute? Qual? <<<")
    pausa("Enter para terminar...")


def principal():
    p = argparse.ArgumentParser(description="Explora o que mais da para desenhar.")
    p.add_argument("--so", choices=["A", "B", "C"], help="roda so um dos testes")
    args = p.parse_args()

    print()
    print("  EXPLORADOR DO APARELHO")
    print("  ======================")
    print("  Objetivo: achar um jeito de usar mais area da tela.")

    try:
        d = deck.Deck().abrir()
    except Exception as e:
        print(f"\n  Nao consegui abrir o aparelho: {e}")
        print("  Feche o programa do fabricante e tente de novo.\n")
        return 1

    try:
        d.reiniciar(100)
        testes = {"A": teste_a, "B": teste_b, "C": teste_c}
        for letra in ([args.so] if args.so else ["A", "B", "C"]):
            testes[letra](d)
        d.limpar_tudo()
        print()
        print("  Fim. Me conte o que aconteceu em cada teste.")
        print("  Se nenhum mudou nada, os 6 quadradinhos sao o limite do aparelho.")
        print()
    except SystemExit:
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
