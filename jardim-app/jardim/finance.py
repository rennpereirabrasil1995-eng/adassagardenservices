"""Finanças (só o dono): o panorama do que está entrando.

Duas fontes de entrada:
  - transferências bancárias que o dono registra à mão (só pra controle: o app não se conecta ao banco);
  - dinheiro que a equipe recebeu dos clientes nos trabalhos (entra aqui sozinho).
O período funciona igual ao do relatório da empresa: dia, semana, mês ou ano, com as setas.
"""
from datetime import date, timedelta

from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from . import i18n, utils
from .auth import permission_required
from .db import get_db
from .reports import _period_label, _read_period  # mesmo período e mesma navegação do relatório

bp = Blueprint("finance", __name__, url_prefix="/financas")


def _short(pence):
    """Valor curto pra caber em cima da barra do gráfico: £120, £1.2k."""
    # arredonda "pra cima" no meio (130.50 -> 131; o round() do Python faria 130) e só depois
    # decide o formato, pra £999.99 virar "£1k" e não "£1000"
    pounds = int(pence / 100 + 0.5)
    if pounds >= 1000:
        tenths = int(pence / 10000 + 0.5)  # décimos de mil libras
        return f"£{tenths / 10:.1f}k".replace(".0k", "k")
    return f"£{pounds}"


def _entries(start, end):
    """Tudo que entrou no período, do mais novo pro mais antigo: transferências + dinheiro dos trabalhos."""
    db = get_db()
    transfers = db.execute(
        "SELECT t.id, t.received_on AS day, t.amount_pence AS pence, t.note, c.name AS client_name "
        "FROM transfers t LEFT JOIN clients c ON c.id = t.client_id WHERE t.received_on BETWEEN ? AND ?",
        (start, end)).fetchall()
    cash = db.execute(
        "SELECT j.id AS job_id, j.job_date AS day, j.cash_pence AS pence, c.name AS client_name, "
        "u.name AS cash_name FROM jobs j JOIN clients c ON c.id = j.client_id "
        "LEFT JOIN users u ON u.id = j.cash_by "
        "WHERE j.cash_pence IS NOT NULL AND j.job_date BETWEEN ? AND ?", (start, end)).fetchall()
    entries = [dict(r, kind="transfer") for r in transfers] + [dict(r, kind="cash") for r in cash]
    entries.sort(key=lambda e: (e["day"], e["kind"], e.get("id") or e.get("job_id") or 0), reverse=True)
    return entries


def _chart(period, start, end, entries):
    """Barras de entradas: um dia por barra (semana e mês) ou um mês por barra (ano)."""
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
    sums = {k: {"transfer": 0, "cash": 0} for k in keys}
    for entry in entries:
        k = key_of(entry["day"])
        if k in sums:
            sums[k][entry["kind"]] += entry["pence"]
    peak = max((v["transfer"] + v["cash"] for v in sums.values()), default=0) or 1
    return [{"label": label_of(k), "title": title_of(k), "transfer": v["transfer"], "cash": v["cash"],
             "total": v["transfer"] + v["cash"], "short": _short(v["transfer"] + v["cash"]),
             "pct": round((v["transfer"] + v["cash"]) * 100 / peak, 1)} for k, v in sums.items()]


def _read_form(db):
    """Lê o formulário de nova transferência. Devolve (o que foi digitado, (dia, pence, cliente), erros)."""
    form = {key: request.form.get(key, "").strip() for key in ("received_on", "client_id", "amount", "note")}
    form["note"] = form["note"][:200]
    errors, pence, client_id = [], None, None
    day = utils.parse_date(form["received_on"])
    if day is None:
        errors.append(i18n.t("finance.date_invalid"))
    try:
        pence = utils.parse_money(form["amount"])
        if pence is None:
            errors.append(i18n.t("finance.amount_required"))
    except ValueError:
        errors.append(i18n.t("finance.amount_invalid"))
    if form["client_id"]:
        row = (db.execute("SELECT id FROM clients WHERE id = ?", (int(form["client_id"]),)).fetchone()
               if form["client_id"].isdigit() else None)
        if row is None:
            errors.append(i18n.t("finance.client_invalid"))
        else:
            client_id = row["id"]
    return form, (day, pence, client_id), errors


@bp.route("/", methods=("GET", "POST"))
@permission_required("finance")
def overview():
    period, ref = _read_period(request.values)  # ao salvar, o período vem em campos escondidos do formulário
    db = get_db()
    form = {"received_on": utils.today_iso(), "client_id": "", "amount": "", "note": ""}
    open_form = False
    if request.method == "POST":
        form, (day, pence, client_id), errors = _read_form(db)
        if not errors:
            db.execute("INSERT INTO transfers (received_on, client_id, amount_pence, note, created_by) "
                       "VALUES (?, ?, ?, ?, ?)", (day.isoformat(), client_id, pence, form["note"], g.user["id"]))
            db.commit()
            flash(i18n.t("finance.added", amount=utils.money(pence)), "ok")
            # abre o período em que a transferência caiu, pra ela já aparecer na tela
            return redirect(url_for("finance.overview", periodo=period, data=day.isoformat()))
        for message in errors:
            flash(message, "error")
        open_form = True

    start, end = utils.period_bounds(period, ref)
    entries = _entries(start, end)
    by_client = {}
    for e in entries:
        name = e["client_name"] or i18n.t("finance.no_client")
        row = by_client.setdefault(name, {"name": name, "transfer": 0, "cash": 0})
        row[e["kind"]] += e["pence"]
    transfer_total = sum(e["pence"] for e in entries if e["kind"] == "transfer")
    cash_total = sum(e["pence"] for e in entries if e["kind"] == "cash")
    today = utils.today_iso()
    return render_template(
        "finance.html", form=form,
        open_form=open_form or db.execute("SELECT 1 FROM transfers LIMIT 1").fetchone() is None,
        clients=db.execute("SELECT id, name FROM clients WHERE active = 1 ORDER BY name COLLATE NOCASE").fetchall(),
        period=period, ref=ref, period_label=_period_label(period, start, end),
        periods=[("dia", i18n.t("history.period_day")), ("semana", i18n.t("history.period_week")),
                 ("mes", i18n.t("history.period_month")), ("ano", i18n.t("history.period_year"))],
        prev_ref=(date.fromisoformat(start) - timedelta(days=1)).isoformat(),
        next_ref=(date.fromisoformat(end) + timedelta(days=1)).isoformat(),
        is_current=start <= today <= end,
        entries=entries, chart=_chart(period, start, end, entries),
        total=transfer_total + cash_total, transfer_total=transfer_total, cash_total=cash_total,
        transfer_count=sum(1 for e in entries if e["kind"] == "transfer"),
        payers=len({e["client_name"] for e in entries if e["client_name"]}),
        by_client=sorted(by_client.values(), key=lambda c: (-(c["transfer"] + c["cash"]), c["name"].lower())),
        next_url=request.full_path,
    )


@bp.route("/transferencias/<int:transfer_id>/excluir", methods=("POST",))
@permission_required("finance")
def delete_transfer(transfer_id):
    db = get_db()
    row = db.execute("SELECT amount_pence FROM transfers WHERE id = ?", (transfer_id,)).fetchone()
    if row is None:
        abort(404)
    db.execute("DELETE FROM transfers WHERE id = ?", (transfer_id,))
    db.commit()
    flash(i18n.t("finance.deleted", amount=utils.money(row["amount_pence"])), "ok")
    return redirect(utils.safe_next(request.form.get("next")) or url_for("finance.overview"))
