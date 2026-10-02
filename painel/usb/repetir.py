# -*- coding: utf-8 -*-
"""
Diagnostico: manda a MESMA imagem varias vezes seguidas e conta quantas
o aparelho aceita antes de travar.

Serve para separar duas coisas que estavam confundidas:
  - o aparelho trava por causa do TAMANHO de uma imagem?
  - ou por causa da SEQUENCIA de varias imagens grandes?

    python repetir.py                 10 envios de 5.5 KB, limpando antes
    python repetir.py --sem-limpar    sem limpar entre os envios
    python repetir.py --kb 4 --vezes 20
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
    print("\n  Rode dentro da pasta painel/usb, onde esta o deck.py.\n")
    raise SystemExit(1)


def imagem(n, larg, alt):
    cores = [(200,60,60),(220,140,40),(220,200,50),(70,180,90),
             (60,170,190),(70,110,220),(150,80,210),(200,70,160)]
    im = Image.new("RGB", (larg, alt), cores[n % len(cores)])
    d = ImageDraw.Draw(im)
    c = max(30, min(larg, alt) // 4)
    d.rectangle([larg//2 - c, alt//2 - c, larg//2 + c, alt//2 + c], fill=(255,255,255))
    return im


def codificar(im, max_bytes):
    g = im.transpose(deck._ROT270)
    for q in (92, 80, 70, 60, 50, 40, 30, 20, 10):
        b = io.BytesIO(); g.save(b, "JPEG", quality=q)
        if len(b.getvalue()) <= max_bytes:
            return b.getvalue()
    return None


def principal():
    p = argparse.ArgumentParser()
    p.add_argument("--kb", type=float, default=5.5)
    p.add_argument("--vezes", type=int, default=10)
    p.add_argument("--larg", type=int, default=320)
    p.add_argument("--alt", type=int, default=480)
    p.add_argument("--pausa", type=float, default=0.3)
    p.add_argument("--sem-limpar", action="store_true", dest="sem_limpar")
    p.add_argument("--reconectar", action="store_true",
                   help="fecha e reabre o aparelho antes de cada envio")
    args = p.parse_args()

    max_bytes = int(args.kb * 1024)
    print()
    print("  QUANTAS IMAGENS SEGUIDAS O APARELHO AGUENTA")
    print("  ===========================================")
    print(f"  {args.vezes} envios de {args.larg}x{args.alt}, ate {args.kb} KB cada")
    print(f"  limpando antes de cada um: {'nao' if args.sem_limpar else 'sim'}")
    print(f"  pausa entre eles: {args.pausa}s")
    print(f"  reconectando antes de cada um: {'sim' if args.reconectar else 'nao'}")
    print()

    try:
        d = deck.Deck().abrir()
    except Exception as e:
        print(f"  Nao consegui abrir: {e}\n")
        return 1

    aguentou = 0
    try:
        d.iniciar(); d.brilho(100)
        for i in range(args.vezes):
            dados = codificar(imagem(i, args.larg, args.alt), max_bytes)
            try:
                if args.reconectar and i > 0:
                    d.reconectar(espera=1.2)
                if not args.sem_limpar:
                    d.limpar_tudo()
                d.definir_jpeg(0, dados)
                d.aplicar()
                aguentou += 1
                print(f"    {i+1:2d}: ok ({len(dados)} bytes)")
            except Exception as e:
                print(f"    {i+1:2d}: TRAVOU ({len(dados)} bytes) - {e}")
                break
            time.sleep(args.pausa)
        print()
        print(f"  >>> Aguentou {aguentou} de {args.vezes} envios. <<<")
        print("  Olhe a tela: as cores mudaram a cada envio?")
        print()
        input("  Enter para sair... ")
    finally:
        d.fechar()
    return 0


if __name__ == "__main__":
    sys.exit(principal())
