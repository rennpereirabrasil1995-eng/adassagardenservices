"""Equipe: o dono cria, edita e desativa funcionários (e outros donos)."""
import secrets
import sqlite3

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from . import i18n
from .auth import PERMISSIONS, SENSITIVE_PERMISSIONS, email_in_use, hash_password, owner_required, password_error
from .db import get_db
from .utils import read_phone, valid_email

bp = Blueprint("team", __name__, url_prefix="/equipe")

ROLE_KEYS = ("employee", "manager", "owner")


def _role_labels():
    return {"employee": i18n.t("common.role_employee"), "manager": i18n.t("common.role_manager"),
            "owner": i18n.t("common.role_owner")}


def _permission_choices():
    """(chave, texto, é sensível?) de cada acesso que o dono pode dar a um gerente."""
    return [(p, i18n.t(f"teamform.perm_{p}"), p in SENSITIVE_PERMISSIONS) for p in PERMISSIONS]


def _permission_short():
    return {p: i18n.t(f"team.perm_short_{p}") for p in PERMISSIONS}
_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # sem letras/números que se confundem (l, 1, o, 0)


def suggest_password(length=10):
    return "".join(secrets.choice(_ALPHABET) for _ in range(length))


def _get_member_or_404(member_id):
    row = get_db().execute("SELECT * FROM users WHERE id = ?", (member_id,)).fetchone()
    if row is None:
        abort(404)
    return row


def _read_form(member_id=None, is_self=False):
    f = request.form
    data = {
        "name": f.get("name", "").strip()[:120],
        "email": f.get("email", "").strip().lower()[:200],
        "phone": read_phone(f, "phone"),
        "address": " ".join(f.get("address", "").split())[:300],
        "role": f.get("role", "employee"),
        "active": 1 if f.get("active") else 0,
        "password": f.get("password", ""),
    }
    if is_self:  # o dono não pode rebaixar nem desativar a si mesmo
        data["role"], data["active"] = "owner", 1
    # acessos só valem pro gerente; mudou de cargo, eles somem
    data["permissions"] = ",".join(p for p in PERMISSIONS if f.get(f"perm_{p}")) if data["role"] == "manager" else ""
    errors = []
    if not data["name"]:
        errors.append(i18n.t("team.name_required"))
    if not valid_email(data["email"]):
        errors.append(i18n.t("team.email_invalid"))
    if data["role"] not in ROLE_KEYS:
        errors.append(i18n.t("team.role_invalid"))
    if email_in_use(data["email"], user_id=member_id):  # nem da equipe, nem da área de uma empresa
        errors.append(i18n.t("team.email_duplicate"))
    return data, errors


def _has_history(member_id):
    """Está (ou esteve) em algum trabalho, ou anotou dinheiro recebido de cliente: não dá pra excluir."""
    return get_db().execute(
        "SELECT 1 FROM job_assignees WHERE user_id = ? UNION ALL SELECT 1 FROM jobs WHERE cash_by = ? LIMIT 1",
        (member_id, member_id)).fetchone() is not None


@bp.route("/")
@owner_required
def list_team():
    rows = get_db().execute(
        "SELECT u.*, (SELECT COUNT(*) FROM jobs j JOIN job_assignees a ON a.job_id = j.id WHERE a.user_id = u.id "
        "AND j.status IN ('scheduled', 'in_progress')) AS open_jobs "
        "FROM users u ORDER BY u.active DESC, u.role, u.name COLLATE NOCASE"
    ).fetchall()
    return render_template("team_list.html", members=rows, roles=_role_labels(), perm_short=_permission_short())


@bp.route("/novo", methods=("GET", "POST"))
@owner_required
def new_member():
    form = {"role": "employee", "active": 1, "password": suggest_password()}
    if request.method == "POST":
        form, errors = _read_form()
        if (msg := password_error(form["password"])):
            errors.append(msg)
        if not errors:
            db = get_db()
            db.execute(
                "INSERT INTO users (name, email, password_hash, role, phone, address, permissions) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (form["name"], form["email"], hash_password(form["password"]), form["role"], form["phone"],
                 form["address"], form["permissions"]),
            )
            db.commit()
            flash(i18n.t("team.created", name=form["name"]), "ok")
            return redirect(url_for("team.list_team"))
        for msg in errors:
            flash(msg, "error")
    return render_template("team_form.html", form=form, member=None, roles=_role_labels(), is_self=False,
                           permissions=_permission_choices())


@bp.route("/<int:member_id>/editar", methods=("GET", "POST"))
@owner_required
def edit_member(member_id):
    member = _get_member_or_404(member_id)
    is_self = member["id"] == g.user["id"]
    has_jobs = _has_history(member_id)
    form = dict(member)
    form["password"] = ""
    if request.method == "POST":
        form, errors = _read_form(member_id, is_self)
        if form["password"] and (msg := password_error(form["password"])):
            errors.append(msg)
        if not errors:
            db = get_db()
            db.execute(
                "UPDATE users SET name = ?, email = ?, phone = ?, address = ?, role = ?, active = ?, permissions = ? "
                "WHERE id = ?",
                (form["name"], form["email"], form["phone"], form["address"], form["role"], form["active"],
                 form["permissions"], member_id),
            )
            if form["password"]:
                db.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                           (hash_password(form["password"]), member_id))
            db.commit()
            flash(i18n.t("common.changes_saved"), "ok")
            if member["active"] and not form["active"]:
                open_jobs = db.execute(
                    "SELECT COUNT(*) FROM jobs j JOIN job_assignees a ON a.job_id = j.id "
                    "WHERE a.user_id = ? AND j.status IN ('scheduled', 'in_progress')",
                    (member_id,),
                ).fetchone()[0]
                if open_jobs:
                    flash(i18n.t("team.deactivated_with_open_jobs", name=form["name"], n=open_jobs), "info")
            return redirect(url_for("team.list_team"))
        for msg in errors:
            flash(msg, "error")
    return render_template("team_form.html", form=form, member=member, roles=_role_labels(),
                           is_self=is_self, has_jobs=has_jobs, permissions=_permission_choices())


@bp.route("/<int:member_id>/excluir", methods=("POST",))
@owner_required
def delete_member(member_id):
    member = _get_member_or_404(member_id)
    if member["id"] == g.user["id"]:
        flash(i18n.t("team.cannot_delete_self"), "error")
        return redirect(url_for("team.edit_member", member_id=member_id))
    db = get_db()
    if _has_history(member_id):
        flash(i18n.t("team.cannot_delete_has_jobs", name=member["name"]), "error")
        return redirect(url_for("team.edit_member", member_id=member_id))
    try:
        db.execute("DELETE FROM users WHERE id = ?", (member_id,))
        db.commit()
    except sqlite3.IntegrityError:
        db.rollback()
        flash(i18n.t("team.cannot_delete_generic", name=member["name"]), "error")
        return redirect(url_for("team.edit_member", member_id=member_id))
    flash(i18n.t("team.deleted", name=member["name"]), "ok")
    return redirect(url_for("team.list_team"))
