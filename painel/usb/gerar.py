# -*- coding: utf-8 -*-
"""
Gera imagens feitas sob medida para este painel: 320x480, dentro do
orcamento de 4 KB, ocupando a TELA INTEIRA.

Foto nao cabe inteira nesse orcamento, mas desenho sim: cor chapada,
gradiente e texto comprimem muito melhor. Entao da para ter os 100% da
tela com qualidade alta, o que a foto nao alcanca.

    python gerar.py --texto "ADASSA"
    python gerar.py --texto "GARDEN" --cor 1 --estilo gradiente
    python gerar.py --listar                 mostra os estilos e cores
    python gerar.py --texto "OI" --saida ../midia/aviso.jpg

Os arquivos saem na pasta midia, prontos para o cheio.py mostrar.
"""

import argparse
import io
import os
import sys

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PASTA_MIDIA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "midia")
# mesmo tamanho que o cheio.py manda ao aparelho: gerar maior e deixar
# ele reduzir desperdica qualidade na reamostragem
LARG, ALT = 256, 384
ORCAMENTO = 4 * 1024

PALETAS = [
    ((15, 20, 40), (60, 130, 240), "azul noite"),
    ((20, 45, 25), (90, 200, 110), "verde jardim"),
    ((50, 15, 15), (240, 120, 60), "laranja quente"),
    ((35, 15, 45), (190, 90, 220), "roxo"),
    ((10, 10, 12), (230, 230, 235), "preto e branco"),
    ((45, 35, 10), (240, 200, 70), "amarelo"),
    ((10, 35, 40), (70, 200, 200), "turquesa"),
]
ESTILOS = ("solido", "chapado", "liso", "gradiente", "diagonal", "listras", "moldura")


def fonte(tamanho, negrito=True):
    nomes = ("arialbd.ttf", "arial.ttf", "DejaVuSans-Bold.ttf", "DejaVuSans.ttf")
    for nome in nomes:
        try:
            return ImageFont.truetype(nome, tamanho)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=tamanho)
    except TypeError:
        return ImageFont.load_default()


def texturar(im, forca=5):
    """
    Ruido leve no fundo. A emenda que o aparelho deixa nas fronteiras de
    bloco so salta aos olhos em superficie lisa; com uma textura fraca ela
    se mistura e deixa de ser uma linha visivel.
    """
    if forca <= 0:
        return im
    import random as _r
    rnd = _r.Random(42)
    px = im.load()
    larg, alt = im.size
    for y in range(alt):
        for x in range(larg):
            r, g, b = px[x, y]
            d = rnd.randint(-forca, forca)
            px[x, y] = (max(0, min(255, r + d)),
                        max(0, min(255, g + d)),
                        max(0, min(255, b + d)))
    return im


def fundo(estilo, escura, clara, larg=LARG, alt=ALT):
    im = Image.new("RGB", (larg, alt), escura)
    d = ImageDraw.Draw(im)

    if estilo == "solido":
        # UMA cor so, do topo ao rodape. A foto no aparelho mostrou que a
        # area de cor chapada sai perfeitamente limpa: a emenda que ele
        # deixa nas fronteiras de bloco so aparece onde os blocos vizinhos
        # tem cores diferentes. Com uma cor so, nao ha vizinho diferente.
        im.paste(clara, (0, 0, larg, alt))

    elif estilo == "chapado":
        # duas faixas de cor solida, sem transicao: comprime muito bem e
        # por isso sai em qualidade alta, onde o aparelho quase nao erra.
        # A emenda que ele deixa nas fronteiras de bloco aparece muito mais
        # em gradiente comprimido com qualidade baixa.
        d.rectangle([0, 0, larg, int(alt * 0.62)], fill=clara)
        d.rectangle([0, int(alt * 0.62), larg, alt], fill=escura)

    elif estilo == "gradiente":
        for y in range(alt):
            t = y / max(1, alt - 1)
            d.line([0, y, larg, y], fill=tuple(
                int(escura[i] + (clara[i] - escura[i]) * t) for i in range(3)))

    elif estilo == "diagonal":
        for y in range(alt):
            t = y / max(1, alt - 1)
            d.line([0, y, larg, y], fill=tuple(
                int(escura[i] + (clara[i] - escura[i]) * t * 0.75) for i in range(3)))
        d.polygon([(0, alt), (larg, alt - alt // 3), (larg, alt), (0, alt)], fill=clara)

    elif estilo == "listras":
        passo = max(40, alt // 6)
        for i, y in enumerate(range(-alt, alt * 2, passo)):
            if i % 2 == 0:
                d.polygon([(0, y), (larg, y - larg), (larg, y - larg + passo),
                           (0, y + passo)], fill=clara)

    elif estilo == "moldura":
        m = max(10, min(larg, alt) // 16)
        d.rectangle([0, 0, larg - 1, m], fill=clara)
        d.rectangle([0, alt - m - 1, larg - 1, alt - 1], fill=clara)

    return im


def centralizar(d, texto, f, larg, y, cor):
    try:
        a, b, c, e = d.textbbox((0, 0), texto, font=f)
        x = (larg - (c - a)) / 2 - a
    except AttributeError:
        x = larg * 0.1
    d.text((x, y), texto, font=f, fill=cor)


def desenhar(texto, estilo, paleta, larg=LARG, alt=ALT, subtexto="", textura=5):
    escura, clara, _ = paleta
    im = fundo(estilo, escura, clara, larg, alt)
    im = texturar(im, textura)
    d = ImageDraw.Draw(im)

    if not texto:
        return im

    # No estilo chapado a letra e menor de proposito. Letra grande gasta
    # bytes em borda e derruba a qualidade do JPEG, e qualidade baixa e o
    # que faz o aparelho marcar as fronteiras de bloco. Medido em 256x384
    # dentro de 4 KB: letra de 80px da qualidade 38, de 44px da 64.
    tamanho = int(alt * 0.13) if estilo in ("chapado", "solido") else int(alt * 0.22)
    while tamanho > 12:
        f = fonte(tamanho)
        try:
            a, b, c, e = d.textbbox((0, 0), texto, font=f)
            if (c - a) <= larg * 0.88:
                break
        except AttributeError:
            break
        tamanho = int(tamanho * 0.92)
    f = fonte(tamanho)

    claro = sum(clara) < 330 if estilo == "solido" else (
        True if estilo == "chapado" else sum(escura) < 330)
    cor_txt = (255, 255, 255) if claro else (15, 15, 20)
    sombra = (0, 0, 0) if claro else (255, 255, 255)

    y = alt * 0.68 if estilo in ("chapado", "solido") else alt * 0.40

    # a sombra custa caro: no cartao de 256x384 dentro de 4 KB ela derruba a
    # qualidade de 73 para 47, e qualidade baixa e justamente o que faz o
    # aparelho marcar as fronteiras de bloco. No estilo chapado ela sai.
    if estilo not in ("chapado", "solido"):
        centralizar(d, texto, f, larg + 3, y + 3, sombra)
    centralizar(d, texto, f, larg, y, cor_txt)

    if subtexto:
        f2 = fonte(max(14, int(tamanho * 0.3)))
        centralizar(d, subtexto, f2, larg, y + tamanho * 1.15, cor_txt)
    return im


def _melhor_qualidade(im, orcamento):
    """Melhor qualidade JPEG que cabe no orcamento. Devolve (dados, q)."""
    baixo, alto, melhor, melhor_q = 8, 95, None, 0
    for _ in range(10):
        q = (baixo + alto) // 2
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=q, optimize=True)
        if len(buf.getvalue()) <= orcamento:
            melhor, melhor_q = buf.getvalue(), q
            baixo = q + 1
        else:
            alto = q - 1
        if baixo > alto:
            break
    return melhor, melhor_q


def salvar_no_orcamento(im, caminho, orcamento=ORCAMENTO):
    """Grava com a melhor qualidade que cabe no orcamento. Devolve (bytes, q)."""
    melhor, melhor_q = _melhor_qualidade(im, orcamento)
    if melhor is None:
        # nao coube no tamanho cheio: reduz o desenho ate caber
        for fator in (0.85, 0.72, 0.6):
            menor = im.resize((max(1, int(im.width * fator)),
                               max(1, int(im.height * fator))), Image.LANCZOS)
            dados, q = _melhor_qualidade(menor, orcamento)
            if dados:
                with open(caminho, "wb") as fp:
                    fp.write(dados)
                return len(dados), q
        return None, 0
    with open(caminho, "wb") as fp:
        fp.write(melhor)
    return len(melhor), melhor_q


def principal():
    p = argparse.ArgumentParser(description="Gera imagens sob medida para o painel.")
    p.add_argument("--texto", default="", help="texto grande no meio")
    p.add_argument("--subtexto", default="", help="linha menor embaixo do texto")
    p.add_argument("--estilo", default="solido", choices=ESTILOS)
    p.add_argument("--cor", type=int, default=0, help="numero da paleta (ver --listar)")
    p.add_argument("--saida", help="caminho do arquivo (padrao: pasta midia)")
    p.add_argument("--larg", type=int, default=LARG)
    p.add_argument("--alt", type=int, default=ALT)
    p.add_argument("--max-kb", type=float, default=4.0, dest="max_kb")
    p.add_argument("--textura", type=int, default=5,
                   help="ruido no fundo, que esconde as listras do aparelho "
                        "(padrao 5). 0 deixa o fundo liso")
    p.add_argument("--listar", action="store_true")
    args = p.parse_args()

    if args.listar:
        print("\n  ESTILOS: " + ", ".join(ESTILOS))
        print("\n  CORES:")
        for i, (_, _, nome) in enumerate(PALETAS):
            print(f"    --cor {i}  {nome}")
        print()
        return 0

    paleta = PALETAS[args.cor % len(PALETAS)]
    textura = 0 if args.estilo in ("chapado", "solido") else args.textura
    im = desenhar(args.texto, args.estilo, paleta, args.larg, args.alt,
                  args.subtexto, textura)

    if args.saida:
        saida = args.saida
    else:
        base = (args.texto or args.estilo).lower().replace(" ", "_")
        base = "".join(ch for ch in base if ch.isalnum() or ch == "_") or "imagem"
        os.makedirs(PASTA_MIDIA, exist_ok=True)
        saida = os.path.join(PASTA_MIDIA, f"{base}_{args.estilo}.jpg")

    tam, q = salvar_no_orcamento(im, saida, int(args.max_kb * 1024))
    if tam is None:
        print(f"\n  Nao coube em {args.max_kb} KB nem na qualidade minima.\n")
        return 1

    print()
    print(f"  Gerado: {saida}")
    print(f"  {args.larg}x{args.alt} | {tam} bytes ({tam/1024:.1f} KB) | qualidade {q}")
    print(f"  Cabe no orcamento e ocupa a TELA INTEIRA.")
    print()
    print("  Para ver no aparelho:  python cheio.py")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(principal())
