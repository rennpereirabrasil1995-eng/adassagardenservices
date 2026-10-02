#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
detectar.py - descobre COMO a tela do macro pad esta ligada no computador.

Rode e mande a saida inteira. Uso:
    python3 detectar.py        (Linux / macOS)
    python detectar.py         (Windows)
"""

import os
import platform
import re
import subprocess
import sys

SO = platform.system()

# fabricantes comuns desses pads / controladores de tela por USB
PISTAS_HID = [
    "soomfon", "stream", "streamdeck", "elgato", "deck", "macro", "keypad",
    "mirabox", "ajazz", "ulanzi", "loupedeck", "hid", "custom hid",
]
PISTAS_MONITOR = ["displaylink", "usb display", "usb graphics", "fresco", "magic control"]


def rodar(cmd, shell=False):
    try:
        r = subprocess.run(cmd, shell=shell, capture_output=True, text=True,
                           timeout=30, errors="replace")
        return (r.stdout or "") + (r.stderr or "")
    except (OSError, subprocess.SubprocessError):
        return ""


def ps(script):
    """Roda PowerShell no Windows."""
    return rodar(["powershell", "-NoProfile", "-NonInteractive", "-Command", script])


def titulo(txt):
    print()
    print("=" * 64)
    print("  " + txt)
    print("=" * 64)


# --------------------------------------------------------------------------- #
# monitores
# --------------------------------------------------------------------------- #

def monitores():
    linhas = []
    if SO == "Windows":
        saida = ps(
            "Add-Type -AssemblyName System.Windows.Forms;"
            "[System.Windows.Forms.Screen]::AllScreens | "
            "ForEach-Object { \"$($_.DeviceName) | $($_.Bounds.Width)x$($_.Bounds.Height) "
            "em $($_.Bounds.X),$($_.Bounds.Y) | principal=$($_.Primary)\" }"
        )
        linhas = [l.strip() for l in saida.splitlines() if l.strip()]
    elif SO == "Darwin":
        saida = rodar(["system_profiler", "SPDisplaysDataType"])
        linhas = [l.rstrip() for l in saida.splitlines()
                  if re.search(r"Resolution|Display Type|^\s{8}\S.*:$", l)]
    else:
        saida = rodar(["xrandr", "--listmonitors"])
        if saida.strip():
            linhas = [l.strip() for l in saida.splitlines() if l.strip()]
        else:  # sem X rodando: olha o DRM direto
            base = "/sys/class/drm"
            if os.path.isdir(base):
                for nome in sorted(os.listdir(base)):
                    arq = os.path.join(base, nome, "status")
                    if os.path.isfile(arq):
                        try:
                            with open(arq) as fp:
                                st = fp.read().strip()
                            if st == "connected":
                                linhas.append(f"{nome}: {st}")
                        except OSError:
                            pass
    return linhas


# --------------------------------------------------------------------------- #
# dispositivos USB
# --------------------------------------------------------------------------- #

def usb():
    itens = []  # (vid, pid, nome, extra)

    if SO == "Windows":
        saida = ps(
            "Get-CimInstance Win32_PnPEntity | "
            "Where-Object { $_.PNPDeviceID -match '^(USB|HID)' } | "
            "ForEach-Object { \"$($_.PNPClass)`t$($_.Name)`t$($_.PNPDeviceID)\" }"
        )
        for linha in saida.splitlines():
            partes = linha.split("\t")
            if len(partes) < 3:
                continue
            classe, nome, devid = partes[0].strip(), partes[1].strip(), partes[2].strip()
            m = re.search(r"VID_([0-9A-Fa-f]{4})&PID_([0-9A-Fa-f]{4})", devid)
            vid = m.group(1).lower() if m else "????"
            pid = m.group(2).lower() if m else "????"
            itens.append((vid, pid, nome, classe))

    elif SO == "Darwin":
        saida = rodar(["system_profiler", "SPUSBDataType"])
        nome = None
        vid = pid = "????"
        for linha in saida.splitlines():
            t = linha.strip()
            if t.endswith(":") and not t.startswith(("Product ID", "Vendor ID", "Serial")):
                if nome and (vid != "????" or pid != "????"):
                    itens.append((vid, pid, nome, ""))
                nome, vid, pid = t[:-1], "????", "????"
            elif t.startswith("Product ID:"):
                m = re.search(r"0x([0-9a-fA-F]+)", t)
                if m:
                    pid = m.group(1).lower().zfill(4)
            elif t.startswith("Vendor ID:"):
                m = re.search(r"0x([0-9a-fA-F]+)", t)
                if m:
                    vid = m.group(1).lower().zfill(4)
        if nome and (vid != "????" or pid != "????"):
            itens.append((vid, pid, nome, ""))

    else:  # Linux
        saida = rodar(["lsusb"])
        for linha in saida.splitlines():
            m = re.search(r"ID ([0-9a-fA-F]{4}):([0-9a-fA-F]{4})\s*(.*)", linha)
            if m:
                itens.append((m.group(1).lower(), m.group(2).lower(),
                              m.group(3).strip() or "(sem nome)", ""))
        if not itens:  # sem lsusb: le o sysfs
            base = "/sys/bus/usb/devices"
            if os.path.isdir(base):
                for d in sorted(os.listdir(base)):
                    cam = os.path.join(base, d)

                    def ler(arq):
                        try:
                            with open(os.path.join(cam, arq)) as fp:
                                return fp.read().strip()
                        except OSError:
                            return ""

                    vid, pid = ler("idVendor"), ler("idProduct")
                    if vid and pid:
                        nome = (ler("manufacturer") + " " + ler("product")).strip()
                        itens.append((vid, pid, nome or "(sem nome)", ""))
    return itens


def ehSuspeito(nome, extra):
    alvo = (nome + " " + extra).lower()
    # ignora o obvio do sistema
    if any(x in alvo for x in ("hub", "host controller", "root ", "bluetooth", "webcam",
                               "câmera", "camera", "mouse", "teclado padrão")):
        return False
    return any(p in alvo for p in PISTAS_HID) or "hid" in extra.lower()


def principal():
    titulo("SISTEMA")
    print(f"  {platform.platform()}")
    print(f"  Python {sys.version.split()[0]}")

    mons = monitores()
    titulo(f"MONITORES ({len(mons)} linha(s))")
    if mons:
        for m in mons:
            print("  " + m)
    else:
        print("  (nao consegui listar)")

    disp = usb()
    titulo(f"USB / HID ({len(disp)} dispositivo(s))")
    if not disp:
        print("  (nao consegui listar)")
    for vid, pid, nome, extra in disp:
        marca = " <<< SUSPEITO" if ehSuspeito(nome, extra) else ""
        cls = f" [{extra}]" if extra else ""
        print(f"  {vid}:{pid}  {nome}{cls}{marca}")

    # ------------------------------------------------------------- veredito
    texto_tudo = " ".join(f"{n} {e}" for _, _, n, e in disp).lower()
    tem_usb_display = any(p in texto_tudo for p in PISTAS_MONITOR)
    suspeitos = [(v, p, n) for v, p, n, e in disp if ehSuspeito(n, e)]
    varios_monitores = len(mons) > 1

    titulo("VEREDITO")
    if tem_usb_display or varios_monitores:
        print("  Parece CASO 1: a tela entra como MONITOR.")
        print("  -> O painel que ja esta pronto funciona:")
        print("       ./iniciar.sh   e arraste a janela pra tela do aparelho (F11).")
        if varios_monitores:
            print(f"  (foram vistos {len(mons)} monitores)")
    elif suspeitos:
        print("  Parece CASO 2: a tela e desenhada pelo proprio aparelho via USB HID.")
        print("  -> O navegador NAO alcanca essa tela; precisa de um programa HID.")
        print("  Candidatos encontrados:")
        for v, p, n in suspeitos:
            print(f"       VID:PID = {v}:{p}   {n}")
    else:
        print("  Nao deu pra decidir sozinho.")
        print("  Dica: rode com o aparelho DESCONECTADO, salve a saida, conecte e rode")
        print("  de novo. O que aparecer na segunda vez e o aparelho.")

    print()
    print("  >>> Copie TUDO que apareceu acima e me mande. <<<")
    print()


if __name__ == "__main__":
    principal()
    if SO == "Windows":
        input("Aperte Enter para fechar...")
