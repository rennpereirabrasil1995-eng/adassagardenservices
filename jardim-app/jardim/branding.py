"""Aparência do app: nome no topo, logo e paleta de cores (Conta → Aparência, só o dono).

Paleta: sai de duas cores, a principal (topo do app) e a de destaque (botões principais, "em
andamento", aba escolhida). O resto (fundo, texto, linhas, links e o modo escuro inteiro) é
calculado a partir delas, sempre conferindo o contraste, pra continuar fácil de ler ao ar livre.
A paleta "Floresta" é a original do app: com ela nenhuma cor é trocada (vale o style.css).

Logo: guardado como PNG (nunca SVG, que pode carregar código) em uploads/brand/, junto com os
ícones gerados a partir dele: o ícone do app instalado no celular e o da aba do navegador. Os
arquivos levam uma versão no nome, então celular e navegador pegam o logo novo assim que ele muda.
"""
import colorsys
import json
import re
import secrets
from functools import lru_cache
from pathlib import Path

from flask import Blueprint, abort, current_app, g, send_from_directory, url_for

from .db import get_db

bp = Blueprint("branding", __name__)

# (cor principal, cor de destaque)
PRESETS = {
    "floresta": ("#12301f", "#f4c430"),
    "oliva": ("#2f3d1c", "#e8a33d"),
    "oceano": ("#0f3550", "#f6b93b"),
    "ceu": ("#1c4a86", "#ffd166"),
    "menta": ("#0d3b37", "#7fdcc0"),
    "terracota": ("#6a2e1c", "#f2c078"),
    "vinho": ("#4b1631", "#f4a6a0"),
    "lavanda": ("#352a5c", "#c9b6ff"),
    "grafite": ("#1f2328", "#f4c430"),
}
DEFAULT_THEME = "floresta"
HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
ASSET_NAME = re.compile(r"^(logo|favicon|icon-192|icon-512|apple)-[0-9a-f]{8}\.png$")


# ---------- Cores ----------

def _rgb(value):
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) / 255 for i in (0, 2, 4))


def _hex(rgb):
    return "#" + "".join(f"{max(0, min(255, round(c * 255))):02x}" for c in rgb)


def luminance(value):
    """Luminância relativa (WCAG): 0 = preto, 1 = branco."""
    def channel(c):
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g_, b = (channel(c) for c in _rgb(value))
    return 0.2126 * r + 0.7152 * g_ + 0.0722 * b


def contrast(a, b):
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _hls(value):
    return colorsys.rgb_to_hls(*_rgb(value))


def _tone(hue, light, sat):
    return _hex(colorsys.hls_to_rgb(hue, max(0.0, min(1.0, light)), max(0.0, min(1.0, sat))))


def _until(hue, sat, start, step, against, target):
    """Anda na luminosidade (step < 0 escurece, > 0 clareia) até o contraste com `against` chegar no
    alvo, ou até chegar no preto/branco."""
    light = start
    color = _tone(hue, light, sat)
    while contrast(color, against) < target:
        light = round(light + step, 4)
        if not 0.0 <= light <= 1.0:
            break
        color = _tone(hue, light, sat)
    return color


def _first_readable(background, candidates, target):
    """A primeira cor da lista que chega no contraste `target`; se nenhuma chega, a de maior contraste."""
    for color in candidates:
        if contrast(color, background) >= target:
            return color
    return max(candidates, key=lambda color: contrast(color, background))


def _soften(ink, background, amount, target):
    """Texto secundário: `ink` um pouco misturado no fundo, sem o contraste cair abaixo de `target`."""
    while amount > 0:
        color = _mix(ink, background, amount)
        if contrast(color, background) >= target:
            return color
        amount = round(amount - 0.05, 2)
    return ink


def header_ink(main):
    """Cor do texto no topo: clara num topo escuro, escura num topo claro; a que der mais contraste."""
    h, _, s = _hls(main)
    light = _until(h, min(s, 0.16), 0.97, 0.01, main, 7)
    dark = _until(h, s, 0.12, -0.01, main, 7)
    return max((light, dark), key=lambda color: contrast(color, main))


# Abaixo disso o texto do topo fica com pouco contraste (no sol, principalmente). A cor principal
# de contraste mais baixo possível ainda chega a ~4,6 com preto ou branco; o ideal é 7 ou mais.
HEADER_MIN = 5.5


def header_hard_to_read(main):
    return contrast(header_ink(main), main) < HEADER_MIN


@lru_cache(maxsize=64)
def palette(main, accent):
    """Todas as variáveis de cor do style.css (modo claro e modo escuro) a partir das duas cores."""
    h, l, s = _hls(main)
    ns = min(s, 0.16)                      # neutros levam só um pouquinho da cor principal
    ah, al, as_ = _hls(accent)

    bg = _tone(h, 0.935, ns * 0.9)
    ink = _until(h, s, min(l, 0.22), -0.01, bg, 12)
    # texto dos botões na cor de destaque: o mesmo tom escuro do texto (escurecendo se precisar) ou branco
    on_marker = _first_readable(accent, (_until(h, s, _hls(ink)[1], -0.01, accent, 4.5), "#ffffff"), 4.5)
    hover = _tone(ah, al - 0.06, as_)      # botão um pouco mais escuro ao passar o mouse...
    if hover == accent or contrast(hover, on_marker) < 4.5:
        hover = _tone(ah, al + 0.06, as_)  # ...ou mais claro, se escurecer atrapalhar a leitura
    mast_ink = header_ink(main)
    light = {
        "--bg": bg, "--surface": "#ffffff", "--hover": _tone(h, 0.967, ns),
        "--ink": ink,
        "--muted": _until(h, ns, 0.42, -0.01, bg, 5.2),
        "--line": _tone(h, 0.85, ns), "--line-strong": _tone(h, 0.6, ns),
        "--link": _until(h, max(s, 0.35) if s > 0.05 else s, 0.45, -0.01, "#ffffff", 5.5),
        "--marker": accent, "--on-marker": on_marker, "--marker-hover": hover,
        "--mast-bg": main, "--mast-ink": mast_ink,
        "--mast-muted": _soften(mast_ink, main, 0.3, 6),  # abas e "Conta"/"Sair": um pouco mais apagados
    }
    light["--focus"] = light["--link"]

    dsurface = _tone(h, 0.11, ns * 1.3)
    dark = {
        "--bg": _tone(h, 0.07, ns * 1.3), "--surface": dsurface, "--hover": _tone(h, 0.14, ns * 1.3),
        "--ink": _tone(h, 0.92, ns),
        "--muted": _until(h, ns, 0.66, 0.01, dsurface, 5.5),
        "--line": _tone(h, 0.18, ns), "--line-strong": _tone(h, 0.36, ns),
        "--link": _until(h, max(s, 0.4) if s > 0.05 else s, 0.6, 0.01, dsurface, 6),
        "--mast-bg": _tone(h, 0.05, min(s, 0.4)), "--mast-ink": _tone(h, 0.95, ns),
        "--mast-muted": _tone(h, 0.75, ns),
    }
    dark["--focus"] = dark["--link"]
    return light, dark


def _mix(a, b, amount):
    """a misturado com b (amount = quanto de b)."""
    ra, rb = _rgb(a), _rgb(b)
    return _hex(tuple(x + (y - x) * amount for x, y in zip(ra, rb)))


# As cores originais do style.css (paleta Floresta), pra prévia em Conta → Aparência.
DEFAULT_LIGHT = {
    "--bg": "#eef1ec", "--surface": "#ffffff", "--hover": "#f5f8f3", "--ink": "#12301f", "--muted": "#4d5d53",
    "--line": "#d5dcd2", "--line-strong": "#8fa093", "--link": "#1f6a38", "--focus": "#1f6a38",
    "--marker": "#f4c430", "--on-marker": "#12301f", "--marker-hover": "#e6b619",
    "--mast-bg": "#12301f", "--mast-ink": "#f2f6f1", "--mast-muted": "#b5c8ba",
}
DEFAULT_DARK = {
    "--bg": "#0e1611", "--surface": "#16211a", "--hover": "#1b2920", "--ink": "#e7efe8", "--muted": "#a3b3a8",
    "--line": "#26352c", "--line-strong": "#4f6557", "--link": "#7fd39a", "--focus": "#7fd39a", "--mast-bg": "#0a110d",
}


def preview(main, accent):
    """Cores do modo claro e do escuro (completas) pra prévia ao vivo em Conta → Aparência."""
    status_light = {"--ok": "#2f7d46", "--on-ok": "#ffffff"}
    status_dark = {"--ok": "#4caf6d", "--on-ok": "#0e1611"}
    if is_default((main, accent)):
        return {**DEFAULT_LIGHT, **status_light}, {**DEFAULT_LIGHT, **DEFAULT_DARK, **status_dark}
    light, dark = palette(main, accent)
    return {**light, **status_light}, {**light, **dark, **status_dark}


def theme_colors(row=None):
    """(principal, destaque) da paleta escolhida."""
    row = row if row is not None else _settings()
    key = row["theme"] if row else DEFAULT_THEME
    if key == "custom" and HEX.match(row["color_main"] or "") and HEX.match(row["color_accent"] or ""):
        return row["color_main"].lower(), row["color_accent"].lower()
    return PRESETS.get(key, PRESETS[DEFAULT_THEME])


def is_default(colors):
    return tuple(colors) == PRESETS[DEFAULT_THEME]


def theme_css(main, accent):
    """O <style> que troca as cores do style.css. Vazio na paleta original. Só na tela (a impressão
    continua em preto no branco)."""
    if is_default((main, accent)):
        return ""
    light, dark = palette(main, accent)
    block = lambda d: " ".join(f"{k}: {v};" for k, v in d.items())
    return (f"@media screen {{ :root {{ {block(light)} }} }}\n"
            f"@media screen and (prefers-color-scheme: dark) {{ :root {{ {block(dark)} }} }}")


def header_colors(row=None):
    """Cor do topo no modo claro e no escuro (pra meta theme-color, ícones e o fundo atrás do logo)."""
    main, accent = theme_colors(row)
    if is_default((main, accent)):
        return "#12301f", "#0a110d"
    light, dark = palette(main, accent)
    return light["--mast-bg"], dark["--mast-bg"]


# ---------- Configurações ----------

def _settings():
    if "brand_row" not in g:
        g.brand_row = get_db().execute("SELECT * FROM settings WHERE id = 1").fetchone()
    return g.brand_row


def forget():
    """Depois de salvar: a próxima leitura pega as configurações novas."""
    g.pop("brand_row", None)


def app_name(row=None):
    """Nome no topo: o escolhido em Aparência, senão o nome da empresa, senão o padrão do app."""
    row = row if row is not None else _settings()
    if row is not None:
        return (row["app_name"] or row["company_name"] or "").strip() or current_app.config["APP_NAME"]
    return current_app.config["APP_NAME"]


def short_name(row=None):
    """Nome embaixo do ícone do app no celular (cabe uns 12 caracteres)."""
    row = row if row is not None else _settings()
    if row is not None and row["app_short_name"]:
        return row["app_short_name"]
    name = app_name(row)
    if row is None or not (row["app_name"] or row["company_name"]):
        return current_app.config["APP_SHORT_NAME"]
    return name if len(name) <= 12 else name.split(" ")[0][:12]


def brand_dir():
    return Path(current_app.config["UPLOAD_ROOT"]) / "brand"


def logo_info(row=None):
    row = row if row is not None else _settings()
    if row is None or not row["logo_version"]:
        return None
    try:
        meta = json.loads(row["logo_meta"] or "{}")
    except ValueError:
        meta = {}
    v = row["logo_version"]
    return {"version": v, "meta": meta,
            "logo": url_for("branding.asset", filename=f"logo-{v}.png"),
            "favicon": url_for("branding.asset", filename=f"favicon-{v}.png"),
            "icon192": url_for("branding.asset", filename=f"icon-192-{v}.png"),
            "icon512": url_for("branding.asset", filename=f"icon-512-{v}.png"),
            "apple": url_for("branding.asset", filename=f"apple-{v}.png")}


def _chip(meta, header):
    """Logo escuro com fundo transparente num topo escuro some: ganha um fundinho claro (e vice-versa)."""
    if not meta.get("alpha"):
        return ""
    logo_lum, head_lum = meta.get("lum", 0.5), luminance(header)
    ratio = (max(logo_lum, head_lum) + 0.05) / (min(logo_lum, head_lum) + 0.05)
    if ratio >= 2.2:
        return ""
    return "light" if logo_lum < 0.4 else "dark"


def context():
    """O que os templates usam (base.html, página da cotação, manifesto)."""
    row = _settings()
    main, accent = theme_colors(row)
    head_light, head_dark = header_colors(row)
    logo = logo_info(row)
    chips = []
    if logo:
        for mode, header in (("l", head_light), ("d", head_dark)):
            chip = _chip(logo["meta"], header)
            if chip:
                chips.append(f"chip-{mode}-{chip}")
    return {
        "name": app_name(row), "short_name": short_name(row), "logo": logo,
        "show_name": bool(row["show_name"]) if row is not None else True,
        "chip_class": " ".join(chips), "css": theme_css(main, accent),
        "theme_light": head_light, "theme_dark": head_dark,
    }


# ---------- Logo e ícones ----------

def _open_logo(file_storage):
    try:
        import pillow_heif

        pillow_heif.register_heif_opener()
    except ImportError:
        pass
    try:
        from PIL import Image, ImageOps

        image = Image.open(file_storage.stream)
        image = ImageOps.exif_transpose(image).convert("RGBA")
    except Exception:
        return None
    box = image.getchannel("A").getbbox()  # corta a sobra transparente em volta
    if box:
        image = image.crop(box)
    image.thumbnail((1024, 1024))
    return image


def _meta(image):
    """Brilho médio da parte visível do logo e se ele tem fundo transparente."""
    alpha = image.getchannel("A")
    has_alpha = alpha.getextrema()[0] < 250
    small = image.copy()
    small.thumbnail((96, 96))
    total = weight = 0.0
    pixels = small.tobytes()  # RGBA, 4 bytes por pixel
    for i in range(0, len(pixels), 4):
        r, g_, b, a = pixels[i:i + 4]
        if a < 128:
            continue
        total += luminance(f"#{r:02x}{g_:02x}{b:02x}")
        weight += 1
    return {"alpha": has_alpha, "lum": round(total / weight, 3) if weight else 0.5,
            "w": image.width, "h": image.height}


def _square_icon(logo, size, background, fill=0.64):
    """Ícone quadrado: o logo centralizado numa cor de fundo (o celular recorta as bordas)."""
    from PIL import Image

    canvas = Image.new("RGBA", (size, size), background)
    inner = logo.copy()
    inner.thumbnail((int(size * fill), int(size * fill)))
    canvas.alpha_composite(inner, ((size - inner.width) // 2, (size - inner.height) // 2))
    return canvas.convert("RGB")


def icon_background(row, meta):
    """Fundo do ícone do celular: a cor do topo; branco se o logo sumir nela."""
    head_light, _ = header_colors(row)
    if meta.get("alpha") and _chip(meta, head_light):
        return "#ffffff"
    return head_light


def needs_render(row):
    """Os ícones precisam ser gerados de novo? (só se a cor de fundo deles mudou: trocar só o nome
    não muda o ícone, e o celular não precisa baixar nada.)"""
    if not row["logo_version"]:
        return (brand_dir() / "logo-original.png").exists()
    try:
        meta = json.loads(row["logo_meta"] or "{}")
    except ValueError:
        return True
    return meta.get("bg") != icon_background(row, meta)


def render_assets(row):
    """Gera (de novo) o logo e os ícones a partir do original, com uma versão nova no nome.
    Chamado quando o logo muda e quando a paleta muda (a cor de fundo do ícone acompanha o topo)."""
    from PIL import Image

    folder = brand_dir()
    original = folder / "logo-original.png"
    if not original.exists():
        return None
    logo = Image.open(original).convert("RGBA")
    meta = _meta(logo)
    background = icon_background(row, meta)  # logo apagado na cor do topo: ícone com fundo branco
    meta["bg"] = background
    version = secrets.token_hex(4)
    header_logo = logo.copy()
    header_logo.thumbnail((480, 160))
    header_logo.save(folder / f"logo-{version}.png", optimize=True)
    fav = logo.copy()
    fav.thumbnail((64, 64))
    fav.save(folder / f"favicon-{version}.png", optimize=True)
    _square_icon(logo, 192, background).save(folder / f"icon-192-{version}.png", optimize=True)
    _square_icon(logo, 512, background).save(folder / f"icon-512-{version}.png", optimize=True)
    _square_icon(logo, 180, background, fill=0.7).save(folder / f"apple-{version}.png", optimize=True)
    _clean_old(keep=version)
    return version, meta


def _clean_old(keep):
    for path in brand_dir().glob("*.png"):
        if ASSET_NAME.match(path.name) and not path.name.endswith(f"-{keep}.png"):
            try:
                path.unlink()
            except OSError:
                pass


def save_logo(file_storage):
    """Guarda o logo enviado (como PNG). Devolve False se o arquivo não abriu como imagem."""
    image = _open_logo(file_storage)
    if image is None:
        return False
    folder = brand_dir()
    folder.mkdir(parents=True, exist_ok=True)
    image.save(folder / "logo-original.png", optimize=True)
    return True


def remove_logo():
    folder = brand_dir()
    for path in list(folder.glob("*.png")) if folder.exists() else []:
        try:
            path.unlink()
        except OSError:
            pass


@bp.route("/marca/<filename>")
def asset(filename):
    """Logo e ícones (públicos: aparecem até na tela de login e na página da cotação)."""
    if not ASSET_NAME.match(filename):
        abort(404)
    return send_from_directory(brand_dir(), filename, max_age=365 * 24 * 3600)
