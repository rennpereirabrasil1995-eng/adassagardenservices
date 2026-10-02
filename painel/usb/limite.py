# -*- coding: utf-8 -*-
"""
Acha o limite de PESO do JPEG que o aparelho aceita desenhar.

Descoberta: 384x384 com 12 KB desenhou; 384x384 com 29 KB nao desenhou.
Entao o que trava nao e o tamanho em pixels, e o peso do arquivo.

Manda a MESMA imagem de 384x384 em pesos decrescentes, cada uma com um
DIGITO GIGANTE (1, 2, 3...). Voce so precisa dizer quais digitos apareceram.

    python limite.py
    python limite.py --lado 320
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
    print()
    print("  Cole as duas linhas:")
    print('    cd "' + _alvo + '"')
    print("    python " + _os.path.basename(__file__))
    print()
    raise SystemExit(1)


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


def imagem(lado, digito, cor, ruido):
    """
    Quadrado colorido com um digito gigante.
    `ruido` controla o peso: quanto mais detalhe, mais pesado fica o JPEG.
    """
    im = Image.new("RGB", (lado, lado), cor)
    d = ImageDraw.Draw(im)
    if ruido:
        passo = max(2, lado // ruido)
        for y in range(0, lado, passo):
            for x in range(0, lado, passo):
                if (x // passo + y // passo) % 2 == 0:
                    d.rectangle([x, y, x + passo - 1, y + passo - 1],
                                fill=(cor[0] // 2, cor[1] // 2, cor[2] // 2))
    d.rectangle([0, 0, lado - 1, lado - 1], outline=(255, 255, 255), width=6)
    f = fonte(int(lado * 0.6))
    d.text((lado * 0.33, lado * 0.15), str(digito), font=f, fill=(255, 255, 255))
    return im


def codificar(im, q):
    buf = io.BytesIO()
    im.transpose(deck._ROT270).convert("RGB").save(buf, format="JPEG", quality=q)
    return buf.getvalue()


def mirar(lado, digito, cor, alvo_bytes):
    """
    Devolve um JPEG o mais perto possivel (por baixo) de `alvo_bytes`,
    buscando a qualidade por bisseccao. Assim a escada de pesos fica limpa.
    """
    melhor = None
    baixo, alto = 5, 95
    for _ in range(12):
        q = (baixo + alto) // 2
        dados = codificar(imagem(lado, digito, cor, 40), q)
        if len(dados) <= alvo_bytes:
            melhor = dados
            baixo = q + 1
        else:
            alto = q - 1
        if baixo > alto:
            break
    if melhor is None:  # nem na qualidade minima cabe: tira o ruido
        melhor = codificar(imagem(lado, digito, cor, 0), 30)
    return melhor


def principal():

    p = argparse.ArgumentParser(description="Acha o limite de peso do JPEG.")
    p.add_argument("--lado", type=int, default=384)
    p.add_argument("--segundos", type=float, default=2.5)
    p.add_argument("--tecla", type=int, default=1)
    args = p.parse_args()

    # alvos de peso em bytes, do mais pesado ao mais leve
    alvos = [30000, 24000, 20000, 17000, 15000, 13000, 11000, 9000]
    cores = [(200, 50, 50), (210, 120, 30), (200, 180, 40), (60, 180, 80),
             (40, 150, 180), (60, 100, 220), (140, 70, 200), (110, 110, 110)]

    tentativas = []
    for i, alvo in enumerate(alvos):
        dados = mirar(args.lado, i + 1, cores[i], alvo)
        tentativas.append((i + 1, dados))

    print()
    print("  LIMITE DE PESO DO JPEG")
    print("  ======================")
    print(f"  Todas as imagens sao {args.lado}x{args.lado}. So o peso muda.")
    print()
    for dig, dados in tentativas:
        print(f"    digito {dig}  ->  {len(dados):6d} bytes  ({len(dados)/1024:5.1f} KB)")
    print()
    print(f"  Vou mandar as 8, {args.segundos}s cada, do mais PESADO ao mais LEVE.")
    print()
    print("  >>> ANOTE QUAIS DIGITOS APARECERAM NA TELA. <<<")
    print("  Provavelmente os primeiros nao aparecem e, a partir de certo")
    print("  ponto, comecam a aparecer. Esse ponto e o limite do aparelho.")
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

    try:
        d.iniciar()
        d.brilho(100)
        for dig, dados in tentativas:
            print(f"    mandando o {dig} ({len(dados)} bytes)...")
            try:
                d.limpar_tudo()
                d.definir_jpeg(args.tecla - 1, dados)
                d.aplicar()
            except Exception as e:
                print(f"      recusou: {e}")
            time.sleep(args.segundos)

        print()
        print("  Fim.")
        print("  >>> Quais digitos voce viu? (ex: 'do 5 em diante') <<<")
        print()
        input("  Enter para sair... ")
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
