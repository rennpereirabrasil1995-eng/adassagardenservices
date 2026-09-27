"""Empresas, do lado do dono (a área que a empresa vê fica em portal.py).

Uma empresa contrata a gente pra vários jardins e quer ver o que foi feito em cada um: hoje ela recebe
as fotos e o relatório pelo WhatsApp. Aqui o dono:
  - cadastra a empresa e liga os jardins dela (cada jardim é um cliente, com clients.company_id);
  - cria o login de quem é da empresa, com a mensagem pronta pra mandar o acesso pelo WhatsApp;
  - vê a área como a empresa vê (prévia) e os comentários que chegaram.
Quem cuida da agenda responde os comentários na página do trabalho (/trabalhos/<id>/comentarios).
Não confundir com os dados da SUA empresa (Conta → Dados da empresa).

Criar login pra alguém de fora e escolher o que ele vê é decisão do dono: essas telas são só dele.

  /empresas/                     as empresas + cadastrar uma nova
  /empresas/<id>                 jardins, quem acessa, o resumo do dia por e-mail e os comentários recentes
  /empresas/<id>/previa/...      ver como a empresa vê (as mesmas telas da área dela)
"""
import json
import smtplib
import sqlite3

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from . import branding, i18n, notifications, portal, portal_mail
from .auth import email_in_use, hash_password, new_session_key, owner_required, password_error, permission_required
from .db import get_db
from .jobs import get_job_or_404
from .team import suggest_password
from .utils import valid_email

bp = Blueprint("companies", __name__)


def _company_or_404(company_id):
    row = get_db().execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()
    if row is None:
        abort(404)
    return row


def _login_or_404(company_id, login_id):
    row = get_db().execute("SELECT * FROM company_users WHERE id = ? AND company_id = ?", (login_id, company_id)).fetchone()
    if row is None:
        abort(404)
    return row


def _back(company_id, anchor=None):
    return redirect(url_for("companies.company_detail", company_id=company_id, _anchor=anchor))


@bp.route("/empresas/")
@owner_required
def list_companies():
    rows = get_db().execute(
        "SELECT co.*, "
        "(SELECT COUNT(*) FROM clients c WHERE c.company_id = co.id) AS gardens, "
        "(SELECT COUNT(*) FROM company_users u WHERE u.company_id = co.id AND u.active = 1) AS logins, "
        "(SELECT COUNT(*) FROM job_comments m WHERE m.company_id = co.id AND m.from_company = 1 AND m.seen_at IS NULL) AS fresh "
        "FROM companies co ORDER BY co.name COLLATE NOCASE").fetchall()
    return render_template("companies_list.html", companies=rows)


@bp.route("/empresas/nova", methods=("POST",))
@owner_required
def new_company():
    name = request.form.get("name", "").strip()[:120]
    if not name:
        flash(i18n.t("companies.name_required"), "error")
        return redirect(url_for("companies.list_companies"))
    db = get_db()
    cur = db.execute("INSERT INTO companies (name) VALUES (?)", (name,))
    db.commit()
    flash(i18n.t("companies.created", name=name), "ok")
    return _back(cur.lastrowid)


@bp.route("/empresas/<int:company_id>")
@owner_required
def company_detail(company_id):
    company = _company_or_404(company_id)
    db = get_db()
    gardens = db.execute(
        "SELECT c.id, c.name, c.address, c.postcode, c.active, "
        "(SELECT MAX(j.job_date) FROM jobs j WHERE j.client_id = c.id AND j.status = 'done') AS last_day "
        "FROM clients c WHERE c.company_id = ? ORDER BY c.name COLLATE NOCASE", (company_id,)).fetchall()
    others = db.execute(  # jardins que dá pra ligar: os ativos que ainda não são desta empresa
        "SELECT c.id, c.name, co.name AS other_company FROM clients c LEFT JOIN companies co ON co.id = c.company_id "
        "WHERE c.active = 1 AND c.company_id IS NOT ? ORDER BY c.name COLLATE NOCASE", (company_id,)).fetchall()
    logins = db.execute("SELECT * FROM company_users WHERE company_id = ? ORDER BY active DESC, name COLLATE NOCASE",
                        (company_id,)).fetchall()
    comments = db.execute(  # a conversa com esta empresa (mesmo de jardim que já saiu dela)
        "SELECT m.*, j.job_date, c.name AS garden FROM job_comments m JOIN jobs j ON j.id = m.job_id "
        "JOIN clients c ON c.id = j.client_id WHERE m.company_id = ? ORDER BY m.id DESC LIMIT 10", (company_id,)).fetchall()
    return render_template("company_detail.html", company=company, gardens=gardens, others=others, logins=logins,
                           comments=comments, languages=i18n.LANGUAGES, daily=_daily(company))


def _daily(company):
    """O quadro "Resumo do dia por e-mail": o horário, pra quem vai e como fica o próximo (a prévia)."""
    people = portal_mail.recipients(company["id"])
    services = portal_mail.pending(company)
    preview = None
    if services:
        first = people[0] if people else None
        preview = portal_mail.compose(company, services, first["name"] if first else "", first["language"] if first else "en")
    return {"hours": portal_mail.HOURS, "email_on": notifications.email_enabled(), "people": people,
            "services": services, "preview": preview,
            "sent_today": company["daily_sent_on"] == portal_mail._now().date().isoformat()}


@bp.route("/empresas/<int:company_id>/resumo", methods=("POST",))
@owner_required
def daily_settings(company_id):
    company = _company_or_404(company_id)
    raw = request.form.get("daily_hour", "")
    hour = int(raw) if raw.isdigit() and int(raw) in portal_mail.HOURS else None  # vazio ou outra coisa: desligado
    db = get_db()
    db.execute("UPDATE companies SET daily_hour = ? WHERE id = ?", (hour, company_id))
    if company["daily_hour"] is None and hour is not None:  # religou: o próximo pega só as últimas 24 horas
        db.execute("UPDATE companies SET daily_sent_at = '' WHERE id = ?", (company_id,))
    db.commit()
    flash(i18n.t("companies.daily_saved") if hour is not None else i18n.t("companies.daily_saved_off"), "ok")
    return _back(company_id, "resumo")


@bp.route("/empresas/<int:company_id>/resumo/mandar", methods=("POST",))
@owner_required
def daily_send_now(company_id):
    """Manda o resumo agora (sem esperar a hora): serve pra testar e pra quando o dia acabou mais cedo."""
    company = _company_or_404(company_id)
    if not notifications.email_enabled():
        flash(i18n.t("companies.daily_no_email"), "error")
        return _back(company_id, "resumo")
    try:
        sent = portal_mail.send(company, force=True)
    except smtplib.SMTPAuthenticationError:
        flash(i18n.t("prefs.test_auth_failed"), "error")
    except Exception as exc:  # rede fora do ar, Gmail bloqueando etc.
        flash(i18n.t("prefs.test_failed", error=exc.__class__.__name__), "error")
    else:
        if sent:
            flash(i18n.t("companies.daily_sent_one") if sent == 1 else i18n.t("companies.daily_sent_many", n=sent), "ok")
        else:
            flash(i18n.t("companies.daily_nothing_to_send"), "info")
    return _back(company_id, "resumo")


@bp.route("/empresas/<int:company_id>/nome", methods=("POST",))
@owner_required
def rename_company(company_id):
    _company_or_404(company_id)
    name = request.form.get("name", "").strip()[:120]
    if not name:
        flash(i18n.t("companies.name_required"), "error")
    else:
        db = get_db()
        db.execute("UPDATE companies SET name = ? WHERE id = ?", (name, company_id))
        db.commit()
        flash(i18n.t("common.changes_saved"), "ok")
    return _back(company_id)


@bp.route("/empresas/<int:company_id>/excluir", methods=("POST",))
@owner_required
def delete_company(company_id):
    """Os jardins continuam no cadastro (só deixam de ser da empresa); os logins dela são apagados."""
    company = _company_or_404(company_id)
    db = get_db()
    db.execute("DELETE FROM companies WHERE id = ?", (company_id,))
    db.commit()
    flash(i18n.t("companies.deleted", name=company["name"]), "ok")
    return redirect(url_for("companies.list_companies"))


# ---------- Jardins da empresa ----------

@bp.route("/empresas/<int:company_id>/jardins", methods=("POST",))
@owner_required
def add_garden(company_id):
    _company_or_404(company_id)
    db = get_db()
    client = db.execute("SELECT id, name FROM clients WHERE id = ?", (request.form.get("client_id", type=int),)).fetchone()
    if client is None:
        flash(i18n.t("companies.pick_garden"), "error")
    else:
        db.execute("UPDATE clients SET company_id = ? WHERE id = ?", (company_id, client["id"]))
        db.commit()
        flash(i18n.t("companies.garden_added", name=client["name"]), "ok")
    return _back(company_id, "jardins")


@bp.route("/empresas/<int:company_id>/jardins/<int:client_id>/tirar", methods=("POST",))
@owner_required
def remove_garden(company_id, client_id):
    db = get_db()
    client = db.execute("SELECT name FROM clients WHERE id = ? AND company_id = ?", (client_id, company_id)).fetchone()
    if client is None:
        abort(404)
    db.execute("UPDATE clients SET company_id = NULL WHERE id = ?", (client_id,))
    db.commit()
    flash(i18n.t("companies.garden_removed", name=client["name"]), "ok")
    return _back(company_id, "jardins")


# ---------- Quem acessa (login de quem é da empresa) ----------

def _access_page(company, login, password, new):
    """A senha aparece só aqui, uma vez, com a mensagem pronta (no idioma da pessoa) pra mandar o acesso."""
    lang = login["language"] if login["language"] in i18n.LANGUAGES else i18n.DEFAULT_LANGUAGE
    url = portal.absolute(url_for("auth.login"))
    message = i18n.translate("companies.access_message", lang, name=login["name"].split()[0], company=branding.app_name(),
                             url=url, email=login["email"], password=password)
    return render_template("company_access.html", company=company, login=login, password=password, url=url,
                           message=message, new=new)


@bp.route("/empresas/<int:company_id>/acessos/novo", methods=("GET", "POST"))
@owner_required
def new_login(company_id):
    company = _company_or_404(company_id)
    form = {"name": "", "email": "", "language": "en", "password": suggest_password()}
    if request.method == "POST":
        f = request.form
        form = {"name": f.get("name", "").strip()[:120], "email": f.get("email", "").strip().lower()[:200],
                "language": f.get("language", "en"), "password": f.get("password", "")}
        if form["language"] not in i18n.LANGUAGES:
            form["language"] = "en"
        errors = []
        if not form["name"]:
            errors.append(i18n.t("team.name_required"))
        if not valid_email(form["email"]):
            errors.append(i18n.t("team.email_invalid"))
        elif email_in_use(form["email"]):
            errors.append(i18n.t("companies.email_taken"))
        if (msg := password_error(form["password"])):
            errors.append(msg)
        if not errors:
            db = get_db()
            try:
                db.execute("INSERT INTO company_users (company_id, name, email, password_hash, language, session_key) "
                           "VALUES (?, ?, ?, ?, ?, ?)", (company_id, form["name"], form["email"],
                                                         hash_password(form["password"]), form["language"], new_session_key()))
                db.commit()
            except sqlite3.IntegrityError:  # alguém cadastrou o mesmo e-mail no mesmo instante
                db.rollback()
                errors.append(i18n.t("companies.email_taken"))
            else:
                return _access_page(company, form, form["password"], new=True)
        for msg in errors:
            flash(msg, "error")
    return render_template("company_login_form.html", company=company, form=form, languages=i18n.LANGUAGES)


@bp.route("/empresas/<int:company_id>/acessos/<int:login_id>/senha", methods=("POST",))
@owner_required
def reset_login(company_id, login_id):
    """Esqueceu a senha: o dono gera uma nova e manda de novo (a antiga para de funcionar)."""
    company, login = _company_or_404(company_id), _login_or_404(company_id, login_id)
    password = suggest_password()
    db = get_db()
    db.execute("UPDATE company_users SET password_hash = ?, session_key = ? WHERE id = ?",  # derruba quem estava logado
               (hash_password(password), new_session_key(), login_id))
    db.commit()
    return _access_page(company, login, password, new=False)


@bp.route("/empresas/<int:company_id>/acessos/<int:login_id>/ativo", methods=("POST",))
@owner_required
def toggle_login(company_id, login_id):
    login = _login_or_404(company_id, login_id)
    db = get_db()
    # desativar troca a chave: nem reativando depois a sessão antiga volta a valer (ela entra de novo com a senha)
    db.execute("UPDATE company_users SET active = ?, session_key = ? WHERE id = ?",
               (0 if login["active"] else 1, new_session_key() if login["active"] else login["session_key"], login_id))
    db.commit()
    flash(i18n.t("companies.login_off" if login["active"] else "companies.login_on", name=login["name"]), "ok")
    return _back(company_id, "acessos")


@bp.route("/empresas/<int:company_id>/acessos/<int:login_id>/excluir", methods=("POST",))
@owner_required
def delete_login(company_id, login_id):
    login = _login_or_404(company_id, login_id)
    db = get_db()
    db.execute("DELETE FROM company_users WHERE id = ?", (login_id,))  # os comentários dela ficam, com o nome
    db.commit()
    flash(i18n.t("companies.login_deleted", name=login["name"]), "ok")
    return _back(company_id, "acessos")


# ---------- Ver como a empresa vê (as telas da área dela, com os links da prévia) ----------

def _preview(company_id):
    row = _company_or_404(company_id)
    company = {"id": row["id"], "name": row["name"]}
    return company, portal.Links(company, preview=True)


@bp.route("/empresas/<int:company_id>/previa/")
@owner_required
def preview_days(company_id):
    company, links = _preview(company_id)
    return portal.days_page(company, links, preview=True)


@bp.route("/empresas/<int:company_id>/previa/jardins")
@owner_required
def preview_gardens(company_id):
    company, links = _preview(company_id)
    return portal.gardens_page(company, links, preview=True)


@bp.route("/empresas/<int:company_id>/previa/jardins/<int:client_id>")
@owner_required
def preview_garden(company_id, client_id):
    company, links = _preview(company_id)
    return portal.garden_page(company, client_id, links, preview=True)


@bp.route("/empresas/<int:company_id>/previa/servico/<int:job_id>")
@owner_required
def preview_service(company_id, job_id):
    company, links = _preview(company_id)
    return portal.service_page(company, job_id, links, preview=True)


# ---------- A equipe responde na página do trabalho ----------

@bp.route("/trabalhos/<int:job_id>/comentarios", methods=("POST",))
@permission_required("schedule")
def reply(job_id):
    job = get_job_or_404(job_id)
    if not job["client_company_id"]:
        abort(404)
    back = redirect(url_for("jobs.job_detail", job_id=job_id, _anchor="comentarios"))
    body = portal.clean_comment(request.form.get("body", ""))
    comment_id, problems = portal.create_comment(job_id, job["client_company_id"], body, portal.picked_files(),
                                                 user=g.user)  # conversa com a empresa de hoje
    if comment_id is None:
        for message, category in problems:
            flash(message, category)
        return back
    if job["status"] == "done":
        portal.queue_reply_mail(job_id, comment_id)  # vai por e-mail pra empresa, se os avisos por e-mail estiverem ligados
        flash(i18n.t("companies.reply_sent", company=job["company_name"]), "ok")
    else:
        flash(i18n.t("companies.reply_saved_open", company=job["company_name"]), "ok")
    for message, category in problems[1:]:  # o primeiro é o "enviado"; o resto, anexo que ficou de fora
        flash(message, category)
    return back


@bp.route("/trabalhos/<int:job_id>/comentarios/arquivo/<int:file_id>")
@permission_required("schedule")
def comment_file(job_id, file_id):
    get_job_or_404(job_id)
    return portal.send_comment_file(job_id, file_id)


@bp.route("/trabalhos/<int:job_id>/comentarios/<int:comment_id>/apagar", methods=("POST",))
@owner_required
def delete_comment(job_id, comment_id):
    get_job_or_404(job_id)
    db = get_db()
    row = db.execute("SELECT body FROM job_comments WHERE id = ? AND job_id = ?", (comment_id, job_id)).fetchone()
    if row is None:
        abort(404)
    names = [f["original"] for f in db.execute("SELECT original FROM comment_files WHERE comment_id = ? ORDER BY id", (comment_id,))]
    text = portal.snippet(row["body"] if row["body"] else "📎 " + ", ".join(names))  # o aviso no sininho com ele também some
    portal.remove_files(comment_id, job_id)  # os anexos saem do disco
    db.execute("DELETE FROM job_comments WHERE id = ?", (comment_id,))
    for notice in db.execute("SELECT id, params FROM notifications WHERE job_id = ? AND kind = ?",
                             (job_id, notifications.COMPANY_COMMENT)).fetchall():
        if json.loads(notice["params"] or "{}").get("text") == text:
            db.execute("DELETE FROM notifications WHERE id = ?", (notice["id"],))
    db.commit()
    flash(i18n.t("companies.comment_deleted"), "ok")
    return redirect(url_for("jobs.job_detail", job_id=job_id, _anchor="comentarios"))
