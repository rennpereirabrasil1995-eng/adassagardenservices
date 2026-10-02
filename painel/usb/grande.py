# -*- coding: utf-8 -*-
"""
Manda UMA imagem grande para o aparelho, para descobrir o limite da tela.

Descoberta: o firmware desenha o JPEG no tamanho que receber - nao fixa em
64x64. Entao da para usar mais area do LCD. Falta saber ate onde.

    python grande.py --lado 160           quadrado de 160
    python grande.py --larg 480 --alt 320 retangulo
    python grande.py --larg 480 --alt 320 --tecla 1
    python grande.py --lado 200 --sem-giro

A imagem tem borda, marcas nos 4 cantos e o tamanho escrito no meio: assim
da para ver se ela foi cortada e onde ela foi ancorada.

Limite do protocolo: o JPEG tem que caber em 65535 bytes, porque o comando
so tem 2 bytes para o tamanho. O programa baixa a qualidade sozinho ate caber.
"""

import argparse
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PIL import Image, ImageDraw

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

LIMITE_JPEG = 0xFFFF  # 65535 bytes: o cabecalho BAT so tem 2 bytes de tamanho


def desenhar_alvo(larg, alt):
    """Imagem com borda, cantos marcados e o tamanho escrito no meio."""
    im = Image.new("RGB", (larg, alt), (15, 15, 25))
    d = ImageDraw.Draw(im)

    # xadrez de fundo, para enxergar escala e corte
    passo = max(16, min(larg, alt) // 8)
    for y in range(0, alt, passo):
        for x in range(0, larg, passo):
            if (x // passo + y // passo) % 2 == 0:
                d.rectangle([x, y, x + passo - 1, y + passo - 1], fill=(35, 35, 55))

    # borda
    d.rectangle([0, 0, larg - 1, alt - 1], outline=(0, 255, 180), width=max(2, min(larg, alt) // 40))

    # cantos: cada um de uma cor, para saber qual canto sumiu se cortar
    c = max(10, min(larg, alt) // 7)
    for (x0, y0, cor) in (
        (0, 0, (255, 60, 60)),             # vermelho  = superior esquerdo
        (larg - c, 0, (255, 220, 40)),     # amarelo   = superior direito
        (0, alt - c, (60, 140, 255)),      # azul      = inferior esquerdo
        (larg - c, alt - c, (120, 255, 90)),  # verde  = inferior direito
    ):
        d.rectangle([x0, y0, x0 + c - 1, y0 + c - 1], fill=cor)

    # cruz no centro
    d.line([larg // 2, 0, larg // 2, alt], fill=(0, 255, 180), width=1)
    d.line([0, alt // 2, larg, alt // 2], fill=(0, 255, 180), width=1)

    try:
        from testar import fonte
        f = fonte(max(14, min(larg, alt) // 6))
        txt = f"{larg}x{alt}"
        d.text((larg * 0.18, alt * 0.42), txt, font=f, fill=(255, 255, 255))
    except Exception:
        pass
    return im


def codificar(im, girar=True):
    """JPEG que cabe no limite de 65535 bytes, baixando a qualidade se precisar."""
    if girar:
        im = im.transpose(deck._ROT270)
    for q in (90, 80, 70, 60, 50, 40, 30, 20, 12):
        buf = io.BytesIO()
        im.convert("RGB").save(buf, format="JPEG", quality=q)
        dados = buf.getvalue()
        if len(dados) <= LIMITE_JPEG:
            return dados, q
    return None, None


def principal():
    p = argparse.ArgumentParser(description="Manda uma imagem grande para o aparelho.")
    p.add_argument("--lado", type=int, help="imagem quadrada deste lado")
    p.add_argument("--larg", type=int, help="largura")
    p.add_argument("--alt", type=int, help="altura")
    p.add_argument("--tecla", type=int, default=1, help="tecla destino, 1 a 9 (padrao 1)")
    p.add_argument("--sem-giro", action="store_true", help="nao gira 90 graus")
    p.add_argument("--limpar-antes", action="store_true", default=True)
    args = p.parse_args()

    if args.lado:
        larg = alt = args.lado
    elif args.larg and args.alt:
        larg, alt = args.larg, args.alt
    else:
        print("\n  Diga o tamanho:  --lado 160   ou   --larg 480 --alt 320\n")
        return 2

    im = desenhar_alvo(larg, alt)
    jpeg, qualidade = codificar(im, girar=not args.sem_giro)
    if jpeg is None:
        print(f"\n  {larg}x{alt} nao cabe em {LIMITE_JPEG} bytes nem na qualidade minima.")
        print("  Esse e o teto do protocolo. Tente um tamanho menor.\n")
        return 1

    print()
    print(f"  Imagem:    {larg}x{alt}")
    print(f"  Giro:      {'nao' if args.sem_giro else '90 graus'}")
    print(f"  JPEG:      {len(jpeg)} bytes (qualidade {qualidade})"
          f"  [limite {LIMITE_JPEG}]")
    print(f"  Tecla:     {args.tecla}")

    try:
        d = deck.Deck().abrir()
    except Exception as e:
        print(f"\n  Nao consegui abrir o aparelho: {e}\n")
        return 1

    try:
        d.iniciar()
        if args.limpar_antes:
            d.limpar_tudo()
        d.brilho(100)
        d.definir_jpeg(args.tecla - 1, jpeg)
        d.aplicar()
        print()
        print("  Mandado. OLHE O APARELHO.")
        print()
        print("  Me diga:")
        print("    - a imagem apareceu inteira, com os 4 cantos coloridos?")
        print("      (vermelho em cima-esquerda, amarelo cima-direita,")
        print("       azul baixo-esquerda, verde baixo-direita)")
        print("    - ela cobre quanto da tela? um quadradinho, metade, tudo?")
        print("    - em que canto da tela ela comeca?")
        print()
        input("  Enter para limpar a tela e sair... ")
    finally:
        try:
            d.limpar_tudo()
        except Exception:
            pass
        d.fechar()
    return 0


if __name__ == "__main__":
    sys.exit(principal())
