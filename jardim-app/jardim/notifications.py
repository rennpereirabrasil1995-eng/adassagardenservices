"""Avisos automáticos para a equipe: dentro do app (o sininho no topo) e, se o
dono ligar em Conta → Avisos por e-mail, também por e-mail (Gmail).

O que gera aviso, sem o dono precisar configurar nada a cada trabalho (num trabalho "Share",
com várias pessoas, cada uma recebe o seu):
  - trabalho agendado para a pessoa (ou ela foi colocada nele)
  - trabalho remarcado, cancelado, excluído, ou a pessoa foi tirada dele
  - várias repetições criadas de uma vez (um aviso só, não um por cópia)
  - lembrete na véspera de cada trabalho

Por que não usamos "tarefa agendada": contas grátis do PythonAnywhere criadas a
partir de 15/01/2026 não têm esse recurso. Então:
  - o lembrete da véspera aparece no app na primeira visita de alguém logado no dia
    (não duplica: um lembrete por trabalho);
  - os e-mails de lembrete saem uma vez por dia, na primeira visita ao site depois
    das 17h (DAILY_EMAIL_HOUR), ou na hora certa todo dia se o "link diário" da tela
    Conta → Avisos por e-mail for cadastrado num serviço grátis como o cron-job.org.
E-mails são enviados DEPOIS que a página já foi entregue, então ninguém espera por eles.
"""
import hmac
import json
import secrets
import smtplib
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage

from flask import Blueprint, abort, current_app, g, render_template, request, url_for

from . import branding, i18n, utils
from .auth import login_required
from .db import get_db

bp = Blueprint("notifications", __name__, url_prefix="/avisos")

JOB_TOMORROW = "job_tomorrow"        # lembrete da véspera (um por trabalho)
JOB_ASSIGNED = "job_assigned"        # trabalho novo para a pessoa (ou passado para ela)
JOB_RESCHEDULED = "job_rescheduled"  # mudou a data ou o horário
JOB_UNASSIGNED = "job_unassigned"    # a pessoa foi tirada do trabalho
JOB_CANCELLED = "job_cancelled"
JOB_DELETED = "job_deleted"
JOBS_REPEATED = "jobs_repeated"      # várias cópias criadas de uma vez
QUOTE_VIEWED = "quote_viewed"        # o cliente abriu a cotação pela primeira vez
QUOTE_ACCEPTED = "quote_accepted"
QUOTE_DECLINED = "quote_declined"
QUOTE_KINDS = (QUOTE_VIEWED, QUOTE_ACCEPTED, QUOTE_DECLINED)
COMPANY_COMMENT = "company_comment"  # alguém da empresa comentou um serviço (área da empresa, portal.py)
OPEN_STATUSES = ("scheduled", "in_progress")
EMAIL_MAX_AGE = timedelta(days=2)    # aviso que não saiu por e-mail em 2 dias não sai mais
RECENT_NOTICE = timedelta(hours=20)  # quem acabou de ser avisado do trabalho não precisa de lembrete também
KEEP_READ_DAYS = 90                  # avisos lidos mais velhos que isso são apagados

_SELECT = (
    "SELECT n.*, u.email AS user_email, u.language AS user_language, j.job_date, j.start_time, j.title AS job_title, "
    "c.name AS client_name "
    "FROM notifications n JOIN users u ON u.id = n.user_id "
    "LEFT JOIN jobs j ON j.id = n.job_id LEFT JOIN clients c ON c.id = j.client_id "
)


def _utc_iso(moment):
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")  # mesmo formato de utils.now_utc_iso


# ---------- Configurações ----------

def settings():
    """A linha única de configurações (cria com os valores padrão se ainda não existir)."""
    db = get_db()
    row = db.execute("SELECT * FROM settings WHERE id = 1").fetchone()
    if row is None:
        db.execute("INSERT OR IGNORE INTO settings (id) VALUES (1)")
        db.commit()
        row = db.execute("SELECT * FROM settings WHERE id = 1").fetchone()
    return row


def email_enabled(row=None):
    row = row if row is not None else settings()
    return bool(row["notify_email"] and row["mail_address"] and row["mail_password"])


def ensure_cron_key():
    row = settings()
    if row["cron_key"]:
        return row["cron_key"]
    key = secrets.token_urlsafe(18)
    db = get_db()
    db.execute("UPDATE settings SET cron_key = ? WHERE id = 1", (key,))
    db.commit()
    return key


# ---------- Criar avisos ----------

def notify(user_id, kind, job_id=None, quote_id=None, **params):
    """Registra um aviso (o commit fica com quem chamou, junto com a mudança do trabalho)."""
    if not user_id:
        return None
    if g.get("user") is not None and user_id == g.user["id"]:
        return None  # ninguém precisa ser avisado do que ele mesmo fez
    db = get_db()
    if db.execute("SELECT 1 FROM users WHERE id = ? AND active = 1", (user_id,)).fetchone() is None:
        return None
    cur = db.execute(
        "INSERT INTO notifications (user_id, kind, job_id, quote_id, params, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, kind, job_id, quote_id, json.dumps(params, ensure_ascii=False), utils.now_utc_iso()),
    )
    g.setdefault("new_notifications", []).append(cur.lastrowid)
    return cur.lastrowid


def _snapshot(job):
    """Como o trabalho estava na hora do aviso (vale mesmo se ele mudar ou sumir depois)."""
    return {"client": job["client_name"], "title": job["title"], "date": job["job_date"], "time": job["start_time"]}


def _current(job_id):
    return get_db().execute(
        "SELECT j.*, c.name AS client_name FROM jobs j JOIN clients c ON c.id = j.client_id WHERE j.id = ?",
        (job_id,),
    ).fetchone()


def _people(job_id):
    """Quem está escalado no trabalho (ids), na ordem em que foi gravado."""
    return [r[0] for r in get_db().execute("SELECT user_id FROM job_assignees WHERE job_id = ?", (job_id,))]


def _drop_reminders(job_id, user_ids=None):
    """O lembrete de um trabalho que mudou perde o sentido; se ainda couber, um novo é criado sozinho.
    Com user_ids, só o dessas pessoas (ex.: quem saiu do trabalho)."""
    sql, args = "DELETE FROM notifications WHERE job_id = ? AND kind = ?", [job_id, JOB_TOMORROW]
    if user_ids is not None:
        if not user_ids:
            return
        sql += f" AND user_id IN ({','.join('?' * len(user_ids))})"
        args += list(user_ids)
    get_db().execute(sql, args)


def job_created(job_id):
    job = _current(job_id)
    if job is not None:
        for user_id in _people(job_id):
            notify(user_id, JOB_ASSIGNED, job_id, **_snapshot(job))


def job_updated(old, job_id):
    """Compara o trabalho antes (old, com people_ids) e depois da edição e avisa só quem precisa saber:
    quem entrou, quem saiu, e quem continua (se mudou data, horário ou se foi cancelado)."""
    new = _current(job_id)
    if new is None:
        return
    before, after = list(old["people_ids"]), _people(job_id)
    joined = [u for u in after if u not in before]
    left = [u for u in before if u not in after]
    stayed = [u for u in after if u in before]
    was_open, is_open = old["status"] in OPEN_STATUSES, new["status"] in OPEN_STATUSES
    moved = (old["job_date"], old["start_time"]) != (new["job_date"], new["start_time"])
    if moved or not is_open:
        _drop_reminders(job_id)
    else:
        _drop_reminders(job_id, left)
    for user_id in left:
        if was_open:
            notify(user_id, JOB_UNASSIGNED, job_id, **_snapshot(old))
    for user_id in joined:
        if is_open:
            notify(user_id, JOB_ASSIGNED, job_id, **_snapshot(new))
    for user_id in stayed:
        if new["status"] == "cancelled" and old["status"] != "cancelled":
            notify(user_id, JOB_CANCELLED, job_id, **_snapshot(new))
        elif is_open and not was_open:  # voltou para a agenda (ex.: descancelado)
            notify(user_id, JOB_ASSIGNED, job_id, **_snapshot(new))
        elif is_open and moved:
            notify(user_id, JOB_RESCHEDULED, job_id,
                   old_date=old["job_date"], old_time=old["start_time"], **_snapshot(new))


def job_deleted(job):
    """Chame ANTES de apagar o trabalho (depois ele some, e os avisos antigos dele junto)."""
    if job["status"] in OPEN_STATUSES:
        for user_id in job["people_ids"]:
            notify(user_id, JOB_DELETED, None, **_snapshot(job))


def jobs_repeated(job, new_ids, every):
    if new_ids:
        first = _current(new_ids[0])
        for user_id in job["people_ids"]:
            notify(user_id, JOBS_REPEATED, new_ids[0], n=len(new_ids), every=every, **_snapshot(first))


def ensure_daily_reminders():
    """Cria o lembrete de cada trabalho de amanhã que ainda não tem um. Barato e sem duplicar."""
    db = get_db()
    tomorrow = (date.fromisoformat(utils.today_iso()) + timedelta(days=1)).isoformat()
    recent = _utc_iso(datetime.now(timezone.utc) - RECENT_NOTICE)
    rows = db.execute(  # um lembrete por pessoa em cada trabalho de amanhã
        "SELECT j.id, a.user_id FROM jobs j JOIN job_assignees a ON a.job_id = j.id "
        "JOIN users u ON u.id = a.user_id AND u.active = 1 "
        "WHERE j.job_date = ? AND j.status = 'scheduled' AND NOT EXISTS ("
        "  SELECT 1 FROM notifications n WHERE n.job_id = j.id AND n.user_id = a.user_id "
        "  AND (n.kind = ? OR (n.kind IN (?, ?, ?) AND n.created_at >= ?)))",
        (tomorrow, JOB_TOMORROW, JOB_ASSIGNED, JOB_RESCHEDULED, JOBS_REPEATED, recent),
    ).fetchall()
    for row in rows:
        db.execute("INSERT INTO notifications (user_id, kind, job_id, created_at) VALUES (?, ?, ?, ?)",
                   (row["user_id"], JOB_TOMORROW, row["id"], utils.now_utc_iso()))
    if rows:
        db.commit()
    return len(rows)


# ---------- Mostrar ----------

def _when(day, time):
    text = utils.date_long(day) if day else ""
    return f"{text}, {time}" if text and time else text


def render(row):
    """Título, texto e link de um aviso, no idioma atual do app."""
    t = i18n.t
    kind = row["kind"]
    p = json.loads(row["params"] or "{}")
    if kind == JOB_TOMORROW:  # usa os dados atuais do trabalho
        title = t("notif.job_tomorrow_title", date=utils.date_long(row["job_date"]))
        body = " · ".join(x for x in (row["start_time"], row["client_name"], row["job_title"]) if x)
    elif kind == JOBS_REPEATED:
        title = t("notif.jobs_repeated_title", n=p.get("n", 0), client=p.get("client", ""))
        body = t("notif.jobs_repeated_body", title=p.get("title", ""), every=p.get("every", 1),
                 when=_when(p.get("date"), p.get("time")))
    elif kind == JOB_RESCHEDULED:
        title = t("notif.job_rescheduled_title", client=p.get("client", ""))
        body = t("notif.job_rescheduled_body", title=p.get("title", ""), when=_when(p.get("date"), p.get("time")),
                 old_when=_when(p.get("old_date"), p.get("old_time")))
    elif kind in QUOTE_KINDS:
        title = t(f"notif.{kind}_title", client=p.get("client", ""), ref=p.get("ref", ""))
        body = " · ".join(x for x in (p.get("title", ""), p.get("total", ""), p.get("note", "")) if x)
    elif kind == COMPANY_COMMENT:
        title = t("notif.company_comment_title", company=p.get("company", ""), client=p.get("client", ""))
        body = t("notif.company_comment_body", author=p.get("author", ""), text=p.get("text", ""),
                 when=utils.date_long(p.get("date")) if p.get("date") else "")
    else:
        title = t(f"notif.{kind}_title", client=p.get("client", ""))
        body = " · ".join(x for x in (p.get("title", ""), _when(p.get("date"), p.get("time"))) if x)
    if kind in QUOTE_KINDS:
        url = url_for("quotes.quote_detail", quote_id=row["quote_id"]) if row["quote_id"] else None
    elif kind == COMPANY_COMMENT:
        url = url_for("jobs.job_detail", job_id=row["job_id"], _anchor="comentarios") if row["job_id"] else None
    else:
        url = url_for("jobs.job_detail", job_id=row["job_id"]) if row["job_id"] and kind not in (JOB_UNASSIGNED, JOB_DELETED) else None
    return {"title": title, "body": body, "read": bool(row["read_at"]), "created_at": row["created_at"], "url": url}


def unread_count(user_id):
    return get_db().execute(
        "SELECT COUNT(*) FROM notifications WHERE user_id = ? AND read_at IS NULL", (user_id,)
    ).fetchone()[0]


@bp.route("/")
@login_required
def list_notifications():
    db = get_db()
    rows = db.execute(_SELECT + "WHERE n.user_id = ? ORDER BY n.created_at DESC, n.id DESC LIMIT 60", (g.user["id"],)).fetchall()
    items = [render(r) for r in rows]  # guarda o "não lido" antes de marcar tudo como lido
    db.execute("UPDATE notifications SET read_at = ? WHERE user_id = ? AND read_at IS NULL",
               (utils.now_utc_iso(), g.user["id"]))
    db.commit()
    return render_template("notifications.html", items=items)


# ---------- E-mail ----------

class _Mailer:
    def __init__(self, row):
        cfg = current_app.config
        server, port = cfg.get("MAIL_SERVER", "smtp.gmail.com"), int(cfg.get("MAIL_PORT", 587))
        timeout = cfg.get("MAIL_TIMEOUT", 15)
        if port == 465:
            self.conn = smtplib.SMTP_SSL(server, port, timeout=timeout)
        else:
            self.conn = smtplib.SMTP(server, port, timeout=timeout)
            self.conn.starttls()
        self.sender = row["mail_address"].strip()
        self.conn.login(self.sender, row["mail_password"].replace(" ", ""))  # a senha de app vem com espaços

    def send(self, to, subject, body):
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = subject, self.sender, to
        msg.set_content(body)
        self.conn.send_message(msg)

    def close(self):
        try:
            self.conn.quit()
        except Exception:  # a conexão já caiu: nada a fazer
            pass


def _absolute(path):
    return (current_app.config.get("APP_URL") or request.host_url).rstrip("/") + path


def _compose(items):
    lines = []
    for it in items:
        lines += [it["title"], it["body"]]
        if it["url"]:
            lines.append(_absolute(it["url"]))
        lines.append("")
    lines.append(i18n.t("email.footer", app=branding.app_name()))
    subject = items[0]["title"] if len(items) == 1 else i18n.t("email.subject_many", n=len(items))
    return subject, "\n".join(lines)


def send_pending(ids=None, include_reminders=False):
    """Manda por e-mail os avisos que ainda não saíram (um e-mail por pessoa, com tudo junto).
    Cada aviso é "reservado" antes de sair, então duas visitas ao mesmo tempo não mandam em dobro.
    Devolve quantos e-mails saíram. Erro de rede ou senha sobe para quem chamou; os avisos que
    não saíram voltam para a fila."""
    row = settings()
    if not email_enabled(row):
        return 0
    db = get_db()
    sql, args = _SELECT + "WHERE n.emailed_at IS NULL AND n.read_at IS NULL AND u.active = 1", []
    if ids is not None:
        if not ids:
            return 0
        sql += f" AND n.id IN ({','.join('?' * len(ids))})"
        args += list(ids)
    today, oldest = utils.today_iso(), _utc_iso(datetime.now(timezone.utc) - EMAIL_MAX_AGE)
    to_send, skipped = [], []
    for r in db.execute(sql + " ORDER BY n.user_id, n.id", args).fetchall():
        if r["kind"] == JOB_TOMORROW:
            if not include_reminders:
                continue
            if not r["job_date"] or r["job_date"] <= today:  # a véspera já passou
                skipped.append(r["id"])
                continue
        elif r["created_at"] < oldest:
            skipped.append(r["id"])
            continue
        to_send.append(r)
    db.executemany("UPDATE notifications SET emailed_at = 'skipped' WHERE id = ? AND emailed_at IS NULL",
                   [(i,) for i in skipped])
    token = "sending:" + secrets.token_hex(6)
    claimed = [r for r in to_send if db.execute(
        "UPDATE notifications SET emailed_at = ? WHERE id = ? AND emailed_at IS NULL", (token, r["id"])).rowcount]
    db.commit()
    if not claimed:
        return 0
    by_user = {}
    for r in claimed:
        by_user.setdefault(r["user_id"], []).append(r)
    sent, mailer = 0, None
    try:
        mailer = _Mailer(row)
        for user_rows in by_user.values():
            previous = g.get("lang")
            chosen = user_rows[0]["user_language"]
            g.lang = chosen if chosen in i18n.LANGUAGES else row["language"]  # no idioma de quem recebe
            try:
                subject, body = _compose([render(r) for r in user_rows])
            finally:
                g.lang = previous
            mailer.send(user_rows[0]["user_email"], subject, body)
            db.executemany("UPDATE notifications SET emailed_at = ? WHERE id = ?",
                           [(utils.now_utc_iso(), r["id"]) for r in user_rows])
            db.commit()
            sent += 1
    finally:
        db.execute("UPDATE notifications SET emailed_at = NULL WHERE emailed_at = ?", (token,))  # não saiu: volta
        db.commit()
        if mailer is not None:
            mailer.close()
    return sent


def send_test_email(to):
    send_email(to, i18n.t("email.test_subject", app=branding.app_name()), i18n.t("email.test_body"))


def send_email(to, subject, body):
    """Um e-mail avulso pelo Gmail de Conta → Avisos por e-mail (ex.: mandar uma cotação). Erros sobem pra quem chamou."""
    mailer = _Mailer(settings())
    try:
        mailer.send(to, subject, body)
    finally:
        mailer.close()


# ---------- Rotina diária (sem tarefa agendada) ----------

def run_daily(force=False):
    """Lembretes de amanhã, limpeza de avisos velhos e e-mails pendentes.
    Sem force, roda no máximo uma vez por dia (só uma visita "ganha" a vez)."""
    db = get_db()
    settings()
    today = utils.today_iso()
    cur = db.execute("UPDATE settings SET last_daily_run = ? WHERE id = 1" + ("" if force else " AND last_daily_run != ?"),
                     (today,) if force else (today, today))
    db.commit()
    if not cur.rowcount:
        return None  # outra visita já rodou hoje
    created = ensure_daily_reminders()
    cutoff = _utc_iso(datetime.now(timezone.utc) - timedelta(days=KEEP_READ_DAYS))
    db.execute("DELETE FROM notifications WHERE read_at IS NOT NULL AND created_at < ?", (cutoff,))
    db.commit()
    return created, send_pending(include_reminders=True)


def _daily_due():
    if datetime.now(utils.tz()).hour < int(current_app.config.get("DAILY_EMAIL_HOUR", 17)):
        return False
    return settings()["last_daily_run"] != utils.today_iso()


@bp.route("/diario/<key>")
def daily_link(key):
    """Link para um serviço externo (ex.: cron-job.org) chamar todo dia no mesmo horário."""
    row = settings()
    if not row["cron_key"] or not hmac.compare_digest(row["cron_key"].encode(), key.encode()):
        abort(404)
    status, created, sent, sms = "ok", 0, 0, 0
    try:
        created, sent = run_daily(force=True)
    except Exception as exc:
        current_app.logger.exception("Falha na rotina diária")
        status = f"erro: {exc.__class__.__name__}"
    try:
        from . import reminders  # importado aqui: reminders usa este módulo
        sms = reminders.run(force=True)  # lembrete pro cliente (só sai entre a hora escolhida e as 21h)
    except Exception as exc:
        current_app.logger.exception("Falha nos lembretes pro cliente")
        status = f"erro: {exc.__class__.__name__}"
    summaries = 0
    try:
        from . import portal_mail  # importado aqui: portal_mail usa este módulo
        summaries = portal_mail.run(force=True)  # resumo do dia pra empresa (a partir da hora escolhida, até as 22h)
    except Exception as exc:
        current_app.logger.exception("Falha no resumo do dia pra empresa")
        status = f"erro: {exc.__class__.__name__}"
    text = (f"{status}: {created} lembrete(s) criado(s), {sent} e-mail(s) enviado(s), {sms} SMS pro cliente, "
            f"{summaries} resumo(s) pra empresa\n")
    return text, 200, {"Content-Type": "text/plain; charset=utf-8"}


@bp.after_app_request
def _after_response(response):
    """Depois que a página já foi entregue: manda por e-mail os avisos criados neste pedido e,
    uma vez por dia depois das 17h, roda a rotina diária."""
    ids = g.pop("new_notifications", None)
    if request.endpoint in ("static", "manifest", "notifications.daily_link"):
        return response
    ids = ids if (ids and response.status_code < 400) else None
    due = _daily_due()
    if not ids and not due:
        return response
    app, base_url, lang = current_app._get_current_object(), request.host_url, g.get("lang")

    def work():
        with app.test_request_context("/", base_url=base_url):
            g.lang = lang or settings()["language"]
            if ids:
                try:
                    send_pending(ids=ids)
                except Exception:  # o aviso continua no app; a rotina diária tenta o e-mail de novo
                    app.logger.exception("Falha ao mandar aviso por e-mail")
            if due:
                try:
                    run_daily()
                except Exception:
                    app.logger.exception("Falha na rotina diária")

    response.call_on_close(work)
    return response
