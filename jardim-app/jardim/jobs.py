"""Trabalhos (visitas): agenda do dono, checklist do funcionário, iniciar e concluir."""
import json
import re
from datetime import date, timedelta

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from . import i18n, notifications, photos, quotes, reminders, reports, utils
from .auth import can, login_required, permission_required, permissions_of
from .db import get_db

bp = Blueprint("jobs", __name__)

OPEN_STATUSES = ("scheduled", "in_progress")

# Consulta base: um trabalho + dados do cliente + contagem de tarefas. Quem vai (uma pessoa ou
# várias) vem de job_assignees e é juntado depois, em _attach_people.
JOB_SELECT = """
SELECT j.*,
       c.name AS client_name, c.address AS client_address, c.postcode AS client_postcode,
       c.phone AS client_phone, c.access_notes AS client_access_notes,
       c.company_id AS client_company_id, (SELECT co.name FROM companies co WHERE co.id = c.company_id) AS company_name,
       (SELECT COUNT(*) FROM job_tasks t WHERE t.job_id = j.id) AS tasks_total,
       (SELECT COUNT(*) FROM job_tasks t WHERE t.job_id = j.id AND t.done = 1) AS tasks_done,
       (SELECT COUNT(*) FROM job_tasks t WHERE t.job_id = j.id AND t.skipped = 1) AS tasks_skipped
FROM jobs j
JOIN clients c ON c.id = j.client_id
"""
DEFAULT_ORDER = " ORDER BY j.job_date, COALESCE(NULLIF(j.start_time, ''), '99:99'), j.id"

# Pedaços de consulta pra "trabalhos em que a pessoa está" e "trabalhos sem ninguém".
HAS_PERSON = "EXISTS (SELECT 1 FROM job_assignees a WHERE a.job_id = j.id AND a.user_id = ?)"
NO_PEOPLE = "NOT EXISTS (SELECT 1 FROM job_assignees a WHERE a.job_id = j.id)"


def people_of(job_ids):
    """{id do trabalho: [pessoas]} em ordem alfabética. Cada pessoa: id, name, active."""
    found, ids = {}, list(job_ids)
    for i in range(0, len(ids), 500):  # o SQLite tem limite de "?" por consulta
        chunk = ids[i:i + 500]
        for r in get_db().execute(
                "SELECT a.job_id, u.id, u.name, u.active FROM job_assignees a JOIN users u ON u.id = a.user_id "
                f"WHERE a.job_id IN ({','.join('?' * len(chunk))}) ORDER BY u.name COLLATE NOCASE, u.id", chunk):
            found.setdefault(r["job_id"], []).append({"id": r["id"], "name": r["name"], "active": r["active"]})
    return found


def _attach_people(jobs):
    people = people_of(j["id"] for j in jobs)
    for job in jobs:
        job["people"] = people.get(job["id"], [])
        job["people_ids"] = [p["id"] for p in job["people"]]
        job["employee_name"] = ", ".join(p["name"] for p in job["people"]) or None  # "Ana, Bruno"
        job["is_share"] = len(job["people"]) > 1
        # horas previstas: o texto como foi digitado; num Share, dividido entre as pessoas (2h com 2 = 1h cada)
        keys = job.keys()
        text = (job["planned_text"] if "planned_text" in keys else "") or ""
        minutes = job["planned_minutes"] if "planned_minutes" in keys else None
        job["planned_label"] = text or (utils.format_minutes(minutes) if minutes else "")
        job["each_label"] = ""
        if job["is_share"] and job["planned_label"]:
            bounds = duration_bounds(text) if text else (minutes, minutes)
            if bounds:
                low, high = (b // len(job["people"]) for b in bounds)
                if low:
                    job["each_label"] = utils.format_minutes(low) if low == high else \
                        f"{utils.format_minutes(low)}–{utils.format_minutes(high)}"
    return jobs


def fetch_jobs(where="", params=(), order=DEFAULT_ORDER, limit=None):
    sql = JOB_SELECT + (f" WHERE {where}" if where else "") + order
    if limit:
        sql += f" LIMIT {int(limit)}"
    return _attach_people([dict(r) for r in get_db().execute(sql, params).fetchall()])


def get_job_or_404(job_id):
    """Quem cuida da agenda vê qualquer trabalho; os outros, só aqueles em que estão escalados."""
    rows = fetch_jobs("j.id = ?", (job_id,))
    if not rows:
        abort(404)
    job = rows[0]
    if not can("schedule") and g.user["id"] not in job["people_ids"]:
        abort(404)
    return job


def _save_people(db, job_id, people_ids):
    """Grava quem vai fazer o trabalho (substitui a lista anterior)."""
    db.execute("DELETE FROM job_assignees WHERE job_id = ?", (job_id,))
    db.executemany("INSERT INTO job_assignees (job_id, user_id) VALUES (?, ?)", [(job_id, uid) for uid in people_ids])


def _days_from_today(days):
    return (date.fromisoformat(utils.today_iso()) + timedelta(days=days)).isoformat()


def _weekly_minutes(employee_id):
    monday, sunday = utils.week_bounds(utils.today_iso())
    rows = get_db().execute(
        f"SELECT started_at, finished_at FROM jobs j WHERE {HAS_PERSON} AND status = 'done' "
        "AND job_date BETWEEN ? AND ?",
        (employee_id, monday, sunday),
    ).fetchall()
    return sum(utils.duration_minutes(r["started_at"], r["finished_at"]) or 0 for r in rows)


HISTORY_PERIODS = ("dia", "semana", "mes", "ano")


def _timestamps_for_status(job, new_status):
    """Ajusta horário de início/fim conforme o novo status."""
    started, finished = job["started_at"], job["finished_at"]
    now = utils.now_utc_iso()
    if new_status == "scheduled":
        started = finished = None
    elif new_status == "in_progress":
        started, finished = started or now, None
    elif new_status == "done":
        finished = finished or now
    return started, finished


# ---------- Relatório pro WhatsApp ----------

# Tarefas sobre tirar foto ou vídeo não entram no relatório: as fotos vão separadas.
REPORT_SKIP = re.compile(r"\b(photos?|pictures?|pics|videos?|fotos?|v[ií]deos?|fotografar|filmar)\b", re.IGNORECASE)


def build_report(job, tasks):
    """Monta o relatório no formato do grupo (em inglês):
        Coachmaker Mews – 23/09/2026
        Tasks completed:
        Watered the plants
    A data é o dia em que o trabalho foi concluído (ou o agendado, se não tiver horário de fim).
    Tarefas não feitas entram numa parte separada, com o motivo entre parênteses."""
    day = utils.local_date_iso(job["finished_at"]) or job["job_date"]
    lines = [f"{job['client_name']} – {date.fromisoformat(day):%d/%m/%Y}"]
    done = [t["description"] for t in tasks if t["done"] and not REPORT_SKIP.search(t["description"])]
    skipped = [f"{t['description']} ({t['note']})" if t["note"] else t["description"]
               for t in tasks if t["skipped"] and not REPORT_SKIP.search(t["description"])]
    if done:
        lines += ["Tasks completed:", *done]
    if skipped:
        lines += ([""] if done else []) + ["Tasks not completed:", *skipped]
    if job["cash_pence"] and job["cash_in_report"]:  # o funcionário escolhe se o valor vai no relatório
        lines += ["", f"Cash collected: {utils.money(job['cash_pence'])}"]
    return "\n".join(lines)


# ---------- Formulário de trabalho (dono) ----------

def parse_tasks(text):
    """Uma tarefa por linha; ignora linhas vazias e marcadores como '-' ou '*'."""
    tasks = []
    for line in (text or "").splitlines():
        line = line.strip().lstrip("-•*").strip()
        if line:
            tasks.append(line[:200])
    return tasks[:50]


def _save_tasks(db, job_id, tasks):
    """Regrava a lista de tarefas mantendo o que já foi marcado (feita / não feita + motivo)
    nas tarefas cujo texto não mudou."""
    previous = {}
    for row in db.execute("SELECT description, done, skipped, note FROM job_tasks WHERE job_id = ?", (job_id,)):
        previous.setdefault(row["description"].lower(), []).append((row["done"], row["skipped"], row["note"]))
    db.execute("DELETE FROM job_tasks WHERE job_id = ?", (job_id,))
    for position, text in enumerate(tasks):
        marks = previous.get(text.lower())
        done, skipped, note = marks.pop(0) if marks else (0, 0, "")
        db.execute(
            "INSERT INTO job_tasks (job_id, description, done, skipped, note, position) VALUES (?, ?, ?, ?, ?, ?)",
            (job_id, text, done, skipped, note, position),
        )


def _merge_tasks(picked, extras):
    """Tarefas marcadas da lista padrão + extras, sem repetir (maiúscula/minúscula não conta)."""
    seen, merged = set(), []
    for text in picked + extras:
        if text.lower() not in seen:
            seen.add(text.lower())
            merged.append(text)
    return merged[:50]


def _add_to_template(db, client_id, extras):
    """Junta as tarefas extras à lista padrão do cliente (sem tirar nenhuma que já estava lá)."""
    row = db.execute("SELECT task_template FROM clients WHERE id = ?", (client_id,)).fetchone()
    if row is None:
        return
    current = parse_tasks(row["task_template"])
    have = {t.lower() for t in current}
    added = [t for t in extras if t.lower() not in have]
    if added:
        db.execute("UPDATE clients SET task_template = ? WHERE id = ?", ("\n".join(current + added), client_id))


def _form_options(job=None):
    db = get_db()
    clients = db.execute(
        "SELECT id, name, address, task_template FROM clients WHERE active = 1 ORDER BY name COLLATE NOCASE"
    ).fetchall()
    keep = job["people_ids"] if job else []  # quem já está no trabalho aparece mesmo se foi desativado
    return {
        "clients": clients,
        "client_tasks": {str(c["id"]): parse_tasks(c["task_template"]) for c in clients},
        "employees": db.execute(
            f"SELECT id, name, role, active FROM users WHERE active = 1 OR id IN ({','.join('?' * len(keep)) or 'NULL'}) "
            "ORDER BY name COLLATE NOCASE", keep
        ).fetchall(),
    }


_HOURS = r"h(?:rs?|ours?|oras?)?"
_MINS = r"m(?:in|ins|inutes?|inutos?)?"
_DURATION = re.compile(
    r"(\d{1,2}):(\d{2})"                                      # 1:30
    r"|(\d{1,2})\s*" + _HOURS + r"\s*(\d{1,2})\s*(?:" + _MINS + r")?\b"  # 1h30, 1 hr 30 min
    r"|(\d+(?:[.,]\d+)?)\s*" + _HOURS + r"\b"                 # 2h, 2 hrs, 1.5 hours
    r"|(\d+)\s*" + _MINS + r"\b"                               # 15min, 45 minutes
    r"|(\d+(?:[.,]\d+)?)",                                   # número solto: hora (ou o que vier a seguir)
    re.I)


def duration_bounds(text):
    """Entende as horas previstas escritas de qualquer jeito ("2", "1.5", "1:30", "1h30", "2 hrs",
    "1 to 2hrs", "15min", "45 minutes") e devolve (menor, maior) em minutos. Um número solto vale como
    hora, ou pega a unidade do próximo número ("1 to 2hrs" = 1h a 2h; "15 to 30 min" = 15 a 30 min).
    Sem nenhum tempo reconhecível (ou fora de 1 min a 24h), devolve None."""
    found = []  # (minutos ou número solto, unidade: "h", "min" ou None)
    for m in _DURATION.finditer(text or ""):
        if m.group(1):
            found.append((int(m.group(1)) * 60 + int(m.group(2)), "h"))
        elif m.group(3):
            found.append((int(m.group(3)) * 60 + int(m.group(4)), "h"))
        elif m.group(5):
            found.append((round(float(m.group(5).replace(",", ".")) * 60), "h"))
        elif m.group(6):
            found.append((int(m.group(6)), "min"))
        else:
            found.append((float(m.group(7).replace(",", ".")), None))
    values, unit = [], "h"
    for value, known in reversed(found):  # de trás pra frente: o solto pega a unidade do que vem depois dele
        if known:
            unit = known
            values.append(int(value))
        else:
            values.append(int(round(value if unit == "min" or value > 24 else value * 60)))  # "30" solto = 30 min
    values = [v for v in values if 0 < v <= 24 * 60]
    return (min(values), max(values)) if values else None


def _read_job_form(with_status=False):
    f = request.form
    db = get_db()
    data = {
        "client_id": f.get("client_id", "").strip(),
        "title": f.get("title", "").strip()[:120],
        "job_date": f.get("job_date", "").strip(),
        "start_time": f.get("start_time", "").strip(),
        "planned_hours": " ".join(f.get("planned_hours", "").split())[:40],
        "description": f.get("description", "").strip()[:2000],
        "tasks": f.get("tasks", ""),
        "status": f.get("status", "scheduled"),
    }
    errors = []
    if not (data["client_id"].isdigit() and db.execute(
            "SELECT 1 FROM clients WHERE id = ?", (data["client_id"],)).fetchone()):
        errors.append(i18n.t("jobs.choose_client"))
    # uma pessoa ou várias (trabalho "Share"); ninguém marcado = decidir depois
    data["people"] = []
    for value in f.getlist("assigned_to"):
        value = value.strip()
        if not value:
            continue
        if not (value.isdigit() and db.execute("SELECT 1 FROM users WHERE id = ?", (value,)).fetchone()):
            errors.append(i18n.t("jobs.employee_invalid"))
        elif int(value) not in data["people"]:
            data["people"].append(int(value))
    if not data["title"]:
        errors.append(i18n.t("jobs.title_required"))
    if utils.parse_date(data["job_date"]) is None:
        errors.append(i18n.t("jobs.date_invalid"))
    if data["start_time"] and not utils.valid_time(data["start_time"]):
        errors.append(i18n.t("jobs.time_invalid"))
    bounds = duration_bounds(data["planned_hours"])  # texto livre; guarda o maior tempo entendido, se houver
    data["planned_minutes"] = bounds[1] if bounds else None
    if with_status and data["status"] not in utils.STATUS_LABELS:
        errors.append(i18n.t("jobs.status_invalid"))
    data["quote_id"] = f.get("quote_id", "").strip()  # trabalho que veio de uma cotação aceita
    data["picked"] = [t.strip()[:200] for t in f.getlist("task_pick") if t.strip()]
    data["extras"] = parse_tasks(data["tasks"])
    data["save_template"] = bool(f.get("save_template"))
    return data, _merge_tasks(data["picked"], data["extras"]), errors


# ---------- Telas do dono ----------

@bp.route("/painel")
@permission_required("schedule")
def dashboard():
    today = utils.today_iso()
    db = get_db()
    return render_template(
        "dashboard.html",
        today_jobs=fetch_jobs("j.job_date = ? AND j.status != 'cancelled'", (today,)),
        late_jobs=fetch_jobs("j.job_date < ? AND j.status IN ('scheduled', 'in_progress')", (today,)),
        upcoming_jobs=fetch_jobs("j.job_date > ? AND j.job_date <= ? AND j.status = 'scheduled'",
                                 (today, _days_from_today(7))),
        recent_done=fetch_jobs("j.status = 'done'", order=" ORDER BY j.finished_at DESC, j.id DESC", limit=20),
        unassigned=db.execute(
            f"SELECT COUNT(*) FROM jobs j WHERE {NO_PEOPLE} AND j.status = 'scheduled' AND j.job_date >= ?",
            (today,)).fetchone()[0],
        team_cash=reports.team_cash(*utils.week_bounds(today)),
        reminders_card=reminders.summary(),
    )


VIEW_KEYS = ("proximos", "hoje", "atrasados", "concluidos", "todos")


def _view_labels():
    return [("proximos", i18n.t("common.view_upcoming")), ("hoje", i18n.t("common.view_today")),
            ("atrasados", i18n.t("common.view_late")), ("concluidos", i18n.t("common.view_done")),
            ("todos", i18n.t("common.view_all"))]


def _view_filter(view, today):
    newest_first = " ORDER BY j.job_date DESC, j.id DESC"
    if view == "hoje":
        return "j.job_date = ? AND j.status != 'cancelled'", (today,), DEFAULT_ORDER
    if view == "atrasados":
        return "j.job_date < ? AND j.status IN ('scheduled', 'in_progress')", (today,), DEFAULT_ORDER
    if view == "concluidos":
        return "j.status = 'done'", (), newest_first
    if view == "todos":
        return "", (), newest_first
    return "j.job_date >= ? AND j.status IN ('scheduled', 'in_progress')", (today,), DEFAULT_ORDER


@bp.route("/trabalhos")
@permission_required("schedule")
def list_jobs():
    view = request.args.get("ver", "proximos")
    if view not in VIEW_KEYS:
        view = "proximos"
    employee = request.args.get("func", "")
    q = " ".join(request.args.get("cliente", "").split())[:80]  # a lupa: só pelo nome do cliente
    where, params, order = _view_filter(view, utils.today_iso())
    clauses, params = ([where] if where else []), list(params)
    if employee == "none":
        clauses.append(NO_PEOPLE)
    elif employee.isdigit():
        clauses.append(HAS_PERSON)
        params.append(int(employee))
    if q:
        clauses.append("c.name LIKE ? ESCAPE '\\'")
        params.append("%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
    return render_template(
        "jobs_list.html",
        jobs=fetch_jobs(" AND ".join(clauses), params, order, limit=300),
        view=view, views=_view_labels(), employee=employee, q=q,
        employees=get_db().execute(
            "SELECT id, name FROM users ORDER BY active DESC, name COLLATE NOCASE").fetchall(),
    )


@bp.route("/trabalhos/novo", methods=("GET", "POST"))
@permission_required("schedule")
def new_job():
    if request.method == "POST":
        form, tasks, errors = _read_job_form()
        if not errors:
            db = get_db()
            cur = db.execute(
                "INSERT INTO jobs (client_id, title, job_date, start_time, description, planned_minutes, planned_text) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (int(form["client_id"]), form["title"], form["job_date"], form["start_time"], form["description"],
                 form["planned_minutes"], form["planned_hours"]),
            )
            _save_people(db, cur.lastrowid, form["people"])
            _save_tasks(db, cur.lastrowid, tasks)
            if form["save_template"]:
                _add_to_template(db, int(form["client_id"]), form["extras"])
            if form["quote_id"].isdigit() and can("quotes"):
                quotes.link_job(int(form["quote_id"]), cur.lastrowid)
            notifications.job_created(cur.lastrowid)
            db.commit()
            flash(i18n.t("jobs.scheduled_msg"), "ok")
            return redirect(url_for("jobs.job_detail", job_id=cur.lastrowid))
        for msg in errors:
            flash(msg, "error")
    else:
        form = {"client_id": request.args.get("client", ""), "people": [],
                "job_date": request.args.get("date") or utils.today_iso(),
                "title": "Manutenção do jardim", "start_time": "", "planned_hours": "", "description": "", "tasks": "",
                "picked": None}  # None = todas as tarefas padrão já vêm marcadas
        from_quote = request.args.get("cotacao", "")
        prefill = quotes.job_prefill(int(from_quote)) if from_quote.isdigit() and can("quotes") else None
        if prefill:  # veio de uma cotação aceita: o que foi orçado vira as tarefas
            form.update(prefill, picked=[])
    return render_template("job_form.html", form=form, job=None, **_form_options())


@bp.route("/trabalhos/<int:job_id>/editar", methods=("GET", "POST"))
@permission_required("schedule")
def edit_job(job_id):
    job = get_job_or_404(job_id)
    db = get_db()
    if request.method == "POST":
        form, tasks, errors = _read_job_form(with_status=True)
        if not errors:
            started, finished = _timestamps_for_status(job, form["status"])
            db.execute(
                "UPDATE jobs SET client_id = ?, title = ?, job_date = ?, start_time = ?, "
                "description = ?, status = ?, started_at = ?, finished_at = ?, planned_minutes = ?, planned_text = ? "
                "WHERE id = ?",
                (int(form["client_id"]), form["title"], form["job_date"], form["start_time"], form["description"],
                 form["status"], started, finished, form["planned_minutes"], form["planned_hours"], job_id),
            )
            _save_people(db, job_id, form["people"])
            _save_tasks(db, job_id, tasks)
            if form["save_template"]:
                _add_to_template(db, int(form["client_id"]), form["extras"])
            notifications.job_updated(job, job_id)  # job = como estava antes da edição
            db.commit()
            flash(i18n.t("common.changes_saved"), "ok")
            return redirect(url_for("jobs.job_detail", job_id=job_id))
        for msg in errors:
            flash(msg, "error")
    else:
        task_lines = [t["description"] for t in db.execute(
            "SELECT description FROM job_tasks WHERE job_id = ? ORDER BY position, id", (job_id,))]
        client = db.execute("SELECT task_template FROM clients WHERE id = ?", (job["client_id"],)).fetchone()
        template = {t.lower() for t in parse_tasks(client["task_template"] if client else "")}
        form = dict(job)
        form["people"] = job["people_ids"]
        form["planned_hours"] = job["planned_label"]
        form["picked"] = [t for t in task_lines if t.lower() in template]  # já vêm marcadas na lista
        form["tasks"] = "\n".join(t for t in task_lines if t.lower() not in template)  # o resto vai em "outras"
    return render_template("job_form.html", form=form, job=job, **_form_options(job))


@bp.route("/trabalhos/<int:job_id>/excluir", methods=("POST",))
@permission_required("schedule")
def delete_job(job_id):
    job = get_job_or_404(job_id)
    db = get_db()
    notifications.job_deleted(job)
    db.execute("DELETE FROM jobs WHERE id = ?", (job_id,))  # leva junto tarefas, avisos e fotos (no banco)
    db.commit()
    photos.remove_job_folder(job_id)  # e aqui os arquivos das fotos de Antes e Depois
    photos.remove_comment_folder(job_id)  # e os anexos da conversa com a empresa
    flash(i18n.t("jobs.deleted"), "ok")
    return redirect(url_for("jobs.list_jobs"))


@bp.route("/trabalhos/<int:job_id>/repetir", methods=("POST",))
@permission_required("schedule")
def repeat_job(job_id):
    """Cria cópias futuras do trabalho (ex.: toda semana, por 4 vezes)."""
    job = get_job_or_404(job_id)
    try:
        every, times = int(request.form.get("every", 1)), int(request.form.get("times", 1))
    except ValueError:
        every, times = 0, 0
    if every not in (1, 2, 3, 4) or not 1 <= times <= 26:
        flash(i18n.t("jobs.repeat_choose_valid"), "error")
        return redirect(url_for("jobs.job_detail", job_id=job_id))
    db = get_db()
    tasks = [t["description"] for t in db.execute(
        "SELECT description FROM job_tasks WHERE job_id = ? ORDER BY position, id", (job_id,))]
    base = date.fromisoformat(job["job_date"])
    new_ids = []
    for step in range(1, times + 1):
        cur = db.execute(
            "INSERT INTO jobs (client_id, title, job_date, start_time, description, planned_minutes, planned_text) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (job["client_id"], job["title"], (base + timedelta(weeks=every * step)).isoformat(),
             job["start_time"], job["description"], job["planned_minutes"], job["planned_text"]),
        )
        _save_people(db, cur.lastrowid, job["people_ids"])
        _save_tasks(db, cur.lastrowid, tasks)
        new_ids.append(cur.lastrowid)
    notifications.jobs_repeated(job, new_ids, every)
    db.commit()
    flash(i18n.t("jobs.repeated", n=times), "ok")
    return redirect(url_for("jobs.list_jobs", ver="proximos"))


# ---------- Telas de quem executa (funcionário; o dono também pode ter trabalhos) ----------

@bp.route("/meus-trabalhos")
@login_required
def my_jobs():
    uid, today = g.user["id"], utils.today_iso()
    return render_template(
        "my_jobs.html",
        today_jobs=fetch_jobs(f"{HAS_PERSON} AND j.job_date = ? AND j.status != 'cancelled'", (uid, today)),
        late_jobs=fetch_jobs(f"{HAS_PERSON} AND j.job_date < ? AND j.status IN ('scheduled', 'in_progress')",
                             (uid, today)),
        upcoming_jobs=fetch_jobs(f"{HAS_PERSON} AND j.job_date > ? AND j.job_date <= ? AND j.status = 'scheduled'",
                                 (uid, today, _days_from_today(14))),
        recent_done=fetch_jobs(f"{HAS_PERSON} AND j.status = 'done'", (uid,),
                               order=" ORDER BY j.finished_at DESC, j.id DESC", limit=20),
        weekly_minutes=_weekly_minutes(uid),
    )


@bp.route("/meus-trabalhos/historico")
@login_required
def my_history():
    """O histórico virou parte de "Minhas horas" (mesmos períodos, agora com gráficos)."""
    period = request.args.get("periodo", "semana")
    return redirect(url_for("hours.my_hours", periodo=period if period in HISTORY_PERIODS else "semana"))


@bp.route("/trabalhos/<int:job_id>")
@login_required
def job_detail(job_id):
    job = get_job_or_404(job_id)
    tasks = get_db().execute(
        "SELECT * FROM job_tasks WHERE job_id = ? ORDER BY position, id", (job_id,)).fetchall()
    report = build_report(job, tasks) if job["status"] == "done" else None
    from . import portal  # importado aqui: o portal.py usa este arquivo
    company_thread = None
    if job["client_company_id"] and can("schedule"):  # jardim de uma empresa: a conversa com ela (área da empresa)
        company_thread = portal.staff_view(job)
    return render_template("job_detail.html", job=job, tasks=tasks,
                           report=report, report_rows=(report.count("\n") + 2) if report else 0,
                           can_work=job["status"] in OPEN_STATUSES,
                           photo_sets=_job_photo_sets(job_id), max_photos=photos.MAX_PHOTOS,
                           company_thread=company_thread, team_thread=portal.team_view(job))


# ---------- Conversa da equipe: dono, gerentes e quem está escalado, sobre este trabalho ----------
# Fica na página do trabalho (quem pode abrir a página pode escrever). A empresa e o cliente nunca veem.

def _team_to_tell(job):
    """Quem fica sabendo de uma mensagem: quem está escalado, os donos e os gerentes com acesso à agenda
    (o notify() já pula quem escreveu)."""
    from . import portal
    return list(dict.fromkeys([*job["people_ids"], *portal.staff_to_tell()]))


@bp.route("/trabalhos/<int:job_id>/equipe", methods=("POST",))
@login_required
def team_comment(job_id):
    from . import portal
    job = get_job_or_404(job_id)
    back = redirect(url_for("jobs.job_detail", job_id=job_id, _anchor="equipe"))
    body = portal.clean_comment(request.form.get("body", ""))
    comment_id, problems = portal.create_comment(job_id, None, body, portal.picked_files(), user=g.user, internal=True)
    if comment_id is None:
        for message, category in problems:
            flash(message, category)
        return back
    db = get_db()
    names = [r["original"] for r in db.execute("SELECT original FROM comment_files WHERE comment_id = ? ORDER BY id", (comment_id,))]
    params = {"client": job["client_name"], "title": job["title"], "date": job["job_date"], "author": g.user["name"],
              "text": portal.snippet(body) if body else portal.snippet("📎 " + ", ".join(names))}
    for user_id in _team_to_tell(job):  # no sininho (e por e-mail, se ligado, depois que a página sai)
        portal.coalesce_notice(db, user_id, notifications.TEAM_COMMENT, job_id, params)
    db.commit()
    flash(i18n.t("jobdetail.team_sent"), "ok")
    for message, category in problems[1:]:  # o primeiro é o "enviado"; o resto, anexo que ficou de fora
        flash(message, category)
    return back


@bp.route("/trabalhos/<int:job_id>/equipe/arquivo/<int:file_id>")
@login_required
def team_comment_file(job_id, file_id):
    from . import portal
    get_job_or_404(job_id)
    return portal.send_comment_file(job_id, file_id, internal=True)


@bp.route("/trabalhos/<int:job_id>/equipe/<int:comment_id>/apagar", methods=("POST",))
@login_required
def delete_team_comment(job_id, comment_id):
    """Quem escreveu apaga a própria mensagem; o dono apaga qualquer uma."""
    from . import portal
    get_job_or_404(job_id)
    db = get_db()
    row = db.execute("SELECT body, user_id FROM job_comments WHERE id = ? AND job_id = ? AND internal = 1",
                     (comment_id, job_id)).fetchone()
    if row is None:
        abort(404)
    if row["user_id"] != g.user["id"] and g.user["role"] != "owner":
        abort(403)
    names = [f["original"] for f in db.execute("SELECT original FROM comment_files WHERE comment_id = ? ORDER BY id", (comment_id,))]
    text = portal.snippet(row["body"] if row["body"] else "📎 " + ", ".join(names))  # o aviso no sininho com ele também some
    portal.remove_files(comment_id, job_id)
    db.execute("DELETE FROM job_comments WHERE id = ?", (comment_id,))
    for notice in db.execute("SELECT id, params FROM notifications WHERE job_id = ? AND kind = ?",
                             (job_id, notifications.TEAM_COMMENT)).fetchall():
        if json.loads(notice["params"] or "{}").get("text") == text:
            db.execute("DELETE FROM notifications WHERE id = ?", (notice["id"],))
    db.commit()
    flash(i18n.t("jobdetail.team_deleted"), "ok")
    return redirect(url_for("jobs.job_detail", job_id=job_id, _anchor="equipe"))


# ---------- Fotos de Antes e Depois ----------

def _manages_all_photos():
    """Quem cuida da agenda ou dos clientes apaga qualquer foto; os outros, só as que eles enviaram."""
    return bool(permissions_of(g.user) & {"schedule", "clients"})


def _job_photo_item(job_id, photo_id, filename, can_delete=True):
    return {
        "full": url_for("jobs.job_photo_file", job_id=job_id, filename=filename),
        "mini": url_for("jobs.job_photo_file", job_id=job_id, filename=filename, mini=1),
        "delete": url_for("jobs.delete_job_photo", job_id=job_id, photo_id=photo_id) if can_delete else None,
    }


def _job_photo_sets(job_id):
    """As duas seções da tela do trabalho: Antes e Depois, cada uma com as suas fotos."""
    rows, manage_all = photos.fetch_job_photos(job_id), _manages_all_photos()
    return [{
        "key": slug,
        "title": i18n.t(f"photos.{phase}"),
        "upload": url_for("jobs.upload_job_photos", job_id=job_id, phase=slug),
        "photos": [_job_photo_item(job_id, r["id"], r["filename"],
                                   can_delete=manage_all or r["uploaded_by"] == g.user["id"])
                   for r in rows if r["phase"] == phase],
    } for slug, phase in photos.PHASES.items()]


@bp.route("/trabalhos/<int:job_id>/fotos/<any(antes, depois):phase>", methods=("POST",))
@login_required
def upload_job_photos(job_id, phase):
    """Quem está no trabalho envia as fotos de Antes ou de Depois (pela câmera ou várias da galeria)."""
    get_job_or_404(job_id)
    upload = photos.add_job_photos(job_id, photos.PHASES[phase], request.files.getlist("photo"), g.user["id"])
    items = [_job_photo_item(job_id, photo_id, filename) for photo_id, filename in upload.added]
    return photos.respond(upload, items, url_for("jobs.job_detail", job_id=job_id, _anchor=f"fotos-{phase}"))


@bp.route("/trabalhos/<int:job_id>/fotos/<int:photo_id>/excluir", methods=("POST",))
@login_required
def delete_job_photo(job_id, photo_id):
    get_job_or_404(job_id)
    ok, message = photos.delete_job_photo(job_id, photo_id, g.user, _manages_all_photos())
    return photos.respond_delete(ok, message, url_for("jobs.job_detail", job_id=job_id, _anchor="fotos"))


@bp.route("/trabalhos/<int:job_id>/fotos/<filename>")
@login_required
def job_photo_file(job_id, filename):
    get_job_or_404(job_id)  # só quem pode ver o trabalho vê as fotos dele
    if not photos.job_photo_exists(job_id, filename):
        abort(404)
    return photos.send_photo(photos.job_dir(job_id), filename, mini=request.args.get("mini") == "1")


@bp.route("/trabalhos/<int:job_id>/atualizar", methods=("POST",))
@login_required
def update_progress(job_id):
    """Salva checklist, notas e materiais; e inicia ou conclui o trabalho."""
    job = get_job_or_404(job_id)
    if job["status"] not in OPEN_STATUSES:
        flash(i18n.t("jobs.already_closed"), "error")
        return redirect(url_for("jobs.job_detail", job_id=job_id))
    db = get_db()
    cash, cash_in_report, cash_error = job["cash_pence"], job["cash_in_report"], False
    if "cash" in request.form:  # dinheiro recebido do cliente (em espécie)
        cash_in_report = 1 if request.form.get("cash_in_report") else 0
        try:
            cash = utils.parse_money(request.form["cash"])
        except ValueError:
            cash_error = True  # mantém o valor antigo; o resto do formulário é salvo normalmente
    for task in db.execute("SELECT id FROM job_tasks WHERE job_id = ?", (job_id,)).fetchall():
        choice = request.form.get(f"task_{task['id']}", "")
        if choice == "done":
            marks = (1, 0, "")
        elif choice == "not_done":  # o motivo é opcional: pode enviar em branco
            marks = (0, 1, request.form.get(f"note_{task['id']}", "").strip()[:300])
        else:
            marks = (0, 0, "")
        db.execute("UPDATE job_tasks SET done = ?, skipped = ?, note = ? WHERE id = ?", (*marks, task["id"]))

    action = request.form.get("action", "save")
    if cash_error:
        action = "save"  # não inicia nem conclui com o valor errado: primeiro corrige
    status = job["status"]
    if action == "start":
        status = "in_progress"
    elif action == "finish":
        status = "done"
    started, finished = _timestamps_for_status(job, status)
    same_cash = cash == job["cash_pence"]
    cash_by = job["cash_by"] if same_cash else (g.user["id"] if cash is not None else None)  # quem anotou está com o dinheiro
    db.execute(
        "UPDATE jobs SET employee_notes = ?, materials = ?, cash_pence = ?, cash_in_report = ?, cash_received_at = ?, "
        "cash_by = ?, status = ?, started_at = ?, finished_at = ? WHERE id = ?",
        (request.form.get("employee_notes", "").strip()[:2000], request.form.get("materials", "").strip()[:1000],
         cash, cash_in_report, job["cash_received_at"] if same_cash else None, cash_by,
         status, started, finished, job_id),
    )
    db.commit()

    if cash_error:
        flash(i18n.t("cash.invalid"), "error")
    elif action == "start":
        flash(i18n.t("jobs.started"), "ok")
    elif action == "finish":
        pending, skipped = db.execute(
            "SELECT COALESCE(SUM(done = 0 AND skipped = 0), 0), COALESCE(SUM(skipped), 0) FROM job_tasks WHERE job_id = ?",
            (job_id,)).fetchone()
        message = i18n.t("jobs.finished")
        if skipped:
            message += i18n.t("jobs.finished_skipped_suffix", n=skipped)
        if pending:
            message += i18n.t("jobs.finished_pending_suffix", n=pending)
        flash(message, "ok")
    else:
        flash(i18n.t("jobs.progress_saved"), "ok")
    return redirect(url_for("jobs.job_detail", job_id=job_id))
