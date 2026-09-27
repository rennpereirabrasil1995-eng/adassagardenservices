"""Resumo do dia por e-mail pra empresa (área da empresa).

Uma vez por dia, a partir da hora escolhida na página da empresa (padrão 18h, até as 22h), cada pessoa da
empresa que não desligou recebe um e-mail com os serviços concluídos nos jardins dela desde o último resumo:
o horário no local, as tarefas feitas e as não feitas (com o motivo), os materiais, quantas fotos, e o link
pra ver tudo na área dela. Sem serviço novo, não sai nada. O que ficou pronto depois do resumo vai no do
dia seguinte.

Sai pelo Gmail de Conta → Avisos por e-mail. Como o PythonAnywhere grátis não tem tarefa agendada, o resumo
sai na primeira visita ao app depois da hora escolhida (qualquer tela, de qualquer pessoa), ou na hora certa
se o "link diário" estiver no cron-job.org (o mesmo link dos avisos e do lembrete pro cliente).
"""
from datetime import datetime, timedelta, timezone

from flask import Blueprint, current_app, g, request, url_for

from . import branding, i18n, notifications, portal, utils
from .db import get_db

bp = Blueprint("portal_mail", __name__)

HOURS = (16, 17, 18, 19, 20, 21)    # horários pra escolher
DEFAULT_HOUR = 18
LAST_HOUR = 22                      # depois das 22h não sai mais (vai no resumo do dia seguinte)
CHECK_EVERY = timedelta(minutes=10)
FIRST_WINDOW = timedelta(hours=24)  # o primeiro resumo pega o que foi concluído nas últimas 24 horas


def _now():
    """Agora, no fuso do app (separado pra os testes escolherem a hora)."""
    return datetime.now(utils.tz())


def _utc_iso(moment):
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def recipients(company_id):
    """Quem recebe: os acessos ativos da empresa que não desligaram o resumo na conta deles."""
    return get_db().execute("SELECT id, name, email, language FROM company_users WHERE company_id = ? AND active = 1 "
                            "AND daily_mail = 1 ORDER BY id", (company_id,)).fetchall()


def pending(company):
    """Serviços concluídos desde o último resumo (no primeiro, os das últimas 24 horas)."""
    since = company["daily_sent_at"] or _utc_iso(_now() - FIRST_WINDOW)
    return portal.services(company["id"], "j.finished_at > ?", (since,),
                           order=" ORDER BY j.job_date, j.finished_at, j.id")


def _when(s):
    if s["start"] and s["finish"]:
        span = f"{s['start']}–{s['finish']}"
        return f"{span} ({utils.format_minutes(s['minutes'])})" if s["minutes"] is not None else span
    return i18n.t("portal.finished_at", time=s["finish"]) if s["finish"] else ""


def compose(company, services, person_name, lang):
    """(assunto, texto) do resumo, no idioma de quem recebe."""
    t = i18n.t
    previous, g.lang = g.get("lang"), lang if lang in i18n.LANGUAGES else i18n.DEFAULT_LANGUAGE
    try:
        brand, today = branding.app_name(), _now().date().isoformat()
        count = t("portal.services_one") if len(services) == 1 else t("portal.services_many", n=len(services))
        minutes = sum(s["minutes"] or 0 for s in services)
        summary = count + (t("pmail.on_site", time=utils.format_minutes(minutes)) if minutes else "")
        today_only = all(s["job_date"] == today for s in services)
        first_name = (person_name or "").split()[0] if (person_name or "").strip() else ""
        lines = [t("pmail.hello", name=first_name) if first_name else t("pmail.hello_plain"), "",
                 t("pmail.intro_today" if today_only else "pmail.intro_since", summary=summary)]
        for s in services:
            lines += ["", s["garden"], " · ".join(x for x in (utils.date_long(s["job_date"]), _when(s)) if x)]
            lines += [f"✓ {task}" for task in s["done"]]
            lines += [f"✗ {x['text']} – " + (t("pmail.not_done_reason", reason=x["note"]) if x["note"] else t("pmail.not_done"))
                      for x in s["not_done"]]
            if s["materials"]:
                lines.append(f"{t('portal.materials')}: {s['materials']}")
            if s["before"] or s["after"]:
                lines.append(t("pmail.photos", before=len(s["before"]), after=len(s["after"])))
            lines.append(portal.absolute(url_for("portal.service", job_id=s["id"])))
        lines += ["", t("pmail.see_all"), portal.absolute(url_for("portal.days", day=services[-1]["job_date"])),
                  "", f"— {brand}", t("pmail.why")]
        subject = t("pmail.subject", brand=brand, date=utils.date_long(today))
    finally:
        g.lang = previous
    return subject, "\n".join(lines)


def send(company, force=False):
    """Manda o resumo de uma empresa pra quem recebe. Devolve quantos e-mails saíram (0: nada novo, ninguém
    pra receber, ou outra visita já mandou hoje). Erro de rede ou senha sobe pra quem chamou."""
    services, people = pending(company), recipients(company["id"])
    if not services or not people:
        return 0
    db, today = get_db(), _now().date().isoformat()
    if not force:  # reserva o dia: duas visitas ao mesmo tempo não mandam em dobro
        if not db.execute("UPDATE companies SET daily_sent_on = ? WHERE id = ? AND daily_sent_on != ?",
                          (today, company["id"], today)).rowcount:
            return 0
        db.commit()
    sent = 0
    try:
        for person in people:
            subject, body = compose(company, services, person["name"], person["language"])
            notifications.send_email(person["email"], subject, body)
            sent += 1
    finally:
        if sent:  # o próximo resumo começa depois do último serviço que foi neste
            last = max(s["finished_at"] for s in services)
            db.execute("UPDATE companies SET daily_sent_on = ?, daily_sent_at = ? WHERE id = ?", (today, last, company["id"]))
        elif not force:  # não saiu nenhum: libera o dia pra tentar de novo daqui a pouco
            db.execute("UPDATE companies SET daily_sent_on = ? WHERE id = ?", (company["daily_sent_on"], company["id"]))
        db.commit()
    return sent


def _due_companies():
    now = _now()
    if now.hour >= LAST_HOUR:
        return []
    return get_db().execute("SELECT * FROM companies WHERE daily_hour IS NOT NULL AND daily_hour <= ? AND daily_sent_on != ? "
                            "ORDER BY id", (now.hour, now.date().isoformat())).fetchall()


def run(force=False):
    """Manda os resumos que já estão na hora. Com force (link diário), não espera os 10 minutos entre uma
    olhada e outra, mas continua respeitando o horário. Devolve quantos e-mails saíram."""
    if not notifications.email_enabled():
        return 0
    companies = _due_companies()
    if not companies:
        return 0
    db, now = get_db(), _now()
    claimed = db.execute("UPDATE settings SET company_mail_checked_at = ? WHERE id = 1 AND (company_mail_checked_at = '' "
                         "OR company_mail_checked_at < ?)", (_utc_iso(now), _utc_iso(now - CHECK_EVERY))).rowcount
    db.commit()
    if not claimed and not force:
        return 0  # outra visita olhou há pouco
    total = 0
    for company in companies:
        try:
            total += send(company)
        except Exception:  # sem internet ou senha errada: tenta de novo na próxima olhada
            current_app.logger.exception("Falha no resumo do dia pra empresa")
            break
    return total


def _due():
    row = notifications.settings()
    if not notifications.email_enabled(row) or not _due_companies():
        return False
    last = row["company_mail_checked_at"]
    return not last or last < _utc_iso(_now() - CHECK_EVERY)


@bp.after_app_request
def _after_response(response):
    """Depois que a página já foi entregue: se estiver na hora de algum resumo, manda."""
    if request.endpoint in ("static", "manifest", "branding.asset", "notifications.daily_link") or response.status_code >= 400:
        return response
    try:
        if not _due():
            return response
    except Exception:  # banco ainda sem as colunas novas (antes do Reload) etc.: o site segue normal
        return response
    app, base_url = current_app._get_current_object(), request.host_url

    def work():
        with app.test_request_context("/", base_url=base_url):
            try:
                run()
            except Exception:
                app.logger.exception("Falha no resumo do dia pra empresa")

    response.call_on_close(work)
    return response
