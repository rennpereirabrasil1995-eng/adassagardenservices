# -*- coding: utf-8 -*-
"""
Driver USB para o Soomfon CN002 / Stream Controller SE (VID 1500, PID 3001)
e parentes da familia Ajazz AKP03 / Mirabox N3.

O protocolo foi documentado por engenharia reversa pelo projeto mirajazz
(https://github.com/4ndv/mirajazz, GPL) e pelo plugin opendeck-akp03
(https://github.com/4ndv/opendeck-akp03). Esta e uma reimplementacao em
Python da parte necessaria para desenhar nas teclas.

Como funciona:
  - todo comando e um relatorio HID de 1025 bytes: 0x00 + 1024 de carga
  - os comandos comecam com "CRT" e um codigo de 3 letras
      DIS  inicializa        LIG  brilho          BAT  envia imagem
      STP  aplica na tela    CLE  limpa tecla     HAN  dorme
  - a imagem de cada tecla e um JPEG 64x64 girado 90 graus
"""

from __future__ import annotations

import io
import time

try:
    import hid
except ImportError:  # pragma: no cover
    hid = None

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None

# o Pillow mudou o nome dessas constantes na versao 9.1; aceita as duas
if Image is not None:
    _LANCZOS = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
    _ROT270 = getattr(getattr(Image, "Transpose", Image), "ROTATE_270")
else:  # pragma: no cover
    _LANCZOS = _ROT270 = None

VID = 0x1500
PID = 0x3001
USAGE_PAGE = 0xFFA0
USAGE = 0x0001

TAM_PACOTE = 1024          # protocolo 2 e 3
TAM_RELATORIO = TAM_PACOTE + 1
LADO_IMAGEM = 64           # JPEG 64x64
GIRO = 90                  # graus, horario
TECLAS_COM_TELA = 6        # as 6 de cima tem LCD; as 3 de baixo sao botoes

CRT = b"CRT"


class ErroDeck(Exception):
    pass


def _texto(s: str) -> bytes:
    return s.encode("ascii")


class Deck:
    """Conexao com o aparelho."""

    def __init__(self, vid: int = VID, pid: int = PID):
        if hid is None:
            raise ErroDeck(
                "Falta a biblioteca hidapi. Instale com:  pip install hidapi pillow"
            )
        if Image is None:
            raise ErroDeck(
                "Falta a biblioteca Pillow. Instale com:  pip install hidapi pillow"
            )
        self.vid = vid
        self.pid = pid
        self.dev = None
        self._aberto = False
        self._iniciado = False
        self._cache = {}       # tecla -> jpeg, enviado no flush()
        self.atraso_pacote = 0.0   # segundos entre os pedacos da imagem;
                                   # sem isso a imagem grande chega rasgada

    # ----------------------------------------------------------- conexao

    @staticmethod
    def listar(vid: int = VID, pid: int = PID) -> list:
        """Lista as interfaces HID do aparelho."""
        if hid is None:
            raise ErroDeck("Falta a biblioteca hidapi. Rode: pip install hidapi pillow")
        return list(hid.enumerate(vid, pid))

    def abrir(self):
        """Abre a interface certa (usage page 0xFFA0, usage 1)."""
        candidatos = self.listar(self.vid, self.pid)
        if not candidatos:
            raise ErroDeck(
                f"Aparelho {self.vid:04x}:{self.pid:04x} nao encontrado.\n"
                "Confira se o cabo esta ligado e se o programa do fabricante "
                "esta FECHADO (os dois nao podem usar o aparelho ao mesmo tempo)."
            )

        escolhido = None
        for c in candidatos:
            if c.get("usage_page") == USAGE_PAGE and c.get("usage") == USAGE:
                escolhido = c
                break
        if escolhido is None:
            # algumas versoes do hidapi nao preenchem usage_page: tenta a ultima
            escolhido = candidatos[-1]

        caminho = escolhido["path"]

        # a biblioteca "hidapi" e a "hid" tem APIs diferentes; aceita as duas
        if hasattr(hid, "device"):
            self.dev = hid.device()
            self.dev.open_path(caminho)
            self._escrever_bruto = self.dev.write
        else:
            self.dev = hid.Device(path=caminho)
            self._escrever_bruto = self.dev.write

        self._aberto = True
        return self

    def reconectar(self, espera=1.5):
        """
        Fecha e abre de novo. Serve para quando o aparelho para de aceitar
        escrita depois de receber uma imagem grande demais.
        """
        import time as _t
        self.fechar()
        _t.sleep(espera)
        self._iniciado = False
        self._cache.clear()
        self.abrir()
        self.iniciar()
        return self

    def fechar(self):
        if self.dev is not None:
            try:
                self.dev.close()
            except Exception:
                pass
        self.dev = None
        self._aberto = False

    def __enter__(self):
        return self.abrir()

    def __exit__(self, *_):
        self.fechar()

    # ------------------------------------------------------------ baixo nivel

    def _escrever(self, dados: bytes):
        if not self._aberto:
            raise ErroDeck("Aparelho nao esta aberto.")
        enviados = self._escrever_bruto(bytes(dados))
        if enviados is not None and enviados < 0:
            raise ErroDeck("Falha ao escrever no aparelho.")

    def _comando(self, *partes) -> None:
        """Monta um relatorio de 1025 bytes e manda."""
        buf = bytearray([0x00])
        for p in partes:
            if isinstance(p, int):
                buf.append(p & 0xFF)
            elif isinstance(p, (bytes, bytearray)):
                buf.extend(p)
            else:
                raise TypeError(type(p))
        if len(buf) > TAM_RELATORIO:
            raise ErroDeck("Comando maior que o pacote.")
        buf.extend(b"\x00" * (TAM_RELATORIO - len(buf)))
        self._escrever(buf)

    # -------------------------------------------------------------- comandos

    def iniciar(self):
        """Handshake inicial. Idempotente."""
        if self._iniciado:
            return
        self._comando(CRT, 0x00, 0x00, _texto("DIS"))
        self._comando(CRT, 0x00, 0x00, _texto("LIG"), 0x00, 0x00, 0x00, 0x00)
        self._iniciado = True

    def brilho(self, porcento: int):
        """Brilho da tela, de 0 a 100."""
        self.iniciar()
        p = max(0, min(100, int(porcento)))
        self._comando(CRT, 0x00, 0x00, _texto("LIG"), 0x00, 0x00, p)

    def brilho_leds(self, porcento: int):
        """Brilho dos LEDs dos knobs, de 0 a 100."""
        self.iniciar()
        p = max(0, min(100, int(porcento)))
        self._comando(CRT, 0x00, 0x00, _texto("LBLIG"), p)

    def limpar_tecla(self, tecla: int):
        """Apaga uma tecla (0 a 8). Precisa de aplicar() depois."""
        self.iniciar()
        alvo = 0xFF if tecla == 0xFF else tecla + 1
        self._comando(_texto("CLE"), 0x00, 0x00, 0x00, alvo)
        self._cache.pop(tecla, None)

    def limpar_tudo(self):
        """Apaga todas as teclas."""
        self.iniciar()
        self.limpar_tecla(0xFF)
        # protocolo 2 e 3 precisam do STP para a limpeza aparecer
        self._comando(CRT, 0x00, 0x00, _texto("STP"))
        self._cache.clear()

    def _enviar_imagem(self, tecla: int, jpeg: bytes):
        """Manda o JPEG de uma tecla: cabecalho + pedacos de 1024 bytes."""
        tamanho = len(jpeg)
        self._comando(
            CRT, 0x00, 0x00, _texto("BAT"), 0x00, 0x00,
            (tamanho >> 8) & 0xFF, tamanho & 0xFF, tecla + 1,
        )
        enviado = 0
        primeiro = True
        while enviado < tamanho:
            if not primeiro and self.atraso_pacote > 0:
                import time as _t
                _t.sleep(self.atraso_pacote)
            primeiro = False
            pedaco = jpeg[enviado:enviado + TAM_PACOTE]
            buf = bytearray([0x00])
            buf.extend(pedaco)
            buf.extend(b"\x00" * (TAM_RELATORIO - len(buf)))
            self._escrever(buf)
            enviado += len(pedaco)

    def definir_imagem(self, tecla: int, imagem) -> None:
        """
        Guarda a imagem de uma tecla. So aparece depois de aplicar().
        `imagem` e um PIL.Image ou o caminho de um arquivo.
        """
        self.iniciar()
        if not isinstance(imagem, Image.Image):
            imagem = Image.open(imagem)
        self._cache[tecla] = preparar_jpeg(imagem)

    def definir_jpeg(self, tecla: int, jpeg: bytes) -> None:
        """Igual a definir_imagem, mas com o JPEG ja pronto (mais rapido)."""
        self.iniciar()
        self._cache[tecla] = jpeg

    def aplicar(self):
        """Envia o que esta guardado e manda a tela atualizar."""
        self.iniciar()
        if not self._cache:
            return
        for tecla, jpeg in list(self._cache.items()):
            self._enviar_imagem(tecla, jpeg)
        self._cache.clear()
        self._comando(CRT, 0x00, 0x00, _texto("STP"))

    def manter_vivo(self):
        """Evita que o aparelho durma."""
        self._comando(CRT, 0x00, 0x00, _texto("CONNECT"))

    def dormir(self):
        self._comando(CRT, 0x00, 0x00, _texto("HAN"))

    def desligar(self):
        self._comando(CRT, 0x00, 0x00, _texto("CLE"), 0x00, 0x00, _texto("DC"))
        self._comando(CRT, 0x00, 0x00, _texto("HAN"))

    def reiniciar(self, brilho_inicial: int = 100):
        """Limpa a tela e acerta o brilho."""
        self.iniciar()
        self.limpar_tudo()
        self.brilho(brilho_inicial)


# ------------------------------------------------------------------ imagens

def preparar_jpeg(imagem, lado: int = LADO_IMAGEM, qualidade: int = 90) -> bytes:
    """
    Deixa a imagem no formato que o aparelho espera:
    quadrada de 64x64, girada 90 graus no sentido horario, JPEG RGB.
    """
    if not isinstance(imagem, Image.Image):
        imagem = Image.open(imagem)
    im = imagem.convert("RGB").resize((lado, lado), _LANCZOS)
    if GIRO == 90:
        im = im.transpose(_ROT270)  # 270 anti-horario = 90 no horario
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=qualidade)
    return buf.getvalue()


def recortar_em_teclas(imagem, colunas: int = 3, linhas: int = 2) -> list:
    """
    Corta uma imagem grande nos pedacos de cada tecla, para espalhar
    uma figura so pelas 6 janelinhas. Devolve uma lista de PIL.Image.
    """
    if not isinstance(imagem, Image.Image):
        imagem = Image.open(imagem)
    im = imagem.convert("RGB")
    larg = im.width // colunas
    alt = im.height // linhas
    pedacos = []
    for lin in range(linhas):
        for col in range(colunas):
            caixa = (col * larg, lin * alt, (col + 1) * larg, (lin + 1) * alt)
            pedacos.append(im.crop(caixa))
    return pedacos
