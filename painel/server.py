#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Painel de Midia - servidor local para usar a tela do macro pad (soomfon e similares)
para exibir imagens, GIFs e videos.

Nao precisa instalar nada alem do Python 3.8+.

Uso:
    python3 server.py                 # porta 8080
    python3 server.py --porta 9000
    python3 server.py --midia /caminho/da/pasta

Depois abra:
    http://localhost:8080/          -> a TELA (arraste pra tela do aparelho e de F11)
    http://SEU_IP:8080/painel       -> o CONTROLE (celular, mesmo wi-fi)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import re
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, "web")

EXT_IMAGEM = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".avif", ".svg"}
EXT_VIDEO = {".mp4", ".webm", ".m4v", ".ogv", ".mov", ".mkv", ".avi"}
# formatos que o navegador toca sem dor de cabeca
VIDEO_OK = {".mp4", ".webm", ".m4v", ".ogv"}

NOME_CONFIG = "_config.json"
LIMITE_UPLOAD = 2 * 1024 * 1024 * 1024  # 2 GB

CONFIG_PADRAO = {
    "modo": "grade",                 # grade | cheio | calibrar
    "fundo": "#000000",
    "grade": {"colunas": 3, "linhas": 2},
    "calibragem": {
        "rotacao": 0,                # 0, 90, 180, 270
        "espelhar": False,
        "largura": 150,              # tamanho da celula, em pixels da tela
        "altura": 150,
        "gap_x": 24,
        "gap_y": 24,
        "offset_x": 0,               # ajuste fino do bloco inteiro
        "offset_y": 0,
        "raio": 10,                  # canto arredondado da celula
    },
    "teclas": [],                    # preenchido em normalizar_config()
    "cheio": {
        "lista": [],                 # vazio = usa todos os arquivos da pasta
        "duracao": 8,
        "ajuste": "contain",
        "embaralhar": False,
        "som": False,
    },
}

TECLA_PADRAO = {"midia": "", "ajuste": "cover", "zoom": 1.0, "dx": 0, "dy": 0, "rotulo": ""}

_trava = threading.Lock()
_contador_recarga = 0


# --------------------------------------------------------------------------- #
# util
# --------------------------------------------------------------------------- #

def nome_seguro(nome: str) -> str:
    """Impede ../ e nomes esquisitos. Devolve '' se nao sobrar nada util."""
    nome = (nome or "").replace("\\", "/").split("/")[-1].strip()
    nome = re.sub(r"[^A-Za-z0-9._ ()\-]", "_", nome)
    nome = nome.lstrip(".").strip()
    return nome[:120]


def tipo_do_arquivo(nome: str) -> str:
    ext = os.path.splitext(nome)[1].lower()
    if ext in EXT_IMAGEM:
        return "imagem"
    if ext in EXT_VIDEO:
        return "video"
    return ""


def ip_local() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


# --------------------------------------------------------------------------- #
# config + arquivos
# --------------------------------------------------------------------------- #

def caminho_config(pasta: str) -> str:
    return os.path.join(pasta, NOME_CONFIG)


def normalizar_config(cfg: dict) -> dict:
    base = json.loads(json.dumps(CONFIG_PADRAO))
    if isinstance(cfg, dict):
        base = mesclar(base, cfg)

    g = base["grade"]
    g["colunas"] = max(1, min(8, int(g.get("colunas", 3) or 3)))
    g["linhas"] = max(1, min(8, int(g.get("linhas", 2) or 2)))

    total = g["colunas"] * g["linhas"]
    teclas = base.get("teclas") or []
    if not isinstance(teclas, list):
        teclas = []
    novas = []
    for i in range(total):
        t = dict(TECLA_PADRAO)
        if i < len(teclas) and isinstance(teclas[i], dict):
            t.update({k: v for k, v in teclas[i].items() if k in TECLA_PADRAO})
        t["midia"] = nome_seguro(str(t.get("midia") or ""))
        t["ajuste"] = "contain" if t.get("ajuste") == "contain" else "cover"
        try:
            t["zoom"] = max(0.2, min(5.0, float(t.get("zoom", 1) or 1)))
        except (TypeError, ValueError):
            t["zoom"] = 1.0
        for eixo in ("dx", "dy"):
            try:
                t[eixo] = max(-500, min(500, int(float(t.get(eixo, 0) or 0))))
            except (TypeError, ValueError):
                t[eixo] = 0
        t["rotulo"] = str(t.get("rotulo") or "")[:40]
        novas.append(t)
    base["teclas"] = novas

    c = base["calibragem"]
    try:
        c["rotacao"] = int(c.get("rotacao", 0) or 0) % 360 // 90 * 90
    except (TypeError, ValueError):
        c["rotacao"] = 0
    c["espelhar"] = bool(c.get("espelhar"))
    for chave, minimo, maximo, padrao in (
        ("largura", 10, 2000, 150), ("altura", 10, 2000, 150),
        ("gap_x", -500, 1000, 24), ("gap_y", -500, 1000, 24),
        ("offset_x", -2000, 2000, 0), ("offset_y", -2000, 2000, 0),
        ("raio", 0, 200, 10),
    ):
        try:
            c[chave] = max(minimo, min(maximo, int(float(c.get(chave, padrao)))))
        except (TypeError, ValueError):
            c[chave] = padrao

    ch = base["cheio"]
    try:
        ch["duracao"] = max(1, min(600, int(float(ch.get("duracao", 8)))))
    except (TypeError, ValueError):
        ch["duracao"] = 8
    ch["ajuste"] = "cover" if ch.get("ajuste") == "cover" else "contain"
    ch["embaralhar"] = bool(ch.get("embaralhar"))
    ch["som"] = bool(ch.get("som"))
    lista = ch.get("lista")
    ch["lista"] = [nome_seguro(str(n)) for n in lista if str(n).strip()] if isinstance(lista, list) else []

    if base.get("modo") not in ("grade", "cheio", "calibrar"):
        base["modo"] = "grade"
    fundo = str(base.get("fundo") or "#000000")
    base["fundo"] = fundo if re.fullmatch(r"#[0-9A-Fa-f]{6}", fundo) else "#000000"
    return base


def mesclar(base: dict, novo: dict) -> dict:
    for k, v in novo.items():
        if k not in base:
            continue
        if isinstance(base[k], dict) and isinstance(v, dict):
            base[k] = mesclar(base[k], v)
        else:
            base[k] = v
    return base


def ler_config(pasta: str) -> dict:
    try:
        with open(caminho_config(pasta), "r", encoding="utf-8") as fp:
            return normalizar_config(json.load(fp))
    except (OSError, ValueError):
        return normalizar_config({})


def gravar_config(pasta: str, cfg: dict) -> dict:
    cfg = normalizar_config(cfg)
    tmp = caminho_config(pasta) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fp:
        json.dump(cfg, fp, ensure_ascii=False, indent=2)
    os.replace(tmp, caminho_config(pasta))
    return cfg


def listar_arquivos(pasta: str) -> list:
    itens = []
    try:
        nomes = sorted(os.listdir(pasta), key=lambda n: n.lower())
    except OSError:
        return itens
    for nome in nomes:
        if nome.startswith(".") or nome.startswith("_"):
            continue
        caminho = os.path.join(pasta, nome)
        if not os.path.isfile(caminho):
            continue
        tipo = tipo_do_arquivo(nome)
        if not tipo:
            continue
        ext = os.path.splitext(nome)[1].lower()
        st = os.stat(caminho)
        itens.append({
            "nome": nome,
            "tipo": tipo,
            "tamanho": st.st_size,
            "mtime": int(st.st_mtime),
            "suportado": tipo == "imagem" or ext in VIDEO_OK,
        })
    return itens


def assinatura(cfg: dict, arquivos: list) -> str:
    h = hashlib.sha1()
    h.update(json.dumps(cfg, sort_keys=True, ensure_ascii=False).encode("utf-8"))
    for a in arquivos:
        h.update(f"{a['nome']}:{a['tamanho']}:{a['mtime']}".encode("utf-8"))
    h.update(str(_contador_recarga).encode("utf-8"))
    return h.hexdigest()[:16]


# --------------------------------------------------------------------------- #
# servidor
# --------------------------------------------------------------------------- #

class Handler(BaseHTTPRequestHandler):
    server_version = "PainelMidia/1.0"
    protocol_version = "HTTP/1.1"
    pasta_midia = ""

    # ---- helpers de resposta -------------------------------------------- #

    def _json(self, dados, codigo=200):
        corpo = json.dumps(dados, ensure_ascii=False).encode("utf-8")
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(corpo)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(corpo)

    def _erro(self, codigo, msg):
        self._json({"erro": msg}, codigo)

    def _corpo_json(self) -> dict:
        tam = int(self.headers.get("Content-Length") or 0)
        if tam <= 0 or tam > 5 * 1024 * 1024:
            return {}
        try:
            return json.loads(self.rfile.read(tam).decode("utf-8")) or {}
        except (ValueError, UnicodeDecodeError):
            return {}

    def _enviar_arquivo(self, caminho, so_cabecalho=False, cache=True):
        if not os.path.isfile(caminho):
            self.send_error(404, "Nao encontrado")
            return
        tamanho = os.path.getsize(caminho)
        tipo = mimetypes.guess_type(caminho)[0] or "application/octet-stream"
        inicio, fim = 0, tamanho - 1
        parcial = False

        faixa = self.headers.get("Range")
        if faixa:
            m = re.match(r"bytes=(\d*)-(\d*)", faixa.strip())
            if m:
                a, b = m.group(1), m.group(2)
                if a:
                    inicio = int(a)
                    fim = int(b) if b else tamanho - 1
                elif b:  # bytes=-500 -> ultimos 500
                    inicio = max(0, tamanho - int(b))
                if inicio >= tamanho:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{tamanho}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                fim = min(fim, tamanho - 1)
                parcial = True

        total = fim - inicio + 1
        self.send_response(206 if parcial else 200)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(total))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "max-age=60" if cache else "no-store")
        if parcial:
            self.send_header("Content-Range", f"bytes {inicio}-{fim}/{tamanho}")
        self.end_headers()
        if so_cabecalho:
            return
        with open(caminho, "rb") as fp:
            fp.seek(inicio)
            restante = total
            while restante > 0:
                bloco = fp.read(min(256 * 1024, restante))
                if not bloco:
                    break
                try:
                    self.wfile.write(bloco)
                except (BrokenPipeError, ConnectionResetError):
                    return
                restante -= len(bloco)

    def _caminho_midia(self, caminho_url):
        nome = nome_seguro(unquote(caminho_url[len("/midia/"):]))
        if not nome:
            return None
        return os.path.join(self.pasta_midia, nome)

    # ---- rotas ----------------------------------------------------------- #

    def do_GET(self, so_cabecalho=False):
        caminho = urlparse(self.path).path

        if caminho == "/":
            self._enviar_arquivo(os.path.join(WEB_DIR, "tela.html"), so_cabecalho, cache=False)
        elif caminho in ("/painel", "/painel/"):
            self._enviar_arquivo(os.path.join(WEB_DIR, "painel.html"), so_cabecalho, cache=False)
        elif caminho.startswith("/midia/"):
            alvo = self._caminho_midia(caminho)
            if not alvo:
                self.send_error(404, "Nao encontrado")
            else:
                self._enviar_arquivo(alvo, so_cabecalho)
        elif caminho == "/api/estado":
            with _trava:
                cfg = ler_config(self.pasta_midia)
                arquivos = listar_arquivos(self.pasta_midia)
                self._json({
                    "assinatura": assinatura(cfg, arquivos),
                    "config": cfg,
                    "arquivos": arquivos,
                })
        elif caminho == "/api/info":
            self._json({"ip": ip_local(), "porta": self.server.server_address[1],
                        "pasta": self.pasta_midia})
        elif caminho == "/favicon.ico":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self.send_error(404, "Nao encontrado")

    def do_HEAD(self):
        self.do_GET(so_cabecalho=True)

    def do_POST(self):
        caminho = urlparse(self.path).path
        global _contador_recarga

        if caminho == "/api/upload":
            nome = nome_seguro(unquote(self.headers.get("X-Nome") or ""))
            if not nome or not tipo_do_arquivo(nome):
                self._erro(400, "Arquivo sem nome valido ou formato nao aceito.")
                return
            tam = int(self.headers.get("Content-Length") or 0)
            if tam <= 0:
                self._erro(400, "Arquivo vazio.")
                return
            if tam > LIMITE_UPLOAD:
                self._erro(413, "Arquivo maior que 2 GB.")
                return
            destino = os.path.join(self.pasta_midia, nome)
            tmp = destino + ".parcial"
            lido = 0
            try:
                with open(tmp, "wb") as fp:
                    while lido < tam:
                        bloco = self.rfile.read(min(256 * 1024, tam - lido))
                        if not bloco:
                            break
                        fp.write(bloco)
                        lido += len(bloco)
                if lido != tam:
                    os.remove(tmp)
                    self._erro(400, "Envio incompleto.")
                    return
                os.replace(tmp, destino)
            except OSError as exc:
                if os.path.exists(tmp):
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
                self._erro(500, f"Falha ao gravar: {exc}")
                return
            self._json({"ok": True, "nome": nome})

        elif caminho == "/api/excluir":
            nome = nome_seguro(str(self._corpo_json().get("nome") or ""))
            if not nome:
                self._erro(400, "Nome invalido.")
                return
            alvo = os.path.join(self.pasta_midia, nome)
            if os.path.isfile(alvo):
                try:
                    os.remove(alvo)
                except OSError as exc:
                    self._erro(500, f"Nao deu pra apagar: {exc}")
                    return
            with _trava:
                cfg = ler_config(self.pasta_midia)
                for t in cfg["teclas"]:
                    if t["midia"] == nome:
                        t["midia"] = ""
                cfg["cheio"]["lista"] = [n for n in cfg["cheio"]["lista"] if n != nome]
                gravar_config(self.pasta_midia, cfg)
            self._json({"ok": True})

        elif caminho == "/api/config":
            novo = self._corpo_json()
            with _trava:
                cfg = ler_config(self.pasta_midia)
                # teclas chega como lista completa; o resto mescla
                if isinstance(novo.get("teclas"), list):
                    cfg["teclas"] = novo["teclas"]
                    novo = {k: v for k, v in novo.items() if k != "teclas"}
                if isinstance(novo.get("cheio"), dict) and isinstance(novo["cheio"].get("lista"), list):
                    cfg["cheio"]["lista"] = novo["cheio"]["lista"]
                cfg = gravar_config(self.pasta_midia, mesclar(cfg, novo))
            self._json({"ok": True, "config": cfg})

        elif caminho == "/api/recarregar":
            with _trava:
                _contador_recarga += 1
            self._json({"ok": True})

        else:
            self._erro(404, "Rota desconhecida.")

    def log_message(self, formato, *args):  # menos barulho no terminal
        if "--verboso" in sys.argv:
            super().log_message(formato, *args)


def principal():
    p = argparse.ArgumentParser(description="Painel de midia para a tela do macro pad.")
    p.add_argument("--porta", type=int, default=8080)
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--midia", default=os.path.join(BASE_DIR, "midia"),
                   help="pasta com as imagens e videos")
    p.add_argument("--verboso", action="store_true")
    args = p.parse_args()

    pasta = os.path.abspath(args.midia)
    os.makedirs(pasta, exist_ok=True)
    Handler.pasta_midia = pasta
    if not os.path.exists(caminho_config(pasta)):
        gravar_config(pasta, {})

    mimetypes.add_type("video/mp4", ".m4v")
    mimetypes.add_type("image/avif", ".avif")

    servidor = ThreadingHTTPServer((args.host, args.porta), Handler)
    servidor.daemon_threads = True
    ip = ip_local()
    print("=" * 58)
    print("  PAINEL DE MIDIA no ar")
    print("=" * 58)
    print(f"  TELA (abra nessa tela e de F11):  http://localhost:{args.porta}/")
    print(f"  CONTROLE (celular/outro PC):      http://{ip}:{args.porta}/painel")
    print(f"  Pasta de midia:                   {pasta}")
    print("  Ctrl+C para parar.")
    print("=" * 58)
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        print("\nParando...")
    finally:
        servidor.server_close()


if __name__ == "__main__":
    principal()
