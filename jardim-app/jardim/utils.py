"""Funções de apoio: datas, validações e filtros usados nos templates."""
import re
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import current_app, g

from . import i18n

WEEKDAYS_BY_LANG = {
    "pt_BR": ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"],
    "en": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
}
MONTHS_BY_LANG = {
    "pt_BR": ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"],
    "en": ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
}
STATUS_KEYS = ("scheduled", "in_progress", "done", "cancelled")  # valores no banco; o texto vem do i18n
STATUS_LABELS = {key: key for key in STATUS_KEYS}  # mantido só pra validar chaves (ver jobs.py)


# ---------- Datas e horas ----------

def tz():
    try:
        return ZoneInfo(current_app.config["TIMEZONE"])
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


def today_iso():
    return datetime.now(tz()).date().isoformat()


def now_utc_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_date(value):
    """'2026-09-21' -> date, ou None se for inválida."""
    if not value or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def valid_time(value):
    return bool(re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value or ""))


def valid_email(value):
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value or ""))


def safe_next(target):
    """Só aceita redirecionar para caminhos internos (evita 'open redirect'). Nada de espaço, tab ou quebra
    de linha: o navegador some com eles, e "/<tab>/site.com" viraria "//site.com"."""
    if not target or any(ord(ch) < 33 or ord(ch) == 127 for ch in target):
        return None
    if target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return None


# ---------- Filtros dos templates ----------

def _to_local(iso_value):
    try:
        moment = datetime.fromisoformat(iso_value)
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(tz())


def to_local(iso_value):
    """Horário salvo em UTC (ISO) no fuso do app, ou None."""
    return _to_local(iso_value)


def date_br(value):
    d = parse_date(value)
    return d.strftime("%d/%m/%Y") if d else ""


def weekday(value):
    d = parse_date(value)
    return WEEKDAYS_BY_LANG[g.get("lang", i18n.DEFAULT_LANGUAGE)][d.weekday()] if d else ""


def day_num(value):
    d = parse_date(value)
    return f"{d.day}" if d else ""


def month_abbr(value):
    d = parse_date(value)
    return MONTHS_BY_LANG[g.get("lang", i18n.DEFAULT_LANGUAGE)][d.month - 1] if d else ""


def date_long(value):
    d = parse_date(value)
    lang = g.get("lang", i18n.DEFAULT_LANGUAGE)
    return f"{WEEKDAYS_BY_LANG[lang][d.weekday()]}, {d.day} {MONTHS_BY_LANG[lang][d.month - 1]}" if d else ""


def dt_local(iso_value):
    moment = _to_local(iso_value)
    return f"{moment:%d/%m} {i18n.t('common.at')} {moment:%H:%M}" if moment else "—"


def time_local(iso_value):
    moment = _to_local(iso_value)
    return moment.strftime("%H:%M") if moment else "—"


_MONEY = re.compile(r"^\d{1,6}(?:[.,]\d{1,2})?$")


def parse_money(text):
    """'60', '60.5', '60,50' ou '£60' viram pence (6000, 6050...). Vazio ou zero: None. Inválido: ValueError.
    Vírgula ou ponto servem de separador dos centavos; '1,200' é recusado para não virar £1.20 sem querer."""
    raw = (text or "").strip().replace("£", "").replace(" ", "")
    if not raw:
        return None
    if not _MONEY.match(raw):
        raise ValueError(f"valor inválido: {text!r}")
    pounds, _, pence = raw.replace(",", ".").partition(".")
    return (int(pounds) * 100 + (int((pence + "0")[:2]) if pence else 0)) or None


def money(pence):
    """6000 → '£60'; 6050 → '£60.50'; 120000 → '£1,200'."""
    if not pence:
        return ""
    pounds, rest = divmod(int(pence), 100)
    return f"£{pounds:,}" if not rest else f"£{pounds:,}.{rest:02d}"


def money_plain(pence):
    """Para preencher o campo do formulário: 6050 → '60.50' (sem £ e sem separador de milhar)."""
    if not pence:
        return ""
    pounds, rest = divmod(int(pence), 100)
    return f"{pounds}" if not rest else f"{pounds}.{rest:02d}"


def local_date_iso(iso_value):
    """Data (no fuso do app) de um horário salvo em UTC, ex.: o dia em que o trabalho foi concluído."""
    moment = _to_local(iso_value)
    return moment.date().isoformat() if moment else None


def duration_minutes(start_iso, end_iso):
    start, end = _to_local(start_iso), _to_local(end_iso)
    if not start or not end or end < start:
        return None
    return int((end - start).total_seconds() // 60)


def format_minutes(total_minutes):
    hours, minutes = divmod(int(total_minutes), 60)
    return f"{hours}h{minutes:02d}" if hours else f"{minutes} min"


def duration(start_iso, end_iso):
    minutes = duration_minutes(start_iso, end_iso)
    return format_minutes(minutes) if minutes is not None else "—"


def period_bounds(period, ref_iso):
    """Primeiro e último dia do período (dia, semana de segunda a domingo, mês ou ano) que contém a data."""
    ref = date.fromisoformat(ref_iso)
    if period == "dia":
        return ref_iso, ref_iso
    if period == "mes":
        end = date(ref.year + (ref.month == 12), ref.month % 12 + 1, 1) - timedelta(days=1)
        return ref.replace(day=1).isoformat(), end.isoformat()
    if period == "ano":
        return ref.replace(month=1, day=1).isoformat(), ref.replace(month=12, day=31).isoformat()
    return week_bounds(ref_iso)


def week_bounds(today_iso_str):
    """Segunda a domingo da semana que contém a data dada."""
    d = date.fromisoformat(today_iso_str)
    monday = d - timedelta(days=d.weekday())
    return monday.isoformat(), (monday + timedelta(days=6)).isoformat()


def status_label(value):
    return i18n.t(f"common.status_{value}") if value in STATUS_KEYS else value


def tier_label(value):
    return i18n.t("common.tier_none") if not value else i18n.t(f"common.tier_{value}")


def intl_phone(phone):
    """Telefone internacional só com os dígitos (Reino Unido por padrão): '07700 900111' → '447700900111'.
    '+55 11 9...' e '0055...' ficam com o código do país que já têm. Número estranho: ''."""
    raw = (phone or "").strip()
    digits = re.sub(r"\D", "", raw)
    if raw.startswith("00"):
        digits = digits[2:]
    elif not raw.startswith("+") and digits.startswith("0"):
        digits = "44" + digits[1:]
    return digits if 10 <= len(digits) <= 15 else ""


def maps_link(address, postcode=""):
    query = f"{address or ''} {postcode or ''}".strip()
    return "https://www.google.com/maps/search/?api=1&query=" + quote_plus(query)


def init_app(app):
    for func in (date_br, weekday, day_num, month_abbr, date_long, dt_local,
                 time_local, duration, format_minutes, status_label, tier_label, maps_link,
                 money, money_plain):
        app.add_template_filter(func, func.__name__)
