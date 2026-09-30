"""Relatório da empresa (só o dono): como a empresa está indo num período.

Trabalhos, horas, dinheiro recebido pela equipe (pra conferir o repasse) e clientes.
O período é dia, semana (segunda a domingo), mês ou ano, e dá pra voltar para os anteriores.
"""
from datetime import date, timedelta
from io import BytesIO

from flask import Blueprint, abort, flash, redirect, render_template, request, send_file, url_for

from . import branding, i18n, utils
from .auth import permission_required
from .db import get_db

bp = Blueprint("reports", __name__, url_prefix="/relatorio")

PERIODS = ("dia", "semana", "mes", "ano")


# O dinheiro de um trabalho fica com quem anotou o valor (jobs.cash_by): num trabalho "Share",
# com várias pessoas, é essa pessoa que tem que repassar.

def team_cash(week_start, week_end):
    """Por pessoa: dinheiro recebido de clientes na semana e quanto ainda falta repassar (de qualquer data)."""
    return get_db().execute(
        "SELECT u.id, u.name, "
        "COALESCE(SUM(CASE WHEN j.job_date BETWEEN ? AND ? THEN j.cash_pence END), 0) AS week, "
        "COALESCE(SUM(CASE WHEN j.cash_received_at IS NULL THEN j.cash_pence END), 0) AS pending "
        "FROM jobs j JOIN users u ON u.id = j.cash_by WHERE j.cash_pence IS NOT NULL "
        "GROUP BY u.id HAVING week > 0 OR pending > 0 ORDER BY u.name COLLATE NOCASE",
        (week_start, week_end),
    ).fetchall()


def pending_cash():
    """Todo dinheiro recebido de clientes que ainda não foi repassado ao dono, de qualquer data."""
    return get_db().execute(
        "SELECT j.id, j.job_date, j.cash_pence, j.cash_received_at, c.name AS client_name, "
        "COALESCE(u.name, '—') AS cash_name FROM jobs j "
        "JOIN clients c ON c.id = j.client_id LEFT JOIN users u ON u.id = j.cash_by "
        "WHERE j.cash_pence IS NOT NULL AND j.cash_received_at IS NULL "
        "ORDER BY cash_name COLLATE NOCASE, j.job_date, j.id"
    ).fetchall()


@bp.route("/dinheiro/<int:job_id>/recebido", methods=("POST",))
@permission_required("cash")
def toggle_cash_received(job_id):
    """Marca que o funcionário já repassou o dinheiro deste trabalho (tocar de novo desfaz)."""
    db = get_db()
    job = db.execute(
        "SELECT j.cash_pence, j.cash_received_at, COALESCE(u.name, '—') AS cash_name FROM jobs j "
        "LEFT JOIN users u ON u.id = j.cash_by WHERE j.id = ?", (job_id,)).fetchone()
    if job is None or not job["cash_pence"]:
        abort(404)
    received = None if job["cash_received_at"] else utils.now_utc_iso()
    db.execute("UPDATE jobs SET cash_received_at = ? WHERE id = ?", (received, job_id))
    db.commit()
    key = "company.cash_marked_received" if received else "company.cash_marked_pending"
    flash(i18n.t(key, amount=utils.money(job["cash_pence"]), name=job["cash_name"]), "ok")
    return redirect(utils.safe_next(request.form.get("next")) or url_for("reports.company_report"))


def _period_label(period, start, end):
    if period == "dia":
        return utils.date_long(start)
    if period == "ano":
        return start[:4]
    if period == "mes":
        return f"{utils.month_abbr(start)} {start[:4]}"
    return f"{utils.day_num(start)} {utils.month_abbr(start)} – {utils.day_num(end)} {utils.month_abbr(end)} {end[:4]}"


def _read_period(args):
    period = args.get("periodo", "semana")
    if period not in PERIODS:
        period = "semana"
    try:
        ref = date.fromisoformat(args.get("data", "")).isoformat()
    except ValueError:
        ref = utils.today_iso()
    return period, ref


def _timeline(period, start, end, jobs):
    """Barras do gráfico: um dia por barra (semana e mês) ou um mês por barra (ano). Cancelados não contam."""
    if period == "dia":
        return []
    s, e = date.fromisoformat(start), date.fromisoformat(end)
    if period == "ano":
        keys = [f"{s.year}-{m:02d}" for m in range(1, 13)]
        key_of = lambda day: day[:7]
        label_of = lambda k: utils.month_abbr(k + "-01")
        title_of = lambda k: f"{utils.month_abbr(k + '-01')} {k[:4]}"
    else:
        keys = [(s + timedelta(days=i)).isoformat() for i in range((e - s).days + 1)]
        key_of = lambda day: day
        if period == "semana":
            label_of = utils.weekday
        else:  # mês: rótulo só em alguns dias, pra não embolar
            label_of = lambda k: str(int(k[8:])) if int(k[8:]) in (1, 8, 15, 22, 29) else ""
        title_of = lambda k: f"{utils.weekday(k)} {int(k[8:])} {utils.month_abbr(k)}"
    counts = {k: [0, 0] for k in keys}
    for j in jobs:
        k = key_of(j["job_date"])
        if j["status"] != "cancelled" and k in counts:
            counts[k][0] += 1
            counts[k][1] += j["status"] == "done"
    peak = max((c[0] for c in counts.values()), default=0) or 1
    return [{"label": label_of(k), "title": title_of(k), "total": counts[k][0], "done": counts[k][1],
             "pct": round(counts[k][0] * 100 / peak, 1)} for k in keys]


def report_data(period, ref):
    """Tudo que o relatório mostra, calculado num lugar só (a tela e o PDF usam os mesmos números)."""
    start, end = utils.period_bounds(period, ref)
    db = get_db()
    jobs = db.execute(
        "SELECT j.*, c.name AS client_name, u.name AS cash_name FROM jobs j "
        "JOIN clients c ON c.id = j.client_id LEFT JOIN users u ON u.id = j.cash_by "
        "WHERE j.job_date BETWEEN ? AND ? ORDER BY j.job_date, j.start_time, j.id",
        (start, end),
    ).fetchall()
    done = [j for j in jobs if j["status"] == "done"]
    minutes = {j["id"]: utils.duration_minutes(j["started_at"], j["finished_at"]) or 0 for j in done}
    crew = {}  # quem estava em cada trabalho concluído
    for r in db.execute(
            "SELECT a.job_id, u.id, u.name FROM job_assignees a JOIN users u ON u.id = a.user_id "
            "JOIN jobs j ON j.id = a.job_id WHERE j.status = 'done' AND j.job_date BETWEEN ? AND ?", (start, end)):
        crew.setdefault(r["job_id"], []).append(r)

    # Por pessoa: trabalhos concluídos e horas (num trabalho Share, contam para cada pessoa que foi)
    # e o dinheiro que ela recebeu dos clientes.
    people = {}
    def person(user_id, name):
        return people.setdefault(user_id, {"name": name, "done": 0, "minutes": 0, "cash": 0})
    total_minutes = 0
    for j in done:
        who = crew.get(j["id"], [])
        for r in who:
            p = person(r["id"], r["name"])
            p["done"] += 1
            p["minutes"] += minutes[j["id"]]
        total_minutes += minutes[j["id"]] * max(1, len(who))  # horas de trabalho da equipe (3 pessoas x 2h = 6h)
    for j in jobs:
        if j["cash_pence"] and j["cash_by"] is not None:
            person(j["cash_by"], j["cash_name"])["cash"] += j["cash_pence"]
    people = sorted(people.values(), key=lambda p: (-p["done"], p["name"].lower()))

    tasks = db.execute(
        "SELECT COALESCE(SUM(t.done), 0) AS done, COALESCE(SUM(t.skipped), 0) AS skipped, "
        "COALESCE(SUM(t.done = 0 AND t.skipped = 0), 0) AS unmarked "
        "FROM job_tasks t JOIN jobs j ON j.id = t.job_id WHERE j.status = 'done' AND j.job_date BETWEEN ? AND ?",
        (start, end),
    ).fetchone()
    clients = db.execute("SELECT active, tier, substr(created_at, 1, 10) AS created FROM clients").fetchall()
    active = [c for c in clients if c["active"]]
    cash_jobs = [j for j in jobs if j["cash_pence"]]
    today = utils.today_iso()

    return dict(
        period=period, ref=ref, start=start, end=end, period_label=_period_label(period, start, end),
        periods=[("dia", i18n.t("history.period_day")), ("semana", i18n.t("history.period_week")),
                 ("mes", i18n.t("history.period_month")), ("ano", i18n.t("history.period_year"))],
        prev_ref=(date.fromisoformat(start) - timedelta(days=1)).isoformat(),
        next_ref=(date.fromisoformat(end) + timedelta(days=1)).isoformat(),
        is_current=start <= today <= end,
        scheduled_count=len(jobs), done_count=len(done),
        cancelled_count=sum(1 for j in jobs if j["status"] == "cancelled"),
        open_count=sum(1 for j in jobs if j["status"] in ("scheduled", "in_progress")),
        total_minutes=total_minutes, has_share=any(len(who) > 1 for who in crew.values()),
        cash_total=sum(j["cash_pence"] for j in cash_jobs),
        cash_received=sum(j["cash_pence"] for j in cash_jobs if j["cash_received_at"]),
        clients_served=len({j["client_id"] for j in done}),
        people=people, max_minutes=max((p["minutes"] for p in people), default=0),
        cash_jobs=cash_jobs, tasks=tasks, timeline=_timeline(period, start, end, jobs),
        late_now=db.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('scheduled', 'in_progress') AND job_date < ?",
                            (today,)).fetchone()[0],
        clients_active=len(active), clients_inactive=len(clients) - len(active),
        tiers={tier: sum(1 for c in active if c["tier"] == tier) for tier in ("diamante", "platina", "ouro", "prata", "")},
        clients_new=sum(1 for c in clients if start <= (c["created"] or "") <= end),
        pending=pending_cash(),
    )


@bp.route("/")
@permission_required("report")
def company_report():
    period, ref = _read_period(request.args)
    return render_template("company_report.html", next_url=request.full_path, **report_data(period, ref))


@bp.route("/pdf")
@permission_required("report")
def company_report_pdf():
    """O mesmo relatório em PDF, pra baixar, imprimir ou mandar pra alguém."""
    period, ref = _read_period(request.args)
    try:
        from . import pdf_report  # o reportlab só é carregado quando alguém baixa um PDF
    except ImportError:  # esqueceram o "pip install -r requirements.txt" depois de atualizar
        flash(i18n.t("company.pdf_missing_lib"), "error")
        return redirect(url_for("reports.company_report", periodo=period, data=ref))
    data = report_data(period, ref)
    row = get_db().execute("SELECT company_name FROM settings WHERE id = 1").fetchone()
    company = (row["company_name"] if row else "") or branding.app_name()
    tag = {"dia": data["start"], "semana": data["start"], "mes": data["start"][:7], "ano": data["start"][:4]}[period]
    return send_file(BytesIO(pdf_report.build(data, company)), mimetype="application/pdf",
                     as_attachment=True, download_name=f"relatorio-{period}-{tag}.pdf")
