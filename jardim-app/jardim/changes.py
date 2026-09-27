"""Mudança num trabalho concluído: editar ou excluir precisa do aceite de quem fez.

Depois que um trabalho está concluído, as horas dele são da pessoa que foi. Se o dono (ou um gerente da
agenda) pudesse editar ou apagar sozinho, a pessoa perderia o controle das próprias horas. Então, num
trabalho concluído com gente escalada, Editar e Excluir não acontecem na hora: viram um pedido.
  - Quem está escalado (fora quem pediu) recebe o aviso e vê o pedido na página do trabalho e em Meus
    trabalhos, com "Aceitar" e "Recusar".
  - Todo mundo aceitou: a mudança acontece (a edição é aplicada como se tivesse sido salva agora; a
    exclusão apaga o trabalho). Alguém recusou: nada muda, e quem pediu fica sabendo.
  - Só existe um pedido aberto por trabalho: um novo pedido substitui o anterior.
Trabalhos em aberto, cancelados ou sem ninguém escalado continuam como sempre: mudam na hora.
"""
import json

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for
from werkzeug.datastructures import MultiDict

from . import i18n, notifications, utils
from .auth import login_required
from .db import get_db

bp = Blueprint("changes", __name__)


def needs_consent(job):
    """Trabalho concluído com alguém escalado além de quem está mexendo?"""
    return job["status"] == "done" and any(uid != g.user["id"] for uid in job["people_ids"])


def _people_names(ids):
    if not ids:
        return ""
    marks = ",".join("?" * len(ids))
    return ", ".join(r["name"] for r in get_db().execute(
        f"SELECT name FROM users WHERE id IN ({marks}) ORDER BY name COLLATE NOCASE", list(ids)))


def _load(row):
    if row is None:
        return None
    item = dict(row)
    item["required_ids"] = json.loads(row["required"] or "[]")
    item["accepted_ids"] = json.loads(row["accepted"] or "[]")
    item["waiting_ids"] = [uid for uid in item["required_ids"] if uid not in item["accepted_ids"]]
    item["waiting_names"] = _people_names(item["waiting_ids"])
    return item


def pending_for(job_id):
    """O pedido aberto deste trabalho (ou None), com quem ainda falta aceitar."""
    return _load(get_db().execute(
        "SELECT ch.*, u.name AS requester_name FROM job_changes ch JOIN users u ON u.id = ch.requested_by "
        "WHERE ch.job_id = ? AND ch.status = 'pending' ORDER BY ch.id DESC LIMIT 1", (job_id,)).fetchone())


def waiting_for(user_id):
    """Pedidos que esperam o aceite desta pessoa (pra Meus trabalhos)."""
    rows = get_db().execute(
        "SELECT ch.*, u.name AS requester_name, j.job_date, c.name AS client_name FROM job_changes ch "
        "JOIN users u ON u.id = ch.requested_by JOIN jobs j ON j.id = ch.job_id JOIN clients c ON c.id = j.client_id "
        "WHERE ch.status = 'pending' ORDER BY ch.id DESC").fetchall()
    out = []
    for r in rows:
        item = _load(r)
        if user_id in item["waiting_ids"]:
            out.append(item)
    return out


def request_change(job, kind, payload=None):
    """Registra o pedido (substituindo um anterior em aberto) e avisa quem precisa aceitar."""
    db = get_db()
    required = [uid for uid in job["people_ids"] if uid != g.user["id"]]
    db.execute("UPDATE job_changes SET status = 'cancelled', decided_at = ? WHERE job_id = ? AND status = 'pending'",
               (utils.now_utc_iso(), job["id"]))
    cur = db.execute(
        "INSERT INTO job_changes (job_id, kind, payload, requested_by, required, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (job["id"], kind, json.dumps(payload or {}, ensure_ascii=False), g.user["id"], json.dumps(required), utils.now_utc_iso()))
    params = {"client": job["client_name"], "date": job["job_date"], "author": g.user["name"], "what": kind}
    for uid in required:
        notifications.notify(uid, notifications.CHANGE_REQUESTED, job["id"], **params)
    db.commit()
    flash(i18n.t("changes.sent", names=_people_names(required)), "ok")
    return cur.lastrowid


def _get(job_id, change_id):
    row = get_db().execute(
        "SELECT ch.*, u.name AS requester_name FROM job_changes ch JOIN users u ON u.id = ch.requested_by "
        "WHERE ch.id = ? AND ch.job_id = ? AND ch.status = 'pending'", (change_id, job_id)).fetchone()
    if row is None:
        abort(404)
    return _load(row)


def _apply(change, job):
    """Todo mundo aceitou: faz a mudança. Devolve pra onde ir depois."""
    from . import jobs  # importado aqui: jobs.py usa este arquivo
    db = get_db()
    if change["kind"] == "delete":
        jobs.perform_delete(job)
        return url_for("jobs.list_jobs")
    form = MultiDict()
    for key, values in json.loads(change["payload"] or "{}").items():
        for value in (values if isinstance(values, list) else [values]):
            form.add(key, value)
    data, tasks, errors = jobs._read_job_form(with_status=True, form=form)
    if errors:  # o pedido ficou velho (ex.: cliente apagado): não dá pra aplicar
        db.execute("UPDATE job_changes SET status = 'cancelled', note = ?, decided_at = ? WHERE id = ?",
                   (" ".join(errors), utils.now_utc_iso(), change["id"]))
        db.commit()
        flash(i18n.t("changes.stale"), "error")
        return url_for("jobs.job_detail", job_id=job["id"])
    jobs.perform_edit(job, data, tasks)
    return url_for("jobs.job_detail", job_id=job["id"])


@bp.route("/trabalhos/<int:job_id>/mudancas/<int:change_id>/aceitar", methods=("POST",))
@login_required
def accept(job_id, change_id):
    from . import jobs
    change = _get(job_id, change_id)
    if g.user["id"] not in change["waiting_ids"]:
        abort(403)
    db = get_db()
    accepted = change["accepted_ids"] + [g.user["id"]]
    db.execute("UPDATE job_changes SET accepted = ? WHERE id = ?", (json.dumps(accepted), change_id))
    db.commit()
    if any(uid not in accepted for uid in change["required_ids"]):  # ainda falta gente
        flash(i18n.t("changes.accepted_waiting"), "ok")
        return redirect(url_for("jobs.job_detail", job_id=job_id))
    job = jobs.fetch_jobs("j.id = ?", (job_id,))[0]
    params = {"client": job["client_name"], "date": job["job_date"], "author": g.user["name"], "what": change["kind"]}
    db.execute("UPDATE job_changes SET status = 'accepted', decided_at = ? WHERE id = ?", (utils.now_utc_iso(), change_id))
    notifications.notify(change["requested_by"], notifications.CHANGE_ACCEPTED,
                         job_id if change["kind"] == "edit" else None, **params)
    db.commit()
    target = _apply(change, job)
    flash(i18n.t("changes.applied_delete" if change["kind"] == "delete" else "changes.applied_edit"), "ok")
    return redirect(target)


@bp.route("/trabalhos/<int:job_id>/mudancas/<int:change_id>/recusar", methods=("POST",))
@login_required
def decline(job_id, change_id):
    from . import jobs
    change = _get(job_id, change_id)
    if g.user["id"] not in change["required_ids"]:
        abort(403)
    note = " ".join(request.form.get("note", "").split())[:300]
    job = jobs.fetch_jobs("j.id = ?", (job_id,))[0]
    db = get_db()
    db.execute("UPDATE job_changes SET status = 'declined', note = ?, decided_at = ? WHERE id = ?",
               (note, utils.now_utc_iso(), change_id))
    notifications.notify(change["requested_by"], notifications.CHANGE_DECLINED, job_id, client=job["client_name"],
                         date=job["job_date"], author=g.user["name"], what=change["kind"], note=note)
    db.commit()
    flash(i18n.t("changes.declined_msg"), "ok")
    return redirect(url_for("jobs.job_detail", job_id=job_id))


@bp.route("/trabalhos/<int:job_id>/mudancas/<int:change_id>/cancelar", methods=("POST",))
@login_required
def cancel(job_id, change_id):
    change = _get(job_id, change_id)
    if g.user["id"] != change["requested_by"] and g.user["role"] != "owner":
        abort(403)
    db = get_db()
    db.execute("UPDATE job_changes SET status = 'cancelled', decided_at = ? WHERE id = ?", (utils.now_utc_iso(), change_id))
    db.commit()
    flash(i18n.t("changes.cancelled_msg"), "ok")
    return redirect(url_for("jobs.job_detail", job_id=job_id))
