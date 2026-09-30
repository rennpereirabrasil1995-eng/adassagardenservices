"""Área da empresa: o que a pessoa de uma empresa vê quando entra com o login dela.

Uma empresa (companies.py) contrata a gente pra vários jardins. Quem é de lá entra pela mesma tela de
login e cai aqui, onde só VÊ, sem mudar nada, os serviços CONCLUÍDOS dos jardins da empresa: dia a dia
ou jardim por jardim, com o horário no local, as tarefas, os materiais e as fotos de Antes e Depois.
Nada de dinheiro, telefone ou anotações do cliente, recado da equipe, nem quem da equipe foi.

Em cada serviço dá pra comentar: o comentário chega no sininho do dono (e de quem cuida da agenda) e
por e-mail, se os avisos por e-mail estiverem ligados. A resposta da equipe aparece no mesmo lugar,
marcada como nova, e também vai por e-mail pra empresa.

As mesmas telas servem de prévia pro dono (Empresas → Ver como a empresa vê): os links apontam pra
/empresas/<id>/previa/... e o comentário fica desligado.

  /portal/                      serviços do dia (?day=AAAA-MM-DD; sem data: hoje, ou o último dia com serviço)
  /portal/gardens               os jardins da empresa
  /portal/gardens/<id>          o histórico de um jardim
  /portal/service/<id>          um serviço: tarefas, materiais, fotos e comentários
  /portal/report                relatório do período (semana, mês, ano): serviços, tempo, tarefas e fotos, por jardim
  /portal/account               senha e idioma
"""
import bisect
import json
import os
import re
import secrets
from datetime import date, datetime, timedelta, timezone
from io import BytesIO

from flask import (Blueprint, abort, current_app, flash, g, redirect, render_template, request, send_file,
                   send_from_directory, session, url_for)
from werkzeug.security import check_password_hash

from . import branding, i18n, notifications, photos, utils
from .auth import hash_password, new_session_key, password_error, portal_required
from .db import get_db
from .jobs import REPORT_SKIP

bp = Blueprint("portal", __name__, url_prefix="/portal")

MAX_COMMENT = 1000  # letras por comentário
STRIP = 4           # fotos de cada fase no cartão do dia (o resto vira "+N", como no WhatsApp)
PAGE = 15           # serviços por página no histórico de um jardim


class Links:
    """Os endereços das telas: os da área da empresa, ou os da prévia do dono (mesmas telas, outro endereço)."""

    def __init__(self, company, preview=False):
        self.company, self.preview = company, preview

    def __call__(self, name, **kw):
        if self.preview:
            return url_for(f"companies.preview_{name}", company_id=self.company["id"], **kw)
        return url_for(f"portal.{name}", **kw)

    def photo(self, job_id, filename, mini=False):
        extra = {"mini": 1} if mini else {}
        if self.preview:  # o dono já pode ver as fotos pela tela do trabalho
            return url_for("jobs.job_photo_file", job_id=job_id, filename=filename, **extra)
        return url_for("portal.photo", job_id=job_id, filename=filename, **extra)

    def file(self, job_id, file_id, mini=False):
        """Anexo de um comentário."""
        extra = {"mini": 1} if mini else {}
        if self.preview:
            return url_for("companies.comment_file", job_id=job_id, file_id=file_id, **extra)
        return url_for("portal.comment_file", job_id=job_id, file_id=file_id, **extra)


def absolute(path):
    """Endereço completo (pra mensagem de acesso e pro e-mail)."""
    return (current_app.config.get("APP_URL") or request.host_url).rstrip("/") + path


@bp.app_template_global()
def place(name, address, postcode=""):
    """O endereço embaixo do nome do jardim, sem repetir quando o jardim já se chama pelo endereço
    ("27 Birch Road" / "27 Birch Road, SW17 8QA" vira só "SW17 8QA")."""
    address = (address or "").strip()
    if address.lower() == (name or "").strip().lower():
        address = ""
    return ", ".join(x for x in (address, (postcode or "").strip()) if x)


# ---------- O que a empresa vê ----------

SERVICE_SELECT = """
SELECT j.id, j.client_id, j.title, j.job_date, j.start_time, j.started_at, j.finished_at, j.materials,
       c.name AS garden, c.address, c.postcode,
       (SELECT COUNT(*) FROM job_assignees a WHERE a.job_id = j.id) AS people
FROM jobs j JOIN clients c ON c.id = j.client_id
WHERE c.company_id = ? AND j.status = 'done'
"""


def visible(company_id, job_id):
    """O serviço é de um jardim da empresa e já foi concluído?"""
    return get_db().execute(
        "SELECT 1 FROM jobs j JOIN clients c ON c.id = j.client_id WHERE j.id = ? AND c.company_id = ? "
        "AND j.status = 'done'", (job_id, company_id)).fetchone() is not None


def services(company_id, where="", params=(), order=" ORDER BY j.job_date DESC, j.id DESC", limit=None, offset=0):
    """Serviços concluídos dos jardins da empresa, cada um com as tarefas, as fotos e os comentários."""
    sql = SERVICE_SELECT + (f" AND {where}" if where else "") + order
    if limit:
        sql += f" LIMIT {int(limit)} OFFSET {int(offset)}"
    rows = [dict(r) for r in get_db().execute(sql, (company_id, *params))]
    _fill(rows, company_id)
    return rows


def _fill(rows, company_id):
    by_id = {r["id"]: r for r in rows}
    for r in rows:
        start, finish = utils.to_local(r["started_at"]), utils.to_local(r["finished_at"])
        r.update(done=[], not_done=[], before=[], after=[], comments=0, new_replies=0,
                 start=f"{start:%H:%M}" if start else "", finish=f"{finish:%H:%M}" if finish else "",
                 minutes=utils.duration_minutes(r["started_at"], r["finished_at"]))
    ids, db = list(by_id), get_db()
    for i in range(0, len(ids), 500):  # o SQLite tem limite de "?" por consulta
        chunk = ids[i:i + 500]
        marks = ",".join("?" * len(chunk))
        for t in db.execute(f"SELECT job_id, description, done, skipped, note FROM job_tasks WHERE job_id IN ({marks}) "
                            "ORDER BY position, id", chunk):
            if REPORT_SKIP.search(t["description"]):  # "tirar fotos": as fotos já estão ali
                continue
            if t["done"]:
                by_id[t["job_id"]]["done"].append(t["description"])
            elif t["skipped"]:
                by_id[t["job_id"]]["not_done"].append({"text": t["description"], "note": t["note"]})
        for p in db.execute(f"SELECT job_id, phase, filename FROM job_photos WHERE job_id IN ({marks}) ORDER BY id", chunk):
            by_id[p["job_id"]][p["phase"]].append(p["filename"])
        for m in db.execute(f"SELECT job_id, COUNT(*) AS n, COALESCE(SUM(from_company = 0 AND seen_at IS NULL), 0) AS fresh "
                            f"FROM job_comments WHERE job_id IN ({marks}) AND company_id = ? GROUP BY job_id",
                            (*chunk, company_id)):
            by_id[m["job_id"]].update(comments=m["n"], new_replies=m["fresh"])


def _with_photos(rows, links):
    """Endereços das fotos (grande e miniatura) de cada serviço."""
    for s in rows:
        for phase in ("before", "after"):
            s[f"{phase}_photos"] = [{"full": links.photo(s["id"], f), "mini": links.photo(s["id"], f, mini=True),
                                     "delete": None} for f in s[phase]]
    return rows


def service_days(company_id):
    return [r[0] for r in get_db().execute(
        "SELECT DISTINCT j.job_date FROM jobs j JOIN clients c ON c.id = j.client_id "
        "WHERE c.company_id = ? AND j.status = 'done' ORDER BY j.job_date", (company_id,))]


def new_replies(company_id):
    """Serviços com resposta da equipe que ninguém da empresa abriu ainda."""
    return [dict(r) for r in get_db().execute(
        "SELECT j.id, j.job_date, c.name AS garden, COUNT(*) AS n FROM job_comments m "
        "JOIN jobs j ON j.id = m.job_id JOIN clients c ON c.id = j.client_id "
        "WHERE c.company_id = ? AND m.company_id = ? AND j.status = 'done' AND m.from_company = 0 AND m.seen_at IS NULL "
        "GROUP BY j.id ORDER BY j.job_date DESC, j.id DESC LIMIT 10", (company_id, company_id))]


# ---------- As telas (a área da empresa e a prévia do dono usam as mesmas) ----------

def days_page(company, links, preview=False):
    days = service_days(company["id"])
    today = utils.today_iso()
    asked = utils.parse_date(request.args.get("day", ""))
    if asked:
        day = asked.isoformat()
    elif today in days or not days:
        day = today
    else:
        day = days[-1]  # hoje ainda não teve serviço: mostra o último dia que teve
    before, after = bisect.bisect_left(days, day), bisect.bisect_right(days, day)
    rows = _with_photos(services(company["id"], "j.job_date = ?", (day,), order=" ORDER BY j.id"), links)
    rows.sort(key=lambda s: (s["start"] or s["start_time"] or s["finish"] or "99:99", s["id"]))  # na ordem em que foram feitos
    return render_template(
        "portal_days.html", company=company, links=links, preview=preview, day=day, services=rows,
        yesterday=(date.fromisoformat(today) - timedelta(days=1)).isoformat(),
        prev_day=days[before - 1] if before else None, next_day=days[after] if after < len(days) else None,
        latest=days[-1] if days else None, has_any=bool(days),
        total_minutes=sum(s["minutes"] or 0 for s in rows), gardens_count=len({s["client_id"] for s in rows}),
        replies=new_replies(company["id"]), strip_limit=STRIP)


def gardens_page(company, links, preview=False):
    rows = get_db().execute(
        "SELECT c.id, c.name, c.address, c.postcode, "
        "(SELECT MAX(j.job_date) FROM jobs j WHERE j.client_id = c.id AND j.status = 'done') AS last_day, "
        "(SELECT COUNT(*) FROM jobs j WHERE j.client_id = c.id AND j.status = 'done') AS services "
        "FROM clients c WHERE c.company_id = ? ORDER BY c.name COLLATE NOCASE", (company["id"],)).fetchall()
    return render_template("portal_gardens.html", company=company, links=links, preview=preview, gardens=rows)


def garden_page(company, client_id, links, preview=False):
    garden = get_db().execute("SELECT id, name, address, postcode FROM clients WHERE id = ? AND company_id = ?",
                              (client_id, company["id"])).fetchone()
    if garden is None:
        abort(404)
    page = min(max(0, request.args.get("page", 0, type=int)), 10000)  # 10 mil páginas = 150 mil serviços: sobra
    rows = services(company["id"], "j.client_id = ?", (client_id,), limit=PAGE + 1, offset=page * PAGE)
    return render_template("portal_garden.html", company=company, links=links, preview=preview, garden=garden,
                           services=_with_photos(rows[:PAGE], links), page=page, more=len(rows) > PAGE, strip_limit=STRIP)


def service_page(company, job_id, links, preview=False):
    rows = services(company["id"], "j.id = ?", (job_id,))
    if not rows:
        abort(404)
    comments = attach_files(thread(job_id, company["id"]), lambda file_id, mini=False: links.file(job_id, file_id, mini))
    fresh = {m["id"] for m in comments if not m["from_company"] and not m["seen_at"]}
    if fresh and not preview:  # a empresa abriu: as respostas deixam de ser novas (a prévia do dono não conta)
        _mark_seen(fresh)
    return render_template("portal_service.html", company=company, links=links, preview=preview,
                           s=_with_photos(rows, links)[0], comments=comments, fresh=fresh, max_comment=MAX_COMMENT,
                           max_files=MAX_FILES, max_doc_mb=MAX_DOC_MB)


# ---------- Relatório da empresa: o que foi feito nos jardins dela num período ----------
# Só o que a empresa já pode ver nas outras telas (serviços concluídos, tempo no local, tarefas, fotos),
# somado por semana, mês ou ano e por jardim. Nada de dinheiro nem de quem da equipe foi.

REPORT_PERIODS = ("semana", "mes", "ano")
NOT_DONE_LIMIT = 40  # tarefas não feitas listadas no relatório (as mais recentes)


def read_report_period(args):
    period = args.get("periodo", "semana")
    if period not in REPORT_PERIODS:
        period = "semana"
    try:
        ref = date.fromisoformat(args.get("data", "")).isoformat()
    except ValueError:
        ref = utils.today_iso()
    return period, ref


def _report_bars(period, start, end, rows, links):
    """Colunas do gráfico: um dia por coluna (semana e mês) ou um mês por coluna (ano). Cada coluna leva
    ao dia (ou ao relatório do mês)."""
    s, e = date.fromisoformat(start), date.fromisoformat(end)
    if period == "ano":
        keys = [f"{s.year}-{m:02d}" for m in range(1, 13)]
        key_of = lambda day: day[:7]
        label_of = lambda k: utils.month_abbr(k + "-01")
        title_of = lambda k: f"{utils.month_abbr(k + '-01')} {k[:4]}"
        link_of = lambda k: links("report", periodo="mes", data=k + "-01")
    else:
        keys = [(s + timedelta(days=i)).isoformat() for i in range((e - s).days + 1)]
        key_of = lambda day: day
        if period == "semana":
            label_of = utils.weekday
        else:  # mês: rótulo só em alguns dias, pra não embolar
            label_of = lambda k: str(int(k[8:])) if int(k[8:]) in (1, 8, 15, 22, 29) else ""
        title_of = lambda k: f"{utils.weekday(k)} {int(k[8:])} {utils.month_abbr(k)}"
        link_of = lambda k: links("days", day=k)
    counts = {k: 0 for k in keys}
    for r in rows:
        k = key_of(r["job_date"])
        if k in counts:
            counts[k] += 1
    peak = max(counts.values(), default=0) or 1
    return [{"label": label_of(k), "title": title_of(k), "total": counts[k], "pct": round(counts[k] * 100 / peak, 1),
             "link": link_of(k) if counts[k] else ""} for k in keys]


def report_data(company, period, ref, links):
    """Tudo que o relatório da empresa mostra (a tela e o PDF usam os mesmos números)."""
    from .reports import _period_label  # o mesmo rótulo de período do relatório do dono
    start, end = utils.period_bounds(period, ref)
    rows = services(company["id"], "j.job_date BETWEEN ? AND ?", (start, end), order=" ORDER BY j.job_date, j.id")
    gardens = {}
    for s in rows:
        gd = gardens.setdefault(s["client_id"], {"id": s["client_id"], "name": s["garden"], "services": 0, "minutes": 0,
                                                 "done": 0, "not_done": 0, "photos": 0})
        gd["services"] += 1
        gd["minutes"] += s["minutes"] or 0
        gd["done"] += len(s["done"])
        gd["not_done"] += len(s["not_done"])
        gd["photos"] += len(s["before"]) + len(s["after"])
    gardens = sorted(gardens.values(), key=lambda gd: (-gd["services"], gd["name"].lower()))
    not_done = [{"job_id": s["id"], "garden": s["garden"], "date": s["job_date"], "text": t["text"], "note": t["note"]}
                for s in reversed(rows) for t in s["not_done"]]
    today = utils.today_iso()
    return dict(
        period=period, ref=ref, start=start, end=end, period_label=_period_label(period, start, end),
        periods=[("semana", i18n.t("history.period_week")), ("mes", i18n.t("history.period_month")),
                 ("ano", i18n.t("history.period_year"))],
        prev_ref=(date.fromisoformat(start) - timedelta(days=1)).isoformat(),
        next_ref=(date.fromisoformat(end) + timedelta(days=1)).isoformat(),
        is_current=start <= today <= end,
        services_count=len(rows), total_minutes=sum(gd["minutes"] for gd in gardens),
        gardens_served=len(gardens),
        gardens_total=get_db().execute("SELECT COUNT(*) FROM clients WHERE company_id = ?", (company["id"],)).fetchone()[0],
        tasks_done=sum(gd["done"] for gd in gardens), tasks_not_done=sum(gd["not_done"] for gd in gardens),
        photos=sum(gd["photos"] for gd in gardens), comments=sum(s["comments"] for s in rows),
        gardens=gardens, max_minutes=max((gd["minutes"] for gd in gardens), default=0),
        not_done=not_done[:NOT_DONE_LIMIT], not_done_total=len(not_done),
        bars=_report_bars(period, start, end, rows, links),
    )


def report_page(company, links, preview=False):
    period, ref = read_report_period(request.args)
    return render_template("portal_report.html", company=company, links=links, preview=preview,
                           **report_data(company, period, ref, links))


def report_pdf_file(company, links):
    """O relatório da empresa em PDF, pra ela guardar ou repassar."""
    period, ref = read_report_period(request.args)
    try:
        from . import pdf_report  # o reportlab só é carregado quando alguém baixa um PDF
    except ImportError:  # esqueceram o "pip install -r requirements.txt" depois de atualizar
        flash(i18n.t("company.pdf_missing_lib"), "error")
        return redirect(links("report", periodo=period, data=ref))
    data = report_data(company, period, ref, links)
    tag = {"semana": data["start"], "mes": data["start"][:7], "ano": data["start"][:4]}[period]
    return send_file(BytesIO(pdf_report.build_company(data, company["name"], branding.app_name())),
                     mimetype="application/pdf", as_attachment=True, download_name=f"report-{period}-{tag}.pdf")


def _company():
    return {"id": g.portal["company_id"], "name": g.portal["company_name"],
            "color": g.portal["company_color"], "tier": g.portal["company_tier"]}


@bp.route("/")
@portal_required
def days():
    company = _company()
    return days_page(company, Links(company))


@bp.route("/gardens")
@portal_required
def gardens():
    company = _company()
    return gardens_page(company, Links(company))


@bp.route("/gardens/<int:client_id>")
@portal_required
def garden(client_id):
    company = _company()
    return garden_page(company, client_id, Links(company))


@bp.route("/service/<int:job_id>")
@portal_required
def service(job_id):
    company = _company()
    return service_page(company, job_id, Links(company))


@bp.route("/report")
@portal_required
def report():
    company = _company()
    return report_page(company, Links(company))


@bp.route("/report/pdf")
@portal_required
def report_pdf():
    company = _company()
    return report_pdf_file(company, Links(company))


@bp.route("/service/<int:job_id>/photo/<filename>")
@portal_required
def photo(job_id, filename):
    """Só as fotos dos serviços que a empresa pode ver (e só as daquele serviço)."""
    if not visible(g.portal["company_id"], job_id) or not photos.job_photo_exists(job_id, filename):
        abort(404)
    return photos.send_photo(photos.job_dir(job_id), filename, mini=request.args.get("mini") == "1")


# ---------- Comentários ----------

_INVISIBLE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f​-‏ -‮⁠-⁤﻿]")


def clean_comment(text):
    """Tira caractere invisível (um comentário só de espaço "invisível" conta como vazio) e linhas em branco demais."""
    text = _INVISIBLE.sub("", (text or "").replace("\r\n", "\n").replace("\r", "\n"))
    return re.sub(r"\n{3,}", "\n\n", text).strip()[:MAX_COMMENT]


def thread(job_id, company_id=None, internal=False):
    """A conversa de um serviço, com o nome da empresa de cada comentário. Com company_id, só a conversa com
    essa empresa: se o jardim mudar de empresa, a nova não vê o que foi falado com a antiga.
    internal=True é a outra conversa: a da equipe entre si (jobs.py), que a empresa nunca vê."""
    sql = ("SELECT m.*, co.name AS company_name FROM job_comments m LEFT JOIN companies co ON co.id = m.company_id "
           "WHERE m.job_id = ? AND m.internal = ?")
    args = [job_id, 1 if internal else 0]
    if company_id is not None:
        sql, args = sql + " AND m.company_id = ?", args + [company_id]
    return [dict(r) for r in get_db().execute(sql + " ORDER BY m.id", args)]


def _mark_seen(ids):
    """Marca como lidos só os comentários que a pessoa viu (e só quando ela abriu a página de verdade)."""
    if request.method != "GET" or not ids:
        return
    db = get_db()
    db.execute(f"UPDATE job_comments SET seen_at = ? WHERE seen_at IS NULL AND id IN ({','.join('?' * len(ids))})",
               (utils.now_utc_iso(), *ids))
    db.commit()


def add_comment(job_id, body, company_id, user=None, portal=None, internal=False):
    """Grava um comentário na conversa com a empresa, ou na da equipe (internal) (o commit fica com quem chamou).
    portal = quem da empresa escreveu; user = quem da equipe."""
    author = portal if portal is not None else user
    cur = get_db().execute(
        "INSERT INTO job_comments (job_id, company_id, from_company, user_id, company_user_id, author_name, body, "
        "created_at, internal) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (job_id, company_id, 1 if portal is not None else 0, None if portal is not None else user["id"],
         portal["id"] if portal is not None else None, author["name"], body, utils.now_utc_iso(), 1 if internal else 0))
    return cur.lastrowid


def snippet(body):
    """O comentário numa linha só, curto, pro aviso no sininho."""
    text = " ".join(body.split())
    return text if len(text) <= 160 else text[:157] + "…"


def staff_to_tell():
    """Quem da equipe fica sabendo dos comentários: os donos e os gerentes com acesso à agenda."""
    return [r["id"] for r in get_db().execute(
        "SELECT id, role, permissions FROM users WHERE active = 1 AND role IN ('owner', 'manager') ORDER BY id")
        if r["role"] == "owner" or "schedule" in (r["permissions"] or "").split(",")]


def staff_view(job):
    """O que a equipe vê na página do trabalho: a conversa com a empresa. Abrir marca os comentários
    da empresa como lidos (os novos continuam destacados nesta visita)."""
    company = get_db().execute("SELECT id, name FROM companies WHERE id = ?", (job["client_company_id"],)).fetchone()
    if company is None:
        return None
    comments = attach_files(thread(job["id"]),  # a equipe vê tudo, cada comentário com o nome da empresa dele
                            lambda file_id, mini=False: url_for("companies.comment_file", job_id=job["id"], file_id=file_id,
                                                                **({"mini": 1} if mini else {})))
    fresh = {m["id"] for m in comments if m["from_company"] and not m["seen_at"]}
    _mark_seen(fresh)
    return {"company": company, "comments": comments, "fresh": fresh, "max_files": MAX_FILES, "max_doc_mb": MAX_DOC_MB}


def team_view(job):
    """A conversa da equipe sobre o trabalho (dono, gerentes e quem está escalado). Sem "novo": o aviso no
    sininho já leva cada pessoa até aqui."""
    comments = attach_files(thread(job["id"], internal=True),
                            lambda file_id, mini=False: url_for("jobs.team_comment_file", job_id=job["id"], file_id=file_id,
                                                                **({"mini": 1} if mini else {})))
    return {"comments": comments, "max_files": MAX_FILES, "max_doc_mb": MAX_DOC_MB}


def coalesce_notice(db, user_id, kind, job_id, params):
    """Um aviso só por conversa: quem ainda não leu o aviso deste trabalho fica com o texto do último comentário."""
    unread = db.execute("SELECT id FROM notifications WHERE user_id = ? AND job_id = ? AND kind = ? AND read_at IS NULL "
                        "ORDER BY id DESC LIMIT 1", (user_id, job_id, kind)).fetchone()
    if unread:
        db.execute("UPDATE notifications SET params = ? WHERE id = ?", (json.dumps(params, ensure_ascii=False), unread["id"]))
    else:
        notifications.notify(user_id, kind, job_id, **params)


MAX_PER_HOUR = 20  # comentários por hora de cada login de empresa (cada um vira aviso e e-mail pra equipe)


@bp.route("/service/<int:job_id>/comment", methods=("POST",))
@portal_required
def comment(job_id):
    company = _company()
    rows = services(company["id"], "j.id = ?", (job_id,))
    if not rows:
        abort(404)
    back = redirect(url_for("portal.service", job_id=job_id, _anchor="comments"))
    body, files = clean_comment(request.form.get("body", "")), picked_files()
    db = get_db()
    hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
    if db.execute("SELECT COUNT(*) FROM job_comments WHERE company_user_id = ? AND created_at >= ?",
                  (g.portal["id"], hour_ago)).fetchone()[0] >= MAX_PER_HOUR:
        flash(i18n.t("portal.comment_too_many"), "error")
        return back
    comment_id, problems = create_comment(job_id, company["id"], body, files, portal=g.portal)
    for message, category in problems:
        flash(message, category)
    if comment_id is None:
        return back
    s = rows[0]
    names = [r["original"] for r in db.execute("SELECT original FROM comment_files WHERE comment_id = ? ORDER BY id", (comment_id,))]
    params = {"company": company["name"], "client": s["garden"], "title": s["title"], "date": s["job_date"],
              "author": g.portal["name"], "text": snippet(body) if body else snippet("📎 " + ", ".join(names))}
    for user_id in staff_to_tell():  # no sininho (e por e-mail, se ligado, depois que a página sai)
        coalesce_notice(db, user_id, notifications.COMPANY_COMMENT, job_id, params)
    db.commit()
    if not problems:
        flash(i18n.t("portal.comment_sent"), "ok")
    return back


# ---------- Anexos dos comentários: fotos e documentos, dos dois lados ----------

MAX_FILES = 6    # por comentário
MAX_DOC_MB = 10  # por documento (as fotos são reduzidas, então o tamanho delas não importa)
DOC_TYPES = {    # extensão: (tipo do arquivo, etiqueta, como o arquivo começa por dentro)
    ".pdf": ("application/pdf", "PDF", (b"%PDF",)),
    ".docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "DOC", (b"PK\x03\x04",)),
    ".doc": ("application/msword", "DOC", (b"\xd0\xcf\x11\xe0",)),
    ".xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "XLS", (b"PK\x03\x04",)),
    ".xls": ("application/vnd.ms-excel", "XLS", (b"\xd0\xcf\x11\xe0",)),
}


def picked_files():
    return [f for f in request.files.getlist("files") if f and f.filename]


def _ext(name):
    return os.path.splitext(name or "")[1].lower()


def _original_name(file_storage):
    name = _INVISIBLE.sub("", os.path.basename((file_storage.filename or "").replace("\\", "/"))).strip()
    return name[-120:] or "file"


def _size_text(size):
    return f"{max(1, round(size / 1024))} KB" if size < 1024 * 1024 else f"{size / 1048576:.1f} MB"


def save_files(job_id, comment_id, files):
    """Grava os anexos de um comentário (o commit fica com quem chamou). Documento: PDF, Word ou Excel de verdade
    (confere por dentro, não só o nome), até MAX_DOC_MB. Qualquer outro arquivo tenta abrir como foto.
    Devolve (quantos entraram, quantos ficaram de fora, acabou o espaço?)."""
    folder, db = photos.comment_dir(job_id), get_db()
    limit = photos.space_limit()
    used = photos.space_used() if limit else 0
    saved = bad = 0
    no_space = False
    for file_storage in files:
        if limit and used >= limit:
            no_space = True
            continue
        original, ext = _original_name(file_storage), _ext(file_storage.filename)
        try:
            if ext in DOC_TYPES:
                data = file_storage.stream.read(MAX_DOC_MB * 1024 * 1024 + 1)
                if len(data) > MAX_DOC_MB * 1024 * 1024 or not data.startswith(DOC_TYPES[ext][2]):
                    bad += 1
                    continue
                folder.mkdir(parents=True, exist_ok=True)
                filename, kind = f"{secrets.token_hex(8)}{ext}", "doc"
                (folder / filename).write_bytes(data)
                size = len(data)
            else:
                filename, kind = photos.store_photo(file_storage, folder), "photo"
                if filename is None:
                    bad += 1
                    continue
                size = (folder / filename).stat().st_size
        except OSError:  # o disco encheu de verdade
            no_space = True
            continue
        used += size
        db.execute("INSERT INTO comment_files (comment_id, kind, filename, original, size) VALUES (?, ?, ?, ?, ?)",
                   (comment_id, kind, filename, original, size))
        saved += 1
    return saved, bad, no_space


def create_comment(job_id, company_id, body, files, user=None, portal=None, internal=False):
    """Grava o comentário com os anexos. Devolve (id, [(mensagem, categoria)]); id None = não gravou nada."""
    t = i18n.t
    if len(files) > MAX_FILES:
        return None, [(t("portal.files_too_many", n=MAX_FILES), "error")]
    if not body and not files:
        return None, [(t("portal.comment_empty"), "error")]
    db = get_db()
    comment_id = add_comment(job_id, body, company_id, user=user, portal=portal, internal=internal)
    saved, bad, no_space = save_files(job_id, comment_id, files)
    problems = []
    if bad:
        problems.append((t("portal.files_bad_one", mb=MAX_DOC_MB) if bad == 1 else t("portal.files_bad_many", n=bad, mb=MAX_DOC_MB), "info"))
    if no_space:
        problems.append((t("photos.no_space"), "info"))
    if not body and not saved:  # era só anexo, e nenhum serviu: não fica comentário vazio
        db.rollback()
        return None, [(message, "error") for message, _category in problems]
    db.commit()
    if problems:
        problems.insert(0, (t("portal.comment_sent"), "ok"))
    return comment_id, problems


def attach_files(comments, url):
    """Põe em cada comentário os anexos dele, com o endereço de cada um (url(id do anexo, miniatura?))."""
    by_id = {m["id"]: m for m in comments}
    for m in comments:
        m["files"] = []
    ids = list(by_id)
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        for f in get_db().execute(f"SELECT * FROM comment_files WHERE comment_id IN ({','.join('?' * len(chunk))}) "
                                  "ORDER BY id", chunk):
            item = dict(f, url=url(f["id"]), size_text=_size_text(f["size"]))
            if f["kind"] == "photo":
                item["mini"] = url(f["id"], True)
            else:
                item["label"] = DOC_TYPES.get(_ext(f["filename"]), ("", "DOC"))[1]
            by_id[f["comment_id"]]["files"].append(item)
    return comments


def remove_files(comment_id, job_id):
    """Apaga do disco os anexos de um comentário (as linhas somem junto com o comentário)."""
    folder = photos.comment_dir(job_id)
    for f in get_db().execute("SELECT filename FROM comment_files WHERE comment_id = ?", (comment_id,)).fetchall():
        photos.remove_photo_files(folder, f["filename"])  # a foto e a miniatura (documento não tem)


def send_comment_file(job_id, file_id, company_id=None, internal=False):
    """Um anexo da conversa. Com company_id, só se o comentário é da conversa com essa empresa.
    internal=True: só anexo da conversa da equipe (a empresa nunca chega aqui)."""
    row = get_db().execute(
        "SELECT f.*, m.company_id, m.internal FROM comment_files f JOIN job_comments m ON m.id = f.comment_id "
        "WHERE f.id = ? AND m.job_id = ?", (file_id, job_id)).fetchone()
    if row is None or (company_id is not None and row["company_id"] != company_id) or bool(row["internal"]) != internal:
        abort(404)
    folder = photos.comment_dir(job_id)
    if row["kind"] == "photo":
        return photos.send_photo(folder, row["filename"], mini=request.args.get("mini") == "1")
    mimetype = DOC_TYPES.get(_ext(row["filename"]), ("application/octet-stream",))[0]
    response = send_from_directory(folder, row["filename"], mimetype=mimetype, max_age=7 * 24 * 3600,
                                   as_attachment=mimetype != "application/pdf",  # PDF abre no celular; Word e Excel baixam
                                   download_name=row["original"] or row["filename"])
    response.cache_control.public = False
    response.cache_control.private = True
    return response


@bp.route("/service/<int:job_id>/file/<int:file_id>")
@portal_required
def comment_file(job_id, file_id):
    if not visible(g.portal["company_id"], job_id):
        abort(404)
    return send_comment_file(job_id, file_id, g.portal["company_id"])


# ---------- A resposta da equipe vai por e-mail pra empresa (se os avisos por e-mail estiverem ligados) ----------

def queue_reply_mail(job_id, comment_id):
    g.setdefault("company_mail", []).append((job_id, comment_id))


def send_reply_mail(job_id, comment_id):
    """Um e-mail pra cada pessoa ativa da empresa, no idioma dela. Devolve quantos saíram."""
    db = get_db()
    row = db.execute(
        "SELECT m.body, m.author_name, m.company_id, j.job_date, j.status, c.name AS garden, c.company_id AS garden_company "
        "FROM job_comments m JOIN jobs j ON j.id = m.job_id JOIN clients c ON c.id = j.client_id "
        "WHERE m.id = ? AND m.job_id = ?", (comment_id, job_id)).fetchone()
    # só pra empresa da conversa, e só se ela ainda vê o serviço (concluído e o jardim ainda é dela)
    if row is None or row["status"] != "done" or not row["company_id"] or row["company_id"] != row["garden_company"]:
        return 0
    brand, link = branding.app_name(), absolute(url_for("portal.service", job_id=job_id, _anchor="comments"))
    files = db.execute("SELECT COUNT(*) FROM comment_files WHERE comment_id = ?", (comment_id,)).fetchone()[0]
    sent = 0
    for person in db.execute("SELECT email, language FROM company_users WHERE company_id = ? AND active = 1 ORDER BY id",
                             (row["company_id"],)).fetchall():
        previous = g.get("lang")
        g.lang = person["language"] if person["language"] in i18n.LANGUAGES else i18n.DEFAULT_LANGUAGE
        try:
            subject = i18n.t("portal.mail_subject", company=brand, garden=row["garden"])
            body = "\n".join([
                i18n.t("portal.mail_intro", name=row["author_name"], company=brand, garden=row["garden"],
                       date=utils.date_long(row["job_date"])),
                "", row["body"], *([i18n.t("portal.mail_files", n=files)] if files else []),
                "", i18n.t("portal.mail_link"), link, "", i18n.t("email.footer", app=brand)])
        finally:
            g.lang = previous
        notifications.send_email(person["email"], subject, body)
        sent += 1
    return sent


@bp.after_app_request
def _after_response(response):
    queued = g.pop("company_mail", None)
    if not queued or response.status_code >= 400 or not notifications.email_enabled():
        return response
    app, base_url = current_app._get_current_object(), request.host_url

    def work():
        with app.test_request_context("/", base_url=base_url):
            for job_id, comment_id in queued:
                try:
                    send_reply_mail(job_id, comment_id)
                except Exception:  # a resposta continua na área da empresa
                    app.logger.exception("Falha ao mandar a resposta pra empresa por e-mail")

    response.call_on_close(work)
    return response


# ---------- Conta (senha e idioma) ----------

@bp.route("/account", methods=("GET", "POST"))
@portal_required
def account():
    if request.method == "POST":
        db = get_db()
        if request.form.get("action") == "language":
            choice = request.form.get("language", "")
            if choice in i18n.LANGUAGES:
                db.execute("UPDATE company_users SET language = ? WHERE id = ?", (choice, g.portal["id"]))
                db.commit()
                flash(i18n.translate("account.language_saved", choice), "ok")  # a mensagem já no idioma novo
            return redirect(url_for("portal.account"))
        if request.form.get("action") == "daily":  # resumo do dia por e-mail (portal_mail.py)
            on = 1 if request.form.get("daily_mail") else 0
            db.execute("UPDATE company_users SET daily_mail = ? WHERE id = ?", (on, g.portal["id"]))
            db.commit()
            flash(i18n.t("portal.daily_on") if on else i18n.t("portal.daily_off"), "ok")
            return redirect(url_for("portal.account"))
        current, new = request.form.get("current_password", ""), request.form.get("new_password", "")
        if not check_password_hash(g.portal["password_hash"], current):
            flash(i18n.t("auth.wrong_current_password"), "error")
        elif (msg := password_error(new, request.form.get("confirm_password", ""))):
            flash(msg, "error")
        else:
            key = new_session_key()  # derruba os outros aparelhos logados com a senha antiga; este continua
            db.execute("UPDATE company_users SET password_hash = ?, session_key = ? WHERE id = ?",
                       (hash_password(new), key, g.portal["id"]))
            db.commit()
            session["portal_key"] = key
            flash(i18n.t("auth.password_changed"), "ok")
            return redirect(url_for("portal.account"))
    return render_template("portal_account.html", languages=i18n.LANGUAGES)
