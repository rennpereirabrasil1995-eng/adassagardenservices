"""Cadastro de clientes (cada cliente = um local de trabalho)."""
from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from . import i18n, photos, quotes, utils
from .auth import can, permission_required
from .db import get_db
from .jobs import fetch_jobs, parse_tasks
from . import utils
from .utils import valid_email

bp = Blueprint("clients", __name__, url_prefix="/clientes")

FIELDS = ("name", "address", "postcode", "phone", "email", "access_notes", "notes", "tier", "task_template")
SAVED = FIELDS + ("reminders",)
TIER_VALUES = {"", "prata", "ouro", "diamante"}


def _get_client_or_404(client_id):
    row = get_db().execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
    if row is None:
        abort(404)
    return row


def _is_owner():
    return g.user["role"] == "owner"


def _companies():
    """Empresas pra escolher no cadastro (só o dono escolhe: a empresa passa a ver os serviços do jardim)."""
    return get_db().execute("SELECT id, name FROM companies ORDER BY name COLLATE NOCASE").fetchall() if _is_owner() else []


def _saved_fields():
    return SAVED + (("company_id",) if _is_owner() else ())


def _read_form():
    data = {field: request.form.get(field, "").strip()[:2000] for field in FIELDS}
    data["name"] = data["name"][:120]
    data["phone"] = utils.read_phone(request.form, "phone")  # com o código do país escolhido
    if data["tier"] not in TIER_VALUES:
        data["tier"] = ""
    data["reminders"] = 1 if request.form.get("reminders") else 0  # lembrete na véspera (Conta → Lembrete pro cliente)
    # uma tarefa por linha; tira linhas vazias e marcadores ("-", "•") de listas coladas
    data["task_template"] = "\n".join(parse_tasks(request.form.get("task_template", "")))
    if _is_owner():  # jardim de uma empresa (área da empresa); empresa que não existe vira "nenhuma"
        chosen = request.form.get("company_id", type=int)
        data["company_id"] = chosen if chosen and get_db().execute(
            "SELECT 1 FROM companies WHERE id = ?", (chosen,)).fetchone() else None
    errors = []
    if not data["name"]:
        errors.append(i18n.t("clients.name_required"))
    if data["email"] and not valid_email(data["email"]):
        errors.append(i18n.t("clients.email_invalid"))
    return data, errors


@bp.route("/")
@permission_required("clients")
def list_clients():
    q = request.args.get("q", "").strip()
    archived = request.args.get("arquivados") == "1"
    sql = ("SELECT c.*, co.name AS company_name, (SELECT COUNT(*) FROM jobs j WHERE j.client_id = c.id) AS jobs_count "
           "FROM clients c LEFT JOIN companies co ON co.id = c.company_id WHERE c.active = ?")
    params = [0 if archived else 1]
    if q:
        sql += " AND (c.name LIKE ? OR c.address LIKE ? OR c.postcode LIKE ?)"
        params += [f"%{q}%"] * 3
    sql += " ORDER BY c.name COLLATE NOCASE"
    rows = get_db().execute(sql, params).fetchall()
    return render_template("clients_list.html", clients=rows, q=q, archived=archived)


@bp.route("/novo", methods=("GET", "POST"))
@permission_required("clients")
def new_client():
    form = {"company_id": request.args.get("empresa", type=int)}  # "Cadastrar jardim novo" na página da empresa
    if request.method == "POST":
        form, errors = _read_form()
        if not errors:
            db = get_db()
            fields = _saved_fields()
            cur = db.execute(
                f"INSERT INTO clients ({', '.join(fields)}) VALUES ({', '.join('?' * len(fields))})",
                [form[f] for f in fields],
            )
            db.commit()
            flash(i18n.t("clients.created"), "ok")
            return redirect(url_for("clients.client_detail", client_id=cur.lastrowid))
        for msg in errors:
            flash(msg, "error")
    return render_template("client_form.html", form=form, client=None, companies=_companies())


@bp.route("/<int:client_id>")
@permission_required("clients")
def client_detail(client_id):
    client = _get_client_or_404(client_id)
    jobs = fetch_jobs("j.client_id = ?", (client_id,),
                      order=" ORDER BY j.job_date DESC, j.id DESC", limit=30)
    gallery = {
        "upload": url_for("photos.upload_client_photos", client_id=client_id),
        "photos": [photos.client_photo_item(client_id, p["id"], p["filename"])
                   for p in photos.fetch_photos(client_id)],
    }
    client_quotes = []
    if can("quotes"):
        today = utils.today_iso()
        client_quotes = [dict(r, ref=quotes.ref(r["number"]), state=quotes.status_of(r, today)) for r in get_db().execute(
            "SELECT * FROM quotes WHERE client_id = ? ORDER BY id DESC LIMIT 20", (client_id,))]
    company = get_db().execute("SELECT id, name FROM companies WHERE id = ?", (client["company_id"],)).fetchone() \
        if client["company_id"] else None
    return render_template("client_detail.html", client=client, jobs=jobs,
                           template_tasks=parse_tasks(client["task_template"]),
                           gallery=gallery, max_photos=photos.MAX_PHOTOS, client_quotes=client_quotes, company=company)


@bp.route("/<int:client_id>/editar", methods=("GET", "POST"))
@permission_required("clients")
def edit_client(client_id):
    client = _get_client_or_404(client_id)
    form = dict(client)
    if request.method == "POST":
        form, errors = _read_form()
        if not errors:
            db = get_db()
            fields = _saved_fields()  # o gerente edita o resto, mas não muda a empresa do jardim
            db.execute(
                f"UPDATE clients SET {', '.join(f + ' = ?' for f in fields)} WHERE id = ?",
                [form[f] for f in fields] + [client_id],
            )
            db.commit()
            flash(i18n.t("common.changes_saved"), "ok")
            return redirect(url_for("clients.client_detail", client_id=client_id))
        for msg in errors:
            flash(msg, "error")
    return render_template("client_form.html", form=form, client=client, companies=_companies())


@bp.route("/<int:client_id>/arquivar", methods=("POST",))
@permission_required("clients")
def toggle_client(client_id):
    client = _get_client_or_404(client_id)
    db = get_db()
    db.execute("UPDATE clients SET active = ? WHERE id = ?", (0 if client["active"] else 1, client_id))
    db.commit()
    flash(i18n.t("clients.archived") if client["active"] else i18n.t("clients.reactivated"), "ok")
    return redirect(url_for("clients.client_detail", client_id=client_id))
