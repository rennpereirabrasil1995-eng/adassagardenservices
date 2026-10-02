# -*- coding: utf-8 -*-
"""
Teste do aparelho: acende as 6 teclas com numeros coloridos.

Se isso funcionar, o protocolo esta certo e o resto e detalhe.

    python testar.py
    python testar.py --brilho 60
    python testar.py --apagar        (so limpa a tela)
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    print("\n  Falta a biblioteca Pillow. Rode:\n    pip install hidapi pillow\n")
    sys.exit(1)

try:
    import deck
except ImportError:
    import os as _os
    _alvo = _os.path.join(_os.path.expanduser("~"),
                          "adassagardenservices-ccr-c658d0db-inlhu7", "painel", "usb")
    print()
    print("  Nao achei o deck.py (o driver do aparelho).")
    print()
    print("  Voce esta rodando em:")
    print("    " + _os.path.dirname(_os.path.abspath(__file__)))
    print()
    print("  Mas precisa rodar na pasta do projeto onde esta o deck.py.")
    print("  Cole as duas linhas:")
    print()
    print('    cd "' + _alvo + '"')
    print("    python " + _os.path.basename(__file__))
    print()
    raise SystemExit(1)

CORES = [
    (220, 50, 50), (235, 140, 30), (225, 200, 40),
    (60, 180, 75), (50, 130, 230), (150, 80, 200),
]


def fonte(tamanho):
    for nome in ("arial.ttf", "DejaVuSans-Bold.ttf", "Arial.ttf"):
        try:
            return ImageFont.truetype(nome, tamanho)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=tamanho)
    except TypeError:          # Pillow antigo
        return ImageFont.load_default()


def tecla_numerada(numero, cor, lado=128):
    """Desenha um quadrado colorido com o numero no meio."""
    im = Image.new("RGB", (lado, lado), cor)
    d = ImageDraw.Draw(im)
    f = fonte(int(lado * 0.6))
    txt = str(numero)
    try:
        cx, cy, dx, dy = d.textbbox((0, 0), txt, font=f)
        larg, alt = dx - cx, dy - cy
        pos = ((lado - larg) / 2 - cx, (lado - alt) / 2 - cy)
    except AttributeError:
        pos = (lado * 0.3, lado * 0.2)
    d.text(pos, txt, font=f, fill=(255, 255, 255))
    d.rectangle([0, 0, lado - 1, lado - 1], outline=(255, 255, 255), width=3)
    return im


def principal():
    p = argparse.ArgumentParser(description="Teste do macro pad.")
    p.add_argument("--brilho", type=int, default=100)
    p.add_argument("--apagar", action="store_true", help="so limpa a tela e sai")
    args = p.parse_args()

    print()
    print("  TESTE DO APARELHO")
    print("  =================")

    try:
        achados = deck.Deck.listar()
    except deck.ErroDeck as e:
        print(f"\n  {e}\n")
        return 1

    if not achados:
        print(f"\n  Aparelho {deck.VID:04x}:{deck.PID:04x} nao encontrado.")
        print("  Confira o cabo e feche o programa do fabricante.\n")
        return 1

    print(f"  Interfaces encontradas: {len(achados)}")
    for a in achados:
        print(f"    usage_page={a.get('usage_page')} usage={a.get('usage')} "
              f"-> {a.get('product_string') or '(sem nome)'}")

    try:
        d = deck.Deck().abrir()
    except Exception as e:
        print(f"\n  Nao consegui abrir o aparelho: {e}")
        print("  Quase sempre e o programa do fabricante segurando o aparelho.")
        print("  Feche ele (inclusive da bandeja do relogio) e tente de novo.\n")
        return 1

    try:
        print("\n  Conectado. Limpando a tela...")
        d.reiniciar(args.brilho)

        if args.apagar:
            print("  Tela apagada.\n")
            return 0

        print("  Mandando as 6 teclas...")
        for i in range(deck.TECLAS_COM_TELA):
            d.definir_imagem(i, tecla_numerada(i + 1, CORES[i % len(CORES)]))
        d.aplicar()

        print()
        print("  >>> OLHE O APARELHO. <<<")
        print("  As 6 teclas devem estar com numeros de 1 a 6, cada uma de uma cor.")
        print()
        print("  Se os numeros aparecerem deitados ou de cabeca para baixo, me avise:")
        print("  e so acertar o giro no arquivo deck.py.")
        print()
        print("  Ctrl+C para sair (a tela fica como esta).")
        try:
            while True:
                time.sleep(30)
                d.manter_vivo()
        except KeyboardInterrupt:
            print("\n  Saindo.\n")
    finally:
        d.fechar()
    return 0


if __name__ == "__main__":
    sys.exit(principal())
