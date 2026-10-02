# -*- coding: utf-8 -*-
"""
Programa principal: mostra as imagens e videos do painel nas teclas do aparelho.

Le a MESMA pasta de midia e o MESMO _config.json que o controle no celular
escreve, entao voce continua mandando os arquivos e escolhendo as teclas pelo
navegador - so quem desenha mudou: em vez do monitor, as 6 teclas do pad.

    python rodar.py
    python rodar.py --fps 12 --brilho 80
    python rodar.py --midia C:\\caminho\\da\\pasta

Modos (escolhidos no controle, em "Modo da tela"):
    grade     cada tecla com a sua propria imagem/GIF/video
    cheio     uma midia so, espalhada pelas 6 teclas
    calibrar  numeros de 1 a 6, para conferir a ordem das teclas
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from PIL import Image, ImageDraw, ImageFont, ImageSequence
except ImportError:
    print("\n  Falta a biblioteca Pillow. Rode:\n    pip install hidapi pillow\n")
    sys.exit(1)

try:
    import deck
except ImportError:
    import os as _os
    print()
    print("  Nao achei o arquivo deck.py (o driver do aparelho).")
    print()
    print("  Este programa precisa rodar NA MESMA PASTA que o deck.py,")
    print("  ou seja, dentro de  painel/usb  do projeto. Faca assim:")
    print()
    print('    cd "%USERPROFILE%/adassagardenservices-ccr-c658d0db-inlhu7/painel/usb"')
    print("    python " + _os.path.basename(__file__))
    print()
    raise SystemExit(1)

try:
    import cv2  # opcional, so para MP4
except ImportError:
    cv2 = None

PASTA_PADRAO = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "midia")
NOME_CONFIG = "_config.json"
LADO = 128          # desenha em 128 e o driver reduz para 64 com qualidade
MAX_QUADROS = 600   # teto por midia, para nao estourar a memoria

EXT_IMAGEM = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".avif"}
EXT_GIF = {".gif", ".webp"}
EXT_VIDEO = {".mp4", ".webm", ".m4v", ".ogv", ".mov", ".mkv", ".avi"}


# --------------------------------------------------------------------------- #
# leitura da midia
# --------------------------------------------------------------------------- #

def carregar_quadros(caminho: str, fps: int) -> list:
    """Devolve a lista de quadros (PIL.Image) de uma midia, ja na taxa pedida."""
    ext = os.path.splitext(caminho)[1].lower()

    if ext in EXT_VIDEO:
        return _quadros_video(caminho, fps)

    try:
        im = Image.open(caminho)
    except Exception as e:
        print(f"  ! nao consegui abrir {os.path.basename(caminho)}: {e}")
        return []

    # GIF (ou WEBP animado): respeita a duracao de cada quadro
    if getattr(im, "is_animated", False):
        quadros = []
        for q in ImageSequence.Iterator(im):
            dur_ms = q.info.get("duration", 100) or 100
            repete = max(1, round(dur_ms / 1000 * fps))
            quadro = q.convert("RGB")
            quadros.extend([quadro] * repete)
            if len(quadros) >= MAX_QUADROS:
                break
        return quadros[:MAX_QUADROS] or [im.convert("RGB")]

    return [im.convert("RGB")]


def _quadros_video(caminho: str, fps: int) -> list:
    if cv2 is None:
        print(f"  ! {os.path.basename(caminho)} e video, e falta o opencv.")
        print("    Para tocar video nas teclas:  pip install opencv-python")
        return []
    cap = cv2.VideoCapture(caminho)
    if not cap.isOpened():
        print(f"  ! nao consegui abrir o video {os.path.basename(caminho)}")
        return []
    fps_video = cap.get(cv2.CAP_PROP_FPS) or fps
    passo = max(1, round(fps_video / fps))
    quadros, i = [], 0
    while len(quadros) < MAX_QUADROS:
        ok, bgr = cap.read()
        if not ok:
            break
        if i % passo == 0:
            rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
            quadros.append(Image.fromarray(rgb))
        i += 1
    cap.release()
    return quadros


# --------------------------------------------------------------------------- #
# desenho
# --------------------------------------------------------------------------- #

def enquadrar(im, ajuste="cover", zoom=1.0, dx=0, dy=0, lado=LADO):
    """Deixa a imagem quadrada, aplicando enquadramento, zoom e deslocamento."""
    im = im.convert("RGB")
    larg, alt = im.size
    if ajuste == "contain":
        escala = min(lado / larg, lado / alt)
    else:
        escala = max(lado / larg, lado / alt)
    escala *= max(0.05, float(zoom or 1))

    nova = (max(1, int(larg * escala)), max(1, int(alt * escala)))
    red = im.resize(nova, deck._LANCZOS)

    tela = Image.new("RGB", (lado, lado), (0, 0, 0))
    pos = ((lado - nova[0]) // 2 + int(dx), (lado - nova[1]) // 2 + int(dy))
    tela.paste(red, pos)
    return tela


def fonte(tamanho):
    for nome in ("arial.ttf", "DejaVuSans-Bold.ttf", "Arial.ttf"):
        try:
            return ImageFont.truetype(nome, tamanho)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=tamanho)
    except TypeError:
        return ImageFont.load_default()


def tecla_numero(n, lado=LADO):
    im = Image.new("RGB", (lado, lado), (10, 30, 45))
    d = ImageDraw.Draw(im)
    f = fonte(int(lado * 0.55))
    txt = str(n)
    try:
        a, b, c, e = d.textbbox((0, 0), txt, font=f)
        pos = ((lado - (c - a)) / 2 - a, (lado - (e - b)) / 2 - b)
    except AttributeError:
        pos = (lado * 0.3, lado * 0.2)
    d.text(pos, txt, font=f, fill=(0, 229, 255))
    d.rectangle([0, 0, lado - 1, lado - 1], outline=(0, 229, 255), width=4)
    return im


def tecla_vazia(lado=LADO):
    return Image.new("RGB", (lado, lado), (0, 0, 0))


# --------------------------------------------------------------------------- #
# montagem do que vai aparecer
# --------------------------------------------------------------------------- #

def montar_linha_do_tempo(cfg: dict, pasta: str, fps: int) -> list:
    """
    Devolve uma lista de quadros. Cada quadro e uma lista de 6 JPEGs,
    um por tecla. Cada quadro dura 1/fps segundo.
    """
    n = deck.TECLAS_COM_TELA
    modo = cfg.get("modo", "grade")
    vazio = deck.preparar_jpeg(tecla_vazia())

    if modo == "calibrar":
        return [[deck.preparar_jpeg(tecla_numero(i + 1)) for i in range(n)]]

    if modo == "cheio":
        return _linha_cheio(cfg, pasta, fps, n, vazio)

    return _linha_grade(cfg, pasta, fps, n, vazio)


def _linha_grade(cfg, pasta, fps, n, vazio):
    """Cada tecla com a sua midia, cada uma no seu proprio ritmo."""
    por_tecla = []
    for i in range(n):
        t = (cfg.get("teclas") or [{}] * n)[i] if i < len(cfg.get("teclas") or []) else {}
        nome = (t or {}).get("midia") or ""
        caminho = os.path.join(pasta, nome)
        if not nome or not os.path.isfile(caminho):
            por_tecla.append([vazio])
            continue
        quadros = carregar_quadros(caminho, fps)
        if not quadros:
            por_tecla.append([vazio])
            continue
        jpegs = [
            deck.preparar_jpeg(
                enquadrar(q, t.get("ajuste", "cover"), t.get("zoom", 1),
                          t.get("dx", 0), t.get("dy", 0))
            )
            for q in quadros
        ]
        por_tecla.append(jpegs)

    total = max(len(j) for j in por_tecla)
    return [[por_tecla[i][k % len(por_tecla[i])] for i in range(n)] for k in range(total)]


def _linha_cheio(cfg, pasta, fps, n, vazio):
    """Uma midia de cada vez, espalhada pelas 6 teclas."""
    ch = cfg.get("cheio") or {}
    escolhidos = [x for x in (ch.get("lista") or []) if os.path.isfile(os.path.join(pasta, x))]
    if not escolhidos:
        escolhidos = sorted(
            a for a in os.listdir(pasta)
            if not a.startswith((".", "_"))
            and os.path.splitext(a)[1].lower() in (EXT_IMAGEM | EXT_GIF | EXT_VIDEO)
            and os.path.isfile(os.path.join(pasta, a))
        )
    if not escolhidos:
        return [[vazio] * n]

    duracao = max(1, int(ch.get("duracao", 8) or 8))
    ajuste = ch.get("ajuste", "contain")
    linha = []
    for nome in escolhidos:
        quadros = carregar_quadros(os.path.join(pasta, nome), fps)
        if not quadros:
            continue
        animada = len(quadros) > 1
        for q in quadros:
            grande = enquadrar(q, ajuste, 1, 0, 0, lado=LADO * 3)
            pedacos = deck.recortar_em_teclas(grande, 3, 2)
            jpegs = [deck.preparar_jpeg(p) for p in pedacos[:n]]
            while len(jpegs) < n:
                jpegs.append(vazio)
            # imagem parada fica o tempo configurado; animacao corre solta
            linha.extend([jpegs] * (1 if animada else duracao * fps))
    return linha or [[vazio] * n]


# --------------------------------------------------------------------------- #
# estado da pasta
# --------------------------------------------------------------------------- #

def assinatura(pasta: str) -> str:
    partes = []
    try:
        for nome in sorted(os.listdir(pasta)):
            caminho = os.path.join(pasta, nome)
            if os.path.isfile(caminho):
                st = os.stat(caminho)
                partes.append(f"{nome}:{st.st_size}:{int(st.st_mtime)}")
    except OSError:
        pass
    return "|".join(partes)


def ler_config(pasta: str) -> dict:
    try:
        with open(os.path.join(pasta, NOME_CONFIG), encoding="utf-8") as fp:
            return json.load(fp)
    except (OSError, ValueError):
        return {}


# --------------------------------------------------------------------------- #

def principal():
    p = argparse.ArgumentParser(description="Mostra o painel nas teclas do aparelho.")
    p.add_argument("--midia", default=PASTA_PADRAO)
    p.add_argument("--fps", type=int, default=10, help="quadros por segundo (padrao 10)")
    p.add_argument("--brilho", type=int, default=100)
    args = p.parse_args()

    pasta = os.path.abspath(args.midia)
    fps = max(1, min(30, args.fps))
    if not os.path.isdir(pasta):
        print(f"\n  Pasta de midia nao encontrada: {pasta}\n")
        return 1

    print()
    print("  PAINEL NAS TECLAS")
    print("  =================")
    print(f"  Pasta: {pasta}")
    print(f"  Taxa:  {fps} quadros por segundo")
    if cv2 is None:
        print("  (sem opencv: MP4 nao toca. Para tocar: pip install opencv-python)")

    try:
        d = deck.Deck().abrir()
    except Exception as e:
        print(f"\n  Nao consegui abrir o aparelho: {e}")
        print("  Quase sempre e o programa do fabricante segurando o aparelho.")
        print("  Feche ele (inclusive perto do relogio) e tente de novo.\n")
        return 1

    print("  Conectado. Ctrl+C para parar.\n")
    ultimo_envio = [None] * deck.TECLAS_COM_TELA
    assinatura_atual = None
    linha = [[deck.preparar_jpeg(tecla_vazia())] * deck.TECLAS_COM_TELA]
    k = 0
    proxima_checagem = 0.0
    intervalo = 1.0 / fps

    try:
        d.reiniciar(args.brilho)
        while True:
            agora = time.time()

            # a pasta ou a configuracao mudou?
            if agora >= proxima_checagem:
                proxima_checagem = agora + 1.0
                cfg = ler_config(pasta)
                nova = json.dumps(cfg, sort_keys=True) + "||" + assinatura(pasta)
                if nova != assinatura_atual:
                    assinatura_atual = nova
                    print(f"  [{time.strftime('%H:%M:%S')}] mudou: remontando "
                          f"(modo {cfg.get('modo', 'grade')})...")
                    linha = montar_linha_do_tempo(cfg, pasta, fps)
                    k = 0
                    ultimo_envio = [None] * deck.TECLAS_COM_TELA
                    print(f"            {len(linha)} quadro(s)")

            quadro = linha[k % len(linha)]
            k += 1

            # so manda a tecla que mudou: economiza USB e deixa mais fluido
            mudou = False
            for i, jpeg in enumerate(quadro):
                if jpeg is not ultimo_envio[i]:
                    d.definir_jpeg(i, jpeg)
                    ultimo_envio[i] = jpeg
                    mudou = True
            if mudou:
                d.aplicar()

            dormir = intervalo - (time.time() - agora)
            if dormir > 0:
                time.sleep(dormir)
    except KeyboardInterrupt:
        print("\n  Parando...")
    finally:
        try:
            d.fechar()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(principal())
