"""Lembrete pro cliente na véspera do serviço: "amanhã a gente vai aí".

Três jeitos (Conta → Lembrete pro cliente, só o dono escolhe):
  off   desligado.
  tap   com um toque (grátis): a tela "Lembretes pra amanhã" lista os clientes do dia seguinte, e cada um
        tem o botão do WhatsApp e o do SMS, que abrem o celular com a mensagem pronta. A mensagem sai do
        número de quem tocou, e a resposta do cliente chega direto nele.
  sms   SMS automático pelo Twilio (pago por mensagem): sai sozinho a partir da hora escolhida (padrão 18h)
        e até as 21h, na primeira visita ao site nesse horário ou pelo link diário (cron-job.org).
Um lembrete por cliente por dia de serviço, mesmo com dois trabalhos no mesmo cliente (vale o horário
mais cedo). Cliente sem telefone, ou com o lembrete desligado no cadastro dele, fica de fora.

O Twilio é chamado direto pela API (sem biblioteca a mais). No PythonAnywhere grátis tudo passa por um
proxy, e o api.twilio.com está na lista liberada; o urllib usa o proxy sozinho (variáveis https_proxy).
"""
import base64
import json
import math
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

from flask import Blueprint, abort, current_app, flash, g, jsonify, redirect, render_template, request, url_for

from . import branding, i18n, utils
from .auth import permission_required
from .db import get_db

bp = Blueprint("reminders", __name__)

MODES = ("off", "tap", "sms")
HOURS = tuple(range(8, 21))       # a partir de que hora o SMS automático pode sair
LAST_HOUR = 21                    # depois das 21h não sai mais nada (ninguém quer SMS de noite)
RETRIES = 3                       # tentativas por lembrete quando o Twilio falha
CHECK_EVERY = timedelta(minutes=10)
STUCK_AFTER = timedelta(minutes=15)  # "saindo" há mais tempo que isso: o envio morreu no meio
TWILIO_URL = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
SID = re.compile(r"^AC[0-9a-fA-F]{32}$")
TOKEN = re.compile(r"^[0-9a-fA-F]{32}$")
E164 = re.compile(r"^\+[1-9]\d{7,14}$")
SENDER_NAME = re.compile(r"^(?=.*[A-Za-z])[A-Za-z0-9 ]{1,11}$")  # nome no lugar do número (só no envio)

WEEKDAYS = {"en": ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"),
            "pt_BR": ("segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo")}
MONTHS = {"en": ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
                 "November", "December"),
          "pt_BR": ("janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro",
                    "novembro", "dezembro")}
# Cabe num SMS só (160 letras) com nomes de tamanho normal. Sem acento de propósito na versão em inglês.
DEFAULT_TEXT = {
    "en": "Hi {client}, {company} here: see you tomorrow, {when}, for your garden. "
          "To change anything, call or text {phone}. Thanks!",
    "pt_BR": "Olá, {client}! Aqui é da {company}: amanhã, {when}, vamos cuidar do seu jardim. "
             "Pra mudar algo, ligue ou mande mensagem pro {phone}. Obrigado!",
}
# o dono pode escrever os campos em inglês ou em português
PLACEHOLDERS = {"client": "cliente", "when": "quando", "date": "dia", "time": "hora", "company": "empresa",
                "phone": "telefone"}


class SmsError(Exception):
    """O SMS não saiu. A mensagem já vem pronta pra mostrar pro dono. network=True: nem chegou no Twilio."""

    def __init__(self, message, network=False):
        super().__init__(message)
        self.network = network


def _now():
    """Agora, no fuso do app (separado pra os testes escolherem a hora)."""
    return datetime.now(utils.tz())


def _utc_iso(moment=None):
    return (moment or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds")


def settings():
    from .notifications import settings as load  # importado aqui: notifications chama este módulo
    return load()


def tomorrow():
    return (date.fromisoformat(utils.today_iso()) + timedelta(days=1)).isoformat()


# ---------- A mensagem ----------

def when_text(day, start_time, lang):
    """'Friday 27 September at 9:00' ou 'sexta-feira, 27 de setembro, às 9:00' (sem horário: só o dia)."""
    lang = lang if lang in WEEKDAYS else "en"
    d = date.fromisoformat(day)
    if lang == "pt_BR":
        text = f"{WEEKDAYS[lang][d.weekday()]}, {d.day} de {MONTHS[lang][d.month - 1]}"
    else:
        text = f"{WEEKDAYS[lang][d.weekday()]} {d.day} {MONTHS[lang][d.month - 1]}"
    time = _short_time(start_time)
    if time:
        text += f", às {time}" if lang == "pt_BR" else f" at {time}"
    return text


def _short_time(start_time):
    if not utils.valid_time(start_time or ""):
        return ""
    hour, minute = start_time.split(":")
    return f"{int(hour)}:{minute}"


def template_of(row):
    lang = row["reminder_language"] if row["reminder_language"] in DEFAULT_TEXT else "en"
    return (row["reminder_text"] or "").strip() or DEFAULT_TEXT[lang]


def values(row, client_name, day, start_time):
    lang = row["reminder_language"] if row["reminder_language"] in DEFAULT_TEXT else "en"
    owner = get_db().execute("SELECT phone FROM users WHERE role = 'owner' ORDER BY id LIMIT 1").fetchone()
    d = date.fromisoformat(day)
    day_text = (f"{d.day} de {MONTHS[lang][d.month - 1]}" if lang == "pt_BR" else f"{d.day} {MONTHS[lang][d.month - 1]}")
    return {"client": client_name, "when": when_text(day, start_time, lang), "date": day_text,
            "time": _short_time(start_time), "company": (row["company_name"] or branding.app_name()).strip(),
            "phone": (row["company_phone"] or (owner["phone"] if owner else "") or "").strip()}


def render(template, vals):
    text = template
    for key, pt_key in PLACEHOLDERS.items():
        text = text.replace("{" + key + "}", vals[key]).replace("{" + pt_key + "}", vals[key])
    return re.sub(r"[ \t]{2,}", " ", text).strip()


GSM7 = set("@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§"
           "¿abcdefghijklmnopqrstuvwxyzäöñüà")
GSM7_EXT = set("^{}\\[~]|€\f")


def sms_parts(text):
    """(quantos SMS, letras, tem_acento_especial). Sem acento especial cabem 160 letras por SMS; com
    (á, ã, ç, emoji...), só 70 — e a mensagem custa mais."""
    if all(ch in GSM7 or ch in GSM7_EXT for ch in text):
        size = sum(2 if ch in GSM7_EXT else 1 for ch in text)
        return (1 if size <= 160 else math.ceil(size / 153)), size, False
    size = len(text.encode("utf-16-le")) // 2
    return (1 if size <= 70 else math.ceil(size / 67)), size, True


# ---------- Quem recebe amanhã ----------

def clients_for(day):
    """Clientes com trabalho agendado no dia, com o horário mais cedo e como está o lembrete de cada um."""
    rows = get_db().execute(
        "SELECT c.id, c.name, c.phone, c.reminders, MIN(NULLIF(j.start_time, '')) AS first_time, "
        "COUNT(j.id) AS jobs, GROUP_CONCAT(DISTINCT j.title) AS titles, "
        "r.status, r.channel, r.detail, r.attempts, r.updated_at "
        "FROM jobs j JOIN clients c ON c.id = j.client_id "
        "LEFT JOIN client_reminders r ON r.client_id = c.id AND r.job_date = j.job_date "
        "WHERE j.job_date = ? AND j.status = 'scheduled' "
        "GROUP BY c.id ORDER BY COALESCE(MIN(NULLIF(j.start_time, '')), '99:99'), c.name COLLATE NOCASE",
        (day,)).fetchall()
    out = []
    for r in rows:
        item = dict(r)
        item["titles"] = (r["titles"] or "").replace(",", " · ")
        item["digits"] = utils.intl_phone(r["phone"])
        if not r["reminders"]:
            item["state"] = "off"
        elif not item["digits"]:
            item["state"] = "no_phone"
        elif r["status"] == "sent":
            item["state"] = "sent"
        elif r["status"] == "failed":
            item["state"] = "failed"
        elif r["status"] == "sending":
            item["state"] = "sending"
        else:
            item["state"] = "pending"
        out.append(item)
    # primeiro quem dá pra avisar (na ordem do dia); no fim, quem está sem telefone ou com o lembrete desligado
    out.sort(key=lambda c: {"no_phone": 1, "off": 2}.get(c["state"], 0))
    return out


def summary():
    """O card do Painel: quantos clientes amanhã e quantos já receberam (None = lembrete desligado)."""
    row = settings()
    if row["reminder_mode"] not in ("tap", "sms"):
        return None
    items = [c for c in clients_for(tomorrow()) if c["state"] not in ("off", "no_phone")]
    if not items:
        return None
    return {"total": len(items), "sent": sum(1 for c in items if c["state"] == "sent"),
            "failed": sum(1 for c in items if c["state"] == "failed"), "mode": row["reminder_mode"],
            "hour": row["reminder_hour"]}


# ---------- SMS pelo Twilio ----------

def sms_ready(row):
    return bool(SID.match(row["twilio_sid"] or "") and row["twilio_token"] and
                (E164.match(row["twilio_from"] or "") or SENDER_NAME.match(row["twilio_from"] or "")))


def _post(url, data, user, password):
    """POST com autenticação básica. Devolve (status HTTP, JSON da resposta). Separado pra os testes."""
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(), method="POST")
    req.add_header("Authorization", "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode())
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except ValueError:
            return exc.code, {}


# Os erros mais comuns do Twilio, explicados.
TWILIO_ERRORS = {20003: "reminders.err_login", 21608: "reminders.err_trial", 21211: "reminders.err_number",
                 21614: "reminders.err_number", 21612: "reminders.err_number", 21408: "reminders.err_country",
                 21606: "reminders.err_sender", 21212: "reminders.err_sender", 21660: "reminders.err_sender"}


def send_sms(row, to_digits, body):
    """Manda um SMS pelo Twilio. Devolve o id da mensagem; se não sair, levanta SmsError."""
    try:
        status, payload = _post(TWILIO_URL.format(sid=row["twilio_sid"]),
                                {"To": "+" + to_digits, "From": row["twilio_from"], "Body": body},
                                row["twilio_sid"], row["twilio_token"])
    except (urllib.error.URLError, OSError) as exc:  # sem internet, proxy fora do ar, demorou demais
        raise SmsError(i18n.t("reminders.err_network", error=getattr(exc, "reason", exc)), network=True) from exc
    if status >= 400 or not payload.get("sid"):
        key = TWILIO_ERRORS.get(payload.get("code"))
        raise SmsError(i18n.t(key) if key else (payload.get("message") or f"HTTP {status}"))
    return payload["sid"]


# ---------- Envio (automático e na mão) ----------

def _claim(client_id, day, channel, force=False):
    """Marca o lembrete como "saindo" antes de mandar, pra duas visitas ao mesmo tempo não mandarem em dobro.
    Sem force, só manda o que ainda não saiu (ou falhou e ainda tem tentativa)."""
    db = get_db()
    now = _utc_iso()
    cur = db.execute("INSERT OR IGNORE INTO client_reminders (client_id, job_date, channel, status, attempts, sent_by, "
                     "updated_at) VALUES (?, ?, ?, 'sending', 1, ?, ?)",
                     (client_id, day, channel, g.user["id"] if g.get("user") else None, now))
    if not cur.rowcount:
        stuck = _utc_iso(datetime.now(timezone.utc) - STUCK_AFTER)
        rule = ("status != 'sending' OR updated_at < ?" if force
                else "(status = 'failed' AND attempts < ?) OR (status = 'sending' AND updated_at < ?)")
        params = (stuck,) if force else (RETRIES, stuck)
        cur = db.execute(f"UPDATE client_reminders SET status = 'sending', channel = ?, attempts = attempts + 1, "
                         f"detail = '', sent_by = ?, updated_at = ? WHERE client_id = ? AND job_date = ? AND ({rule})",
                         (channel, g.user["id"] if g.get("user") else None, now, client_id, day, *params))
    db.commit()
    return cur.rowcount == 1


def _finish(client_id, day, status, detail):
    db = get_db()
    db.execute("UPDATE client_reminders SET status = ?, detail = ?, updated_at = ? WHERE client_id = ? AND job_date = ?",
               (status, str(detail)[:300], _utc_iso(), client_id, day))
    db.commit()


def send_one(row, client, day, force=False):
    """Manda o SMS de um cliente. Devolve True se saiu, False se falhou, None se não era pra mandar.
    Sem internet (ou o Twilio fora do ar), levanta SmsError(network=True) depois de anotar a falha."""
    if not _claim(client["id"], day, "sms", force=force):
        return None
    body = render(template_of(row), values(row, client["name"], day, client["first_time"]))
    try:
        sid = send_sms(row, client["digits"], body)
    except SmsError as exc:
        _finish(client["id"], day, "failed", exc)
        if exc.network:
            raise
        return False
    _finish(client["id"], day, "sent", sid)
    return True


def due(row=None):
    """Hora de rodar o SMS automático? (no máximo a cada 10 min, entre a hora escolhida e as 21h)"""
    row = row if row is not None else settings()
    if row["reminder_mode"] != "sms" or not sms_ready(row):
        return False
    now = _now()
    if not (int(row["reminder_hour"] or 18) <= now.hour < LAST_HOUR):
        return False
    last = row["reminder_checked_at"]
    return not last or last < _utc_iso(datetime.now(timezone.utc) - CHECK_EVERY)


def run(force=False):
    """Manda os SMS de amanhã que ainda não saíram. Devolve quantos saíram."""
    row = settings()
    if not force and not due(row):
        return 0
    if row["reminder_mode"] != "sms" or not sms_ready(row):
        return 0
    if not (int(row["reminder_hour"] or 18) <= _now().hour < LAST_HOUR):
        return 0  # nem o link diário manda fora do horário
    db = get_db()
    now = _utc_iso()
    cur = db.execute("UPDATE settings SET reminder_checked_at = ? WHERE id = 1 AND (reminder_checked_at = '' "
                     "OR reminder_checked_at < ?)", (now, _utc_iso(datetime.now(timezone.utc) - CHECK_EVERY)))
    db.commit()
    if not cur.rowcount and not force:
        return 0  # outra visita já está cuidando disso
    db.execute("DELETE FROM client_reminders WHERE job_date < ?",  # lembretes de mais de 2 meses não servem pra nada
               ((date.fromisoformat(utils.today_iso()) - timedelta(days=60)).isoformat(),))
    db.commit()
    day, sent = tomorrow(), 0
    for client in clients_for(day):
        if client["state"] in ("pending", "failed", "sending"):
            try:
                sent += bool(send_one(row, client, day))
            except SmsError:
                break  # sem internet: nem tenta os outros agora (daqui a 10 minutos tenta de novo)
    return sent


@bp.after_app_request
def _after_response(response):
    """Depois que a página já foi entregue, se estiver no horário, manda os SMS de amanhã."""
    if request.endpoint in ("static", "manifest", "branding.asset", "notifications.daily_link") or response.status_code >= 400:
        return response
    try:
        row = branding._settings()
        if row is None or not due(row):
            return response
    except Exception:  # banco ainda sem as colunas novas (antes do Reload) etc.: o site segue normal
        return response
    app, base_url = current_app._get_current_object(), request.host_url

    def work():
        with app.test_request_context("/", base_url=base_url):
            g.lang = settings()["language"]
            try:
                run()
            except Exception:
                app.logger.exception("Falha nos lembretes pro cliente")

    response.call_on_close(work)
    return response


# ---------- A tela "Lembretes pra amanhã" ----------

def _links(row, client, day):
    text = render(template_of(row), values(row, client["name"], day, client["first_time"]))
    quoted = urllib.parse.quote(text)
    return {"text": text, "whatsapp": f"https://wa.me/{client['digits']}?text={quoted}",
            "sms": f"sms:+{client['digits']}?&body={quoted}"}


@bp.route("/lembretes")
@permission_required("schedule")
def tomorrow_list():
    row = settings()
    day = tomorrow()
    clients = clients_for(day)
    for c in clients:
        if c["digits"]:
            c.update(_links(row, c, day))
        c["when_sent"] = utils.time_local(c["updated_at"]) if c["updated_at"] else ""
    return render_template("reminders.html", day=day, clients=clients, mode=row["reminder_mode"],
                           hour=row["reminder_hour"], sms_ready=sms_ready(row))


def _client_for(client_id, day):
    if day != tomorrow() and day != utils.today_iso():
        abort(404)
    client = next((c for c in clients_for(day) if c["id"] == client_id), None)
    if client is None:
        abort(404)
    return client


@bp.route("/lembretes/<int:client_id>/<day>/feito", methods=("POST",))
@permission_required("schedule")
def mark_done(client_id, day):
    """Tocou no WhatsApp ou no SMS (ou marcou na mão): anota que o lembrete foi."""
    client = _client_for(client_id, day)
    channel = request.form.get("channel", "manual")
    if channel not in ("whatsapp", "sms_phone", "manual"):
        channel = "manual"
    db = get_db()
    db.execute("INSERT INTO client_reminders (client_id, job_date, channel, status, attempts, sent_by, updated_at) "
               "VALUES (?, ?, ?, 'sent', 1, ?, ?) ON CONFLICT (client_id, job_date) DO UPDATE SET "
               "channel = excluded.channel, status = 'sent', detail = '', sent_by = excluded.sent_by, "
               "updated_at = excluded.updated_at",
               (client["id"], day, channel, g.user["id"], _utc_iso()))
    db.commit()
    if request.accept_mimetypes.best == "application/json":
        return jsonify(ok=True, label=i18n.t(f"reminders.via_{channel}"), time=utils.time_local(_utc_iso()))
    flash(i18n.t("reminders.marked", name=client["name"]), "ok")
    return redirect(url_for("reminders.tomorrow_list"))


@bp.route("/lembretes/<int:client_id>/<day>/desfazer", methods=("POST",))
@permission_required("schedule")
def undo(client_id, day):
    client = _client_for(client_id, day)
    db = get_db()
    db.execute("DELETE FROM client_reminders WHERE client_id = ? AND job_date = ? AND status != 'sending'", (client["id"], day))
    db.commit()
    return redirect(url_for("reminders.tomorrow_list"))


@bp.route("/lembretes/<int:client_id>/<day>/sms", methods=("POST",))
@permission_required("schedule")
def send_now(client_id, day):
    """Manda o SMS automático agora (pra quem falhou, ou pra mandar de novo)."""
    client = _client_for(client_id, day)
    row = settings()
    if not sms_ready(row):
        flash(i18n.t("reminders.sms_not_ready"), "error")
    elif not client["digits"]:
        flash(i18n.t("reminders.state_no_phone"), "error")
    else:
        try:
            result = send_one(row, client, day, force=True)
        except SmsError:
            result = False
        if result:
            flash(i18n.t("reminders.sms_sent", name=client["name"]), "ok")
        elif result is None:
            flash(i18n.t("reminders.state_sending"), "info")
        else:
            failed = get_db().execute("SELECT detail FROM client_reminders WHERE client_id = ? AND job_date = ?",
                                      (client["id"], day)).fetchone()
            flash(i18n.t("reminders.sms_failed", name=client["name"], error=failed["detail"] if failed else ""), "error")
    return redirect(url_for("reminders.tomorrow_list"))
