"""Serviço extra: um trabalho que a pessoa da equipe fez e que não estava na agenda do dia.

Quem é da equipe (funcionário ou gerente) preenche um formulário curto em Meus trabalhos: cliente (da
lista ou um nome novo), lugar ou jardim, data, hora de início, quanto tempo levou e o que foi feito.
Isso vira um pedido que fica "aguardando aprovação":
  - o dono vê no Painel e no sininho, e aprova ou recusa em /servicos-extras/;
  - aprovado: vira um trabalho concluído na agenda, com a pessoa escalada e as horas (Início e Fim a partir
    da hora e da duração informadas), e passa a contar em Minhas horas;
  - recusado (com um motivo, se o dono quiser): a pessoa vê o aviso e o pedido fica marcado em Minhas horas.
Enquanto não é aprovado, as horas não contam, e Minhas horas mostra o pedido como pendente.
"""
from datetime import datetime, timedelta, timezone

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from . import i18n, notifications, utils
from .auth import login_required, owner_required
from .db import get_db
from .jobs import duration_bounds, parse_tasks

bp = Blueprint("extras", __name__, url_prefix="/servicos-extras")

DEFAULT_START = "08:00"  # sem hora de início, o trabalho entra às 8h do dia informado


def _read_form():
    f = request.form
    data = {
        "client_id": f.get("client_id", "").strip(),
        "client_name": " ".join(f.get("client_name", "").split())[:120],
        "place": " ".join(f.get("place", "").split())[:300],
        "job_date": f.get("job_date", "").strip(),
        "start_time": f.get("start_time", "").strip(),
        "hours": " ".join(f.get("hours", "").split())[:40],
        "tasks": f.get("tasks", ""),
        "notes": f.get("notes", "").strip()[:1000],
    }
    errors, db = [], get_db()
    if data["client_id"]:
        client = db.execute("SELECT id, name, address, postcode FROM clients WHERE id = ?",
                            (data["client_id"],)).fetchone() if data["client_id"].isdigit() else None
        if client is None:
            errors.append(i18n.t("extras.client_invalid"))
        else:
            data["client_name"] = client["name"]
            data["place"] = data["place"] or ", ".join(x for x in (client["address"], client["postcode"]) if x)
    elif not data["client_name"]:
        errors.append(i18n.t("extras.client_required"))
    if utils.parse_date(data["job_date"]) is None:
        errors.append(i18n.t("jobs.date_invalid"))
    elif data["job_date"] > utils.today_iso():
        errors.append(i18n.t("extras.date_future"))
    if data["start_time"] and not utils.valid_time(data["start_time"]):
        errors.append(i18n.t("jobs.time_invalid"))
    bounds = duration_bounds(data["hours"])
    data["minutes"] = bounds[1] if bounds else None
    if not data["minutes"]:
        errors.append(i18n.t("extras.hours_invalid"))
    data["task_list"] = parse_tasks(data["tasks"])
    if not data["task_list"]:
        errors.append(i18n.t("extras.tasks_required"))
    return data, errors


def _owners():
    return [r["id"] for r in get_db().execute("SELECT id FROM users WHERE role = 'owner' AND active = 1 ORDER BY id")]


def _row(report_id):
    row = get_db().execute(
        "SELECT r.*, u.name AS user_name FROM job_reports r JOIN users u ON u.id = r.user_id WHERE r.id = ?",
        (report_id,)).fetchone()
    if row is None:
        abort(404)
    return row


def _params(row):
    return {"client": row["client_name"], "date": row["job_date"], "hours": utils.format_minutes(row["minutes"]),
            "author": row["user_name"]}


def pending_count():
    return get_db().execute("SELECT COUNT(*) FROM job_reports WHERE status = 'pending'").fetchone()[0]


def mine(user_id, limit=20):
    """Os pedidos da pessoa, mais novos primeiro (pra Meus trabalhos e Minhas horas)."""
    return get_db().execute(
        "SELECT r.*, u.name AS user_name FROM job_reports r JOIN users u ON u.id = r.user_id "
        "WHERE r.user_id = ? ORDER BY r.id DESC LIMIT ?", (user_id, limit)).fetchall()


@bp.route("/novo", methods=("GET", "POST"))
@login_required
def new_report():
    clients = get_db().execute("SELECT id, name, address FROM clients WHERE active = 1 ORDER BY name COLLATE NOCASE").fetchall()
    form = {"client_id": "", "client_name": "", "place": "", "job_date": utils.today_iso(), "start_time": "",
            "hours": "", "tasks": "", "notes": ""}
    if request.method == "POST":
        form, errors = _read_form()
        if not errors:
            db = get_db()
            cur = db.execute(
                "INSERT INTO job_reports (user_id, client_id, client_name, place, job_date, start_time, minutes, hours_text, "
                "tasks, notes, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (g.user["id"], int(form["client_id"]) if form["client_id"].isdigit() else None, form["client_name"],
                 form["place"], form["job_date"], form["start_time"], form["minutes"], form["hours"],
                 "\n".join(form["task_list"]), form["notes"], utils.now_utc_iso()))
            row = _row(cur.lastrowid)
            for owner_id in _owners():  # no sininho do dono (e por e-mail, se ligado)
                notifications.notify(owner_id, notifications.EXTRA_REPORTED, **_params(row), report_id=row["id"])
            db.commit()
            flash(i18n.t("extras.sent"), "ok")
            return redirect(url_for("extras.list_reports"))
        for msg in errors:
            flash(msg, "error")
    return render_template("extras_form.html", form=form, clients=clients)


@bp.route("/")
@login_required
def list_reports():
    db = get_db()
    if g.user["role"] == "owner":
        pending = db.execute("SELECT r.*, u.name AS user_name FROM job_reports r JOIN users u ON u.id = r.user_id "
                             "WHERE r.status = 'pending' ORDER BY r.job_date, r.id").fetchall()
        decided = db.execute("SELECT r.*, u.name AS user_name FROM job_reports r JOIN users u ON u.id = r.user_id "
                             "WHERE r.status != 'pending' ORDER BY r.decided_at DESC, r.id DESC LIMIT 30").fetchall()
        return render_template("extras_list.html", pending=pending, decided=decided, own=None)
    return render_template("extras_list.html", pending=None, decided=None, own=mine(g.user["id"], 50))


def _timestamps(day, start_time, minutes):
    """Início e Fim em UTC a partir do dia, da hora (local) e da duração."""
    start = datetime.fromisoformat(f"{day}T{start_time or DEFAULT_START}:00").replace(tzinfo=utils.tz())
    end = start + timedelta(minutes=minutes)
    return (start.astimezone(timezone.utc).isoformat(timespec="seconds"),
            end.astimezone(timezone.utc).isoformat(timespec="seconds"))


@bp.route("/<int:report_id>/aprovar", methods=("POST",))
@owner_required
def approve(report_id):
    row = _row(report_id)
    if row["status"] != "pending":
        abort(404)
    db = get_db()
    client_id = row["client_id"]
    if client_id is None or db.execute("SELECT 1 FROM clients WHERE id = ?", (client_id,)).fetchone() is None:
        cur = db.execute("INSERT INTO clients (name, address) VALUES (?, ?)", (row["client_name"], row["place"]))
        client_id = cur.lastrowid
    started, finished = _timestamps(row["job_date"], row["start_time"], row["minutes"])
    cur = db.execute(
        "INSERT INTO jobs (client_id, title, job_date, start_time, description, status, started_at, finished_at, "
        "planned_minutes, planned_text) VALUES (?, ?, ?, ?, ?, 'done', ?, ?, ?, ?)",
        (client_id, i18n.t("extras.job_title"), row["job_date"], row["start_time"] or DEFAULT_START, row["notes"],
         started, finished, row["minutes"], row["hours_text"]))
    job_id = cur.lastrowid
    db.execute("INSERT INTO job_assignees (job_id, user_id) VALUES (?, ?)", (job_id, row["user_id"]))
    db.executemany("INSERT INTO job_tasks (job_id, description, done, position) VALUES (?, ?, 1, ?)",
                   [(job_id, text, i) for i, text in enumerate(row["tasks"].splitlines())])
    now = utils.now_utc_iso()
    db.execute("UPDATE job_reports SET status = 'approved', decided_by = ?, decided_at = ?, job_id = ? WHERE id = ?",
               (g.user["id"], now, job_id, report_id))
    notifications.notify(row["user_id"], notifications.EXTRA_APPROVED, job_id, **_params(row))
    db.commit()
    flash(i18n.t("extras.approved_msg", client=row["client_name"]), "ok")
    return redirect(url_for("extras.list_reports"))


@bp.route("/<int:report_id>/recusar", methods=("POST",))
@owner_required
def reject(report_id):
    row = _row(report_id)
    if row["status"] != "pending":
        abort(404)
    note = " ".join(request.form.get("note", "").split())[:300]
    db = get_db()
    db.execute("UPDATE job_reports SET status = 'rejected', decided_by = ?, decided_at = ?, decision_note = ? WHERE id = ?",
               (g.user["id"], utils.now_utc_iso(), note, report_id))
    notifications.notify(row["user_id"], notifications.EXTRA_REJECTED, **_params(row), note=note)
    db.commit()
    flash(i18n.t("extras.rejected_msg"), "ok")
    return redirect(url_for("extras.list_reports"))
