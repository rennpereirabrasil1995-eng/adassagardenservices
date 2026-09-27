"""PDF do relatório da empresa (feito com reportlab), com as mesmas contas e gráficos da tela.

As fontes são as mesmas do app (Atkinson Hyperlegible, pasta fonts/). O que a fonte não tem
(emoji, letras de outros alfabetos) é trocado por uma letra parecida ou '?', pra nada sair quebrado.
"""
import unicodedata
from datetime import datetime
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.graphics.shapes import Drawing, Line, Rect, String
from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from . import i18n, utils

FONT_DIR = Path(__file__).resolve().parent / "fonts"
INK, MUTED = colors.HexColor("#12301f"), colors.HexColor("#4d5d53")
LINE, SOFT = colors.HexColor("#d5dcd2"), colors.HexColor("#eef1ec")
OK, LATE = colors.HexColor("#2f7d46"), colors.HexColor("#a8324a")
MARKER, NEUTRAL = colors.HexColor("#f4c430"), colors.HexColor("#8fa093")
MARGIN = 18 * mm
WIDTH = A4[0] - 2 * MARGIN  # largura útil da página


def _fonts():
    if "Atkinson" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("Atkinson", str(FONT_DIR / "AtkinsonHyperlegible-Regular.ttf")))
        pdfmetrics.registerFont(TTFont("Atkinson-Bold", str(FONT_DIR / "AtkinsonHyperlegible-Bold.ttf")))
        pdfmetrics.registerFontFamily("Atkinson", normal="Atkinson", bold="Atkinson-Bold",
                                      italic="Atkinson", boldItalic="Atkinson-Bold")


def _clean(text):
    glyphs = pdfmetrics.getFont("Atkinson").face.charToGlyph
    out = []
    for ch in str(text if text is not None else ""):
        if ch == "\n" or ord(ch) in glyphs:
            out.append(ch)
        else:
            base = unicodedata.normalize("NFKD", ch)[:1]
            out.append(base if base and ord(base) in glyphs else "?")
    return "".join(out)


def _style(name, size=10, bold=False, color=INK, **kw):
    return ParagraphStyle(name, fontName="Atkinson-Bold" if bold else "Atkinson", fontSize=size,
                          leading=round(size * 1.3, 1), textColor=color, **kw)


def _p(text, style):
    return Paragraph(escape(_clean(text)), style)


def _clip(text, width, font="Atkinson", size=9):
    text = _clean(text)
    while text and pdfmetrics.stringWidth(text, font, size) > width:
        text = text[:-2] + "…"
    return text


def _table(rows, widths, num_cols=(), header=True, total=False):
    tbl = Table(rows, colWidths=widths, repeatRows=1 if header else 0)
    style = [("FONT", (0, 0), (-1, -1), "Atkinson", 9.5), ("TEXTCOLOR", (0, 0), (-1, -1), INK),
             ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBELOW", (0, 0), (-1, -1), 0.5, LINE),
             ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
             ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]
    if header:
        style += [("FONT", (0, 0), (-1, 0), "Atkinson-Bold", 8.5), ("TEXTCOLOR", (0, 0), (-1, 0), MUTED)]
    if total:
        style += [("FONT", (0, -1), (-1, -1), "Atkinson-Bold", 9.5), ("LINEBELOW", (0, -1), (-1, -1), 0, colors.white)]
    style += [("ALIGN", (c, 0), (c, -1), "RIGHT") for c in num_cols]
    tbl.setStyle(TableStyle(style))
    return tbl


def _legend(items, drawing, y):
    """Quadradinho colorido + texto (a cor nunca é a única informação)."""
    x = 0
    for color, text in items:
        text = _clean(text)
        drawing.add(Rect(x, y, 3 * mm, 3 * mm, fillColor=color, strokeColor=None))
        drawing.add(String(x + 4.5 * mm, y + 0.5 * mm, text, fontName="Atkinson", fontSize=8.5, fillColor=INK))
        x += 4.5 * mm + pdfmetrics.stringWidth(text, "Atkinson", 8.5) + 7 * mm


def _timeline(bars, t, legend=None):
    height = 52 * mm
    d = Drawing(WIDTH, height)
    n = len(bars)
    gap = 1.5 if n > 12 else 6
    bw = (WIDTH - gap * (n - 1)) / n
    base, usable = 16 * mm, height - 16 * mm - 5 * mm
    peak = max((b["total"] for b in bars), default=0) or 1
    for i, b in enumerate(bars):
        x = i * (bw + gap)
        h_done, h_all = usable * b["done"] / peak, usable * b["total"] / peak
        if b["done"]:
            d.add(Rect(x, base, bw, h_done, fillColor=OK, strokeColor=None))
        if b["total"] > b["done"]:
            d.add(Rect(x, base + h_done, bw, h_all - h_done, fillColor=NEUTRAL, strokeColor=None))
        if b["label"]:
            d.add(String(x + bw / 2, base - 4 * mm, _clean(b["label"]), fontName="Atkinson", fontSize=7.5,
                         fillColor=MUTED, textAnchor="middle"))
        if b["total"] and n <= 12:
            d.add(String(x + bw / 2, base + h_all + 1.2 * mm, str(b["total"]), fontName="Atkinson-Bold",
                         fontSize=8, fillColor=INK, textAnchor="middle"))
    d.add(Line(0, base, WIDTH, base, strokeColor=NEUTRAL, strokeWidth=0.6))
    _legend(legend or [(OK, t("company.legend_done")), (NEUTRAL, t("company.legend_open"))], d, 0)
    return d


def _hours(people, peak):
    row_h, name_w, val_w = 7 * mm, 45 * mm, 18 * mm
    track_w = WIDTH - name_w - val_w
    d = Drawing(WIDTH, row_h * len(people))
    for i, p in enumerate(people):
        y = (len(people) - 1 - i) * row_h + 2 * mm
        d.add(String(0, y + 0.3 * mm, _clip(p["name"], name_w - 3 * mm), fontName="Atkinson-Bold", fontSize=9, fillColor=INK))
        d.add(Rect(name_w, y, track_w, 3.2 * mm, fillColor=LINE, strokeColor=None, rx=1.6 * mm, ry=1.6 * mm))
        if peak and p["minutes"]:
            d.add(Rect(name_w, y, max(track_w * p["minutes"] / peak, 3.2 * mm), 3.2 * mm,
                       fillColor=OK, strokeColor=None, rx=1.6 * mm, ry=1.6 * mm))
        d.add(String(WIDTH, y + 0.3 * mm, utils.format_minutes(p["minutes"]), fontName="Atkinson", fontSize=9,
                     fillColor=MUTED, textAnchor="end"))
    return d


def _stack(parts):
    """Uma barra dividida em partes, com legenda (valor e porcentagem)."""
    total = sum(value for _, value, _, _ in parts)
    d = Drawing(WIDTH, 13 * mm)
    y, h, x = 8 * mm, 3.6 * mm, 0
    d.add(Rect(0, y, WIDTH, h, fillColor=LINE, strokeColor=None))
    for color, value, _, _ in parts:
        if value and total:
            w = WIDTH * value / total
            d.add(Rect(x, y, w, h, fillColor=color, strokeColor=None))
            x += w
    _legend([(color, f"{label}: {shown}" + (f" ({round(value * 100 / total)}%)" if total else ""))
             for color, value, label, shown in parts], d, 1 * mm)
    return d


def build(data, company):
    """Devolve os bytes do PDF. Precisa rodar dentro de um pedido (usa o idioma atual do app)."""
    _fonts()
    t = i18n.t
    h2 = _style("h2", 13.5, bold=True, spaceBefore=12, spaceAfter=6)
    body, small = _style("body", 9.5), _style("small", 8.5, color=MUTED)
    num = _style("num", 9.5, alignment=TA_RIGHT)
    now = datetime.now(utils.tz()).strftime("%d/%m/%Y %H:%M")
    story = [
        _p(company, _style("company", 10, bold=True, color=MUTED)),
        _p(t("company.page_title"), _style("title", 22, bold=True, spaceBefore=2)),
        _p(data["period_label"], _style("sub", 12, color=MUTED, spaceBefore=2)),
        _p(t("pdf.generated", when=now), small),
        Spacer(0, 6 * mm),
    ]

    # resumo: quatro números
    stats = [(str(data["done_count"]), t("company.jobs_done"), t("company.of_scheduled", n=data["scheduled_count"])),
             (utils.format_minutes(data["total_minutes"]), t("company.hours"), ""),
             (utils.money(data["cash_total"]) or "£0", t("company.cash_total"), ""),
             (str(data["clients_served"]), t("company.clients_served"), "")]
    cells = [[[_p(v, _style("stat", 19, bold=True)), _p(label, _style("statl", 9, bold=True)), _p(extra, small)]
              for v, label, extra in stats]]
    summary = Table(cells, colWidths=[WIDTH / 4] * 4)
    summary.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), SOFT), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                                 ("LINEAFTER", (0, 0), (-2, -1), 4, colors.white),
                                 ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                                 ("LEFTPADDING", (0, 0), (-1, -1), 8)]))
    story.append(summary)

    if data["timeline"]:
        title = t("company.chart_jobs_month") if data["period"] == "ano" else t("company.chart_jobs_day")
        story.append(KeepTogether([_p(title, h2), _timeline(data["timeline"], t)]))

    # por pessoa
    block = [_p(t("company.by_employee"), h2)]
    if data["people"]:
        if data["max_minutes"]:
            block += [_hours(data["people"], data["max_minutes"]), Spacer(0, 3 * mm)]
        rows = [[t("company.col_person"), t("company.col_jobs"), t("company.col_hours"), t("company.col_cash")]]
        rows += [[_p(p["name"], body), str(p["done"]), utils.format_minutes(p["minutes"]), utils.money(p["cash"]) or "—"]
                 for p in data["people"]]
        many = len(data["people"]) > 1
        if many:
            rows.append([t("company.total"), str(data["done_count"]), utils.format_minutes(data["total_minutes"]),
                         utils.money(data["cash_total"]) or "—"])
        block.append(_table([[_clean(c) if isinstance(c, str) else c for c in r] for r in rows],
                            [WIDTH - 90 * mm, 25 * mm, 30 * mm, 35 * mm], num_cols=(1, 2, 3), total=many))
        if data["has_share"]:
            block += [Spacer(0, 1.5 * mm), _p(t("company.share_note"), small)]
    else:
        block.append(_p(t("company.no_jobs_period"), small))
    story.append(KeepTogether(block[:3]))
    story += block[3:]

    # dinheiro recebido, trabalho por trabalho
    story.append(_p(t("company.cash_section"), h2))
    if data["cash_jobs"]:
        pending = data["cash_total"] - data["cash_received"]
        story += [_stack([(OK, data["cash_received"], t("company.legend_received"), utils.money(data["cash_received"]) or "£0"),
                          (MARKER, pending, t("company.legend_pending"), utils.money(pending) or "£0")]), Spacer(0, 2 * mm)]
        rows = [[t("company.col_date"), t("company.col_client"), t("company.col_person"), t("company.col_cash"), t("pdf.status")]]
        for j in data["cash_jobs"]:
            status = (t("pdf.status_received", when=utils.dt_local(j["cash_received_at"]).split(" ")[0])
                      if j["cash_received_at"] else t("pdf.status_pending"))
            rows.append([f"{utils.day_num(j['job_date'])} {utils.month_abbr(j['job_date'])}", _p(j["client_name"], body),
                         _p(j["cash_name"] or "—", body), utils.money(j["cash_pence"]), _p(status, small)])
        story.append(_table([[_clean(c) if isinstance(c, str) else c for c in r] for r in rows],
                            [20 * mm, WIDTH - 118 * mm, 38 * mm, 25 * mm, 35 * mm], num_cols=(3,)))
    else:
        story.append(_p(t("company.no_cash"), small))

    if data["pending"]:  # retrato de hoje: quanto cada um ainda tem pra repassar, de qualquer data
        totals = {}
        for j in data["pending"]:
            totals[j["cash_name"]] = totals.get(j["cash_name"], 0) + j["cash_pence"]
        rows = [[_p(name, body), utils.money(total)] for name, total in totals.items()]
        story.append(KeepTogether([_p(t("pdf.pending_now"), h2),
                                   _table(rows, [WIDTH - 35 * mm, 35 * mm], num_cols=(1,), header=False)]))

    # trabalhos e tarefas
    tk = data["tasks"]
    block = [_p(t("company.jobs_section"), h2)]
    if tk["done"] + tk["skipped"] + tk["unmarked"]:
        block += [_stack([(OK, tk["done"], t("company.legend_tasks_done"), str(tk["done"])),
                          (LATE, tk["skipped"], t("company.legend_tasks_skipped"), str(tk["skipped"])),
                          (NEUTRAL, tk["unmarked"], t("company.legend_tasks_unmarked"), str(tk["unmarked"]))]),
                  Spacer(0, 2 * mm)]
    facts = [(t("company.jobs_scheduled"), data["scheduled_count"]), (t("company.jobs_done_label"), data["done_count"]),
             (t("company.jobs_cancelled"), data["cancelled_count"]), (t("company.jobs_open"), data["open_count"]),
             (t("company.tasks_done"), tk["done"]), (t("company.tasks_not_done"), tk["skipped"]),
             (t("company.tasks_unmarked"), tk["unmarked"]), (t("company.jobs_late_now"), data["late_now"])]
    block.append(_table([[_clean(a), str(b)] for a, b in facts], [WIDTH - 30 * mm, 30 * mm], num_cols=(1,), header=False))
    story.append(KeepTogether(block))

    # clientes
    tiers = ", ".join(f"{utils.tier_label(k)}: {v}" for k, v in data["tiers"].items() if v)
    facts = [(t("company.clients_active"), f"{data['clients_active']}" + (f"  ({tiers})" if tiers else "")),
             (t("company.clients_inactive"), data["clients_inactive"]), (t("company.clients_new"), data["clients_new"])]
    story.append(KeepTogether([_p(t("company.clients_section"), h2),
                               _table([[_clean(a), _p(str(b), num)] for a, b in facts], [WIDTH - 90 * mm, 90 * mm],
                                      header=False)]))

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Atkinson", 8)
        canvas.setFillColor(MUTED)
        canvas.drawString(MARGIN, 10 * mm, _clean(f"{company} · {data['period_label']}"))
        canvas.drawRightString(A4[0] - MARGIN, 10 * mm, _clean(t("pdf.page", n=doc.page)))
        canvas.restoreState()

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=MARGIN, rightMargin=MARGIN, topMargin=16 * mm,
                            bottomMargin=18 * mm, title=_clean(f"{t('company.page_title')} – {data['period_label']}"),
                            author=_clean(company))
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()


def build_company(data, company, app_name):
    """O relatório da área da empresa em PDF: só o que ela vê na tela (portal.report_data)."""
    _fonts()
    t = i18n.t
    h2 = _style("h2", 13.5, bold=True, spaceBefore=12, spaceAfter=6)
    body, small = _style("body", 9.5), _style("small", 8.5, color=MUTED)
    now = datetime.now(utils.tz()).strftime("%d/%m/%Y %H:%M")
    story = [
        _p(f"{company} · {app_name}", _style("company", 10, bold=True, color=MUTED)),
        _p(t("portal.report_title"), _style("title", 22, bold=True, spaceBefore=2)),
        _p(data["period_label"], _style("sub", 12, color=MUTED, spaceBefore=2)),
        _p(t("pdf.generated", when=now), small),
        Spacer(0, 6 * mm),
    ]
    n = data["services_count"]
    stats = [(str(n), t("portal.stat_services_one") if n == 1 else t("portal.stat_services"), ""),
             (utils.format_minutes(data["total_minutes"]) if data["total_minutes"] else "—", t("portal.stat_time"), ""),
             (str(data["gardens_served"]),
              t("portal.stat_gardens_one") if data["gardens_served"] == 1 else t("portal.stat_gardens"),
              t("portal.of_gardens", n=data["gardens_total"])),
             (str(data["tasks_done"]), t("portal.stat_tasks_one") if data["tasks_done"] == 1 else t("portal.stat_tasks"),
              (t("portal.not_done_one") if data["tasks_not_done"] == 1 else t("portal.not_done_many", n=data["tasks_not_done"]))
              if data["tasks_not_done"] else "")]
    cells = [[[_p(v, _style("stat", 19, bold=True)), _p(label, _style("statl", 9, bold=True)), _p(extra, small)]
              for v, label, extra in stats]]
    summary = Table(cells, colWidths=[WIDTH / 4] * 4)
    summary.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), SOFT), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                                 ("LINEAFTER", (0, 0), (-2, -1), 4, colors.white),
                                 ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                                 ("LEFTPADDING", (0, 0), (-1, -1), 8)]))
    story.append(summary)

    if not n:
        story.append(_p(t("portal.report_empty"), _style("empty", 10, color=MUTED, spaceBefore=10)))
    else:
        title = t("portal.chart_services_month") if data["period"] == "ano" else t("portal.chart_services_day")
        bars = [dict(b, done=b["total"]) for b in data["bars"]]  # tudo concluído: coluna inteira verde
        story.append(KeepTogether([_p(title, h2), _timeline(bars, t, legend=[(OK, t("portal.col_services"))])]))

        # por jardim
        block = [_p(t("portal.by_garden"), h2)]
        if len(data["gardens"]) > 1 and data["max_minutes"]:
            block += [_hours(data["gardens"], data["max_minutes"]), Spacer(0, 3 * mm)]
        rows = [[t("portal.col_garden"), t("portal.col_services"), t("portal.col_time"), t("portal.col_tasks_done"),
                 t("portal.col_not_done"), t("portal.col_photos")]]
        rows += [[_p(gd["name"], body), str(gd["services"]), utils.format_minutes(gd["minutes"]) if gd["minutes"] else "—",
                  str(gd["done"]), str(gd["not_done"] or "—"), str(gd["photos"] or "—")] for gd in data["gardens"]]
        many = len(data["gardens"]) > 1
        if many:
            rows.append([t("company.total"), str(n), utils.format_minutes(data["total_minutes"]) if data["total_minutes"] else "—",
                         str(data["tasks_done"]), str(data["tasks_not_done"] or "—"), str(data["photos"] or "—")])
        block.append(_table([[_clean(c) if isinstance(c, str) else c for c in r] for r in rows],
                            [WIDTH - 112 * mm, 20 * mm, 26 * mm, 24 * mm, 22 * mm, 20 * mm], num_cols=(1, 2, 3, 4, 5), total=many))
        story.append(KeepTogether(block[:3]))
        story += block[3:]

        # tarefas
        block = [_p(t("portal.tasks_section"), h2)]
        if data["tasks_done"] + data["tasks_not_done"]:
            block += [_stack([(OK, data["tasks_done"], t("company.legend_tasks_done"), str(data["tasks_done"])),
                              (LATE, data["tasks_not_done"], t("company.legend_tasks_skipped"), str(data["tasks_not_done"]))]),
                      Spacer(0, 2 * mm)]
        if data["not_done"]:
            rows = [[t("portal.col_date"), t("portal.col_garden"), t("portal.col_task"), t("portal.col_note")]]
            rows += [[f"{utils.day_num(x['date'])} {utils.month_abbr(x['date'])}", _p(x["garden"], body), _p(x["text"], body),
                      _p(x["note"] or "—", small)] for x in data["not_done"]]
            block.append(_table([[_clean(c) if isinstance(c, str) else c for c in r] for r in rows],
                                [20 * mm, 45 * mm, WIDTH - 125 * mm, 60 * mm]))
            if data["not_done_total"] > len(data["not_done"]):
                block += [Spacer(0, 1.5 * mm),
                          _p(t("portal.not_done_more", n=len(data["not_done"]), total=data["not_done_total"]), small)]
        else:
            block.append(_p(t("portal.not_done_none"), small))
        story.append(KeepTogether(block[:3]))
        story += block[3:]

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Atkinson", 8)
        canvas.setFillColor(MUTED)
        canvas.drawString(MARGIN, 10 * mm, _clean(f"{company} · {data['period_label']}"))
        canvas.drawRightString(A4[0] - MARGIN, 10 * mm, _clean(t("pdf.page", n=doc.page)))
        canvas.restoreState()

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=MARGIN, rightMargin=MARGIN, topMargin=16 * mm,
                            bottomMargin=18 * mm, title=_clean(f"{t('portal.report_title')} – {company} – {data['period_label']}"),
                            author=_clean(app_name))
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()
