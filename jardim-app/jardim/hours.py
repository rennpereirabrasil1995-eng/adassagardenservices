"""Minhas horas: quanto a pessoa trabalhou no dia, na semana, no mês e no ano, com gráficos.

Só pra consultar. As horas saem do Iniciar e do Concluir de cada trabalho (started_at e
finished_at); aqui ninguém digita, soma ou troca hora nenhuma, e a tela não tem formulário.
A regra é a mesma do relatório da empresa, pra os números baterem com o que o dono vê:
  - conta o trabalho concluído, no dia em que ele estava agendado;
  - num trabalho Share, as horas contam inteiras para cada pessoa que foi.
"""
import math
from datetime import date, timedelta

from flask import Blueprint, g, render_template, request, url_for

from . import i18n, utils
from .auth import login_required
from .db import get_db
from .jobs import HAS_PERSON, fetch_jobs
from .reports import _period_label, _read_period

bp = Blueprint("hours", __name__)

TOP_CLIENTS = 6  # no gráfico por cliente; o resto vira "Outros"
GRID_STEPS = (1, 2, 3, 4, 6, 8, 10, 12, 20, 25, 30, 40, 50, 60, 80, 100, 120, 150, 200, 250, 300, 400, 500)


def short_hours(minutes):
    """Rótulo curto em cima da barra: 6h, 6h30, 45m."""
    if not minutes:
        return ""
    h, m = divmod(int(minutes), 60)
    if not h:
        return f"{m}m"
    return f"{h}h" if not m else f"{h}h{m:02d}"


def _scale(peak):
    """Linhas de grade em horas redondas (no máximo 4) e o topo da escala, em minutos."""
    step = next((s * 60 for s in GRID_STEPS if peak <= s * 60 * 4), GRID_STEPS[-1] * 60)
    top = step * max(1, math.ceil(peak / step))
    return step, top


def _chart(period, start, end, by_day, today):
    """Barras: um dia por barra (semana e mês) ou um mês por barra (ano). Tocar na barra abre o dia (ou o mês)."""
    s, e = date.fromisoformat(start), date.fromisoformat(end)
    if period == "ano":
        keys = [f"{s.year}-{m:02d}" for m in range(1, 13)]
        totals = {k: [0, 0] for k in keys}
        for day, (minutes, jobs) in by_day.items():
            totals[day[:7]][0] += minutes
            totals[day[:7]][1] += jobs
        def info(k):
            first = k + "-01"
            # só a inicial embaixo da coluna (J F M A...): 12 nomes de mês não cabem lado a lado no celular;
            # o nome inteiro aparece no valor da coluna e na tabela "Mês a mês"
            return {"label": utils.month_abbr(first)[:1].upper(), "title": f"{utils.month_abbr(first)} {k[:4]}",
                    "link": url_for("hours.my_hours", periodo="mes", data=first), "today": today[:7] == k}
    else:
        keys = [(s + timedelta(days=i)).isoformat() for i in range((e - s).days + 1)]
        totals = {k: list(by_day.get(k, (0, 0))) for k in keys}
        def info(k):
            label = utils.weekday(k) if period == "semana" else (str(int(k[8:])) if int(k[8:]) in (1, 8, 15, 22, 29) else "")
            return {"label": label, "title": utils.date_long(k),
                    "link": url_for("hours.my_hours", periodo="dia", data=k), "today": k == today}
    peak = max((v[0] for v in totals.values()), default=0)
    step, top = _scale(peak) if peak else (60, 60)
    bars = []
    for k in keys:
        minutes, jobs = totals[k]
        bar = info(k)
        tip = i18n.t("hours.bar_jobs_one" if jobs == 1 else "hours.bar_jobs", n=jobs) if jobs else i18n.t("hours.bar_none")
        bar.update(minutes=minutes, jobs=jobs, short=short_hours(minutes), pct=round(minutes * 100 / top, 2),
                   value=utils.format_minutes(minutes) if minutes else "0h", tip=tip)
        bars.append(bar)
    # 7 barras: cabe o número em cima de cada uma; nas outras, linhas de grade. (A chave não se chama
    # "values" porque no template isso seria o método .values() do dicionário.)
    show_values = period == "semana"
    grid = [] if show_values else [{"pct": round(m * 100 / top, 2), "label": short_hours(m)} for m in range(step, top + 1, step)]
    return {"bars": bars, "grid": grid, "show_values": show_values, "dense": period == "mes", "empty": not peak}


def _day_timeline(jobs):
    """O dia em linha do tempo: cada trabalho é uma barra do Iniciar ao Concluir."""
    rows = []
    for j in jobs:
        start, finish = utils.to_local(j["started_at"]), utils.to_local(j["finished_at"])
        if not start or not finish or finish < start:
            continue
        # pela hora do dia em que começou (mesmo que tenha sido feito em outra data) e, se passou
        # da meia-noite (esqueceram de concluir), a barra para no fim do dia
        a = start.hour * 60 + start.minute
        b = min(a + (finish - start).total_seconds() / 60, 1440)
        rows.append({"job": j, "a": a, "b": b})
    if not rows:
        return None
    lo = math.floor(min(r["a"] for r in rows) / 60) * 60
    hi = math.ceil(max(r["b"] for r in rows) / 60) * 60
    if hi - lo < 240:  # pelo menos 4 horas de régua, pra barra curta não virar um tracinho
        hi = min(1440, lo + 240)
        lo = max(0, hi - 240)
    tick = 60 if hi - lo <= 360 else (120 if hi - lo <= 720 else 240)
    hi = lo + math.ceil((hi - lo) / tick) * tick  # a régua termina numa marca (a última hora fica na ponta)
    if hi > 1440:
        lo, hi = lo - (hi - 1440), 1440
    span = hi - lo
    for r in rows:
        r["left"] = round((r["a"] - lo) * 100 / span, 2)
        r["width"] = round((r["b"] - r["a"]) * 100 / span, 2)
    ticks = [{"pct": round((m - lo) * 100 / span, 2), "label": f"{m // 60:02d}:00"} for m in range(lo, hi + 1, tick)]
    return {"rows": rows, "ticks": ticks, "tick_pct": round(tick * 100 / span, 4)}


def summary(user_id, period, ref):
    """Tudo que a tela mostra. Não grava nada."""
    start, end = utils.period_bounds(period, ref)
    today = utils.today_iso()
    jobs = fetch_jobs(f"{HAS_PERSON} AND j.job_date BETWEEN ? AND ? AND j.status IN ('done', 'in_progress')",
                      (user_id, start, end),
                      order=" ORDER BY j.job_date, COALESCE(j.started_at, j.finished_at), j.id")
    done = [j for j in jobs if j["status"] == "done"]
    running = [j for j in jobs if j["status"] == "in_progress"]
    for j in done:
        j["minutes"] = utils.duration_minutes(j["started_at"], j["finished_at"])  # None: concluído sem Iniciar
    no_start = [j for j in done if j["minutes"] is None]
    for j in running:  # "desde 09:00" (ou "desde 25/09 às 09:00", se começou em outro dia)
        same_day = utils.local_date_iso(j["started_at"]) == j["job_date"]
        j["since"] = utils.time_local(j["started_at"]) if same_day else utils.dt_local(j["started_at"])

    by_day, clients = {}, {}
    for j in done:
        minutes = j["minutes"] or 0
        day = by_day.setdefault(j["job_date"], [0, 0])
        day[0] += minutes
        day[1] += 1
        c = clients.setdefault(j["client_id"], {"name": j["client_name"], "minutes": 0, "jobs": 0})
        c["minutes"] += minutes
        c["jobs"] += 1
    total = sum(v[0] for v in by_day.values())
    days_worked = sum(1 for v in by_day.values() if v[0])

    ranked = sorted(clients.values(), key=lambda c: (-c["minutes"], c["name"].lower()))
    by_client = [c for c in ranked[:TOP_CLIENTS] if c["minutes"]]
    rest = ranked[TOP_CLIENTS:]
    if sum(c["minutes"] for c in rest):
        by_client.append({"name": i18n.t("hours.others", n=len(rest)), "minutes": sum(c["minutes"] for c in rest),
                          "jobs": sum(c["jobs"] for c in rest)})

    data = dict(
        period=period, ref=ref, start=start, end=end, period_label=_period_label(period, start, end),
        periods=[("dia", i18n.t("history.period_day")), ("semana", i18n.t("history.period_week")),
                 ("mes", i18n.t("history.period_month")), ("ano", i18n.t("history.period_year"))],
        prev_ref=(date.fromisoformat(start) - timedelta(days=1)).isoformat(),
        next_ref=(date.fromisoformat(end) + timedelta(days=1)).isoformat(),
        is_current=start <= today <= end,
        total_minutes=total, done_count=len(done), days_worked=days_worked,
        average=total // days_worked if days_worked else 0,
        clients_served=len(clients), by_client=by_client,
        client_peak=max((c["minutes"] for c in by_client), default=0),
        jobs=done, running=running, no_start=no_start, has_share=any(j["is_share"] for j in done),
        chart=None, timeline=None, weeks=[], months=[],
    )
    if period == "dia":
        data["timeline"] = _day_timeline(done)
        times = [(utils.to_local(j["started_at"]), utils.to_local(j["finished_at"])) for j in done if j["minutes"] is not None]
        data["first_start"] = min(t[0] for t in times).strftime("%H:%M") if times else ""
        data["last_finish"] = max(t[1] for t in times).strftime("%H:%M") if times else ""
    else:
        data["chart"] = _chart(period, start, end, by_day, today)
    if period == "mes":  # semana a semana (segunda a domingo), só com os dias deste mês
        d, last = date.fromisoformat(start), date.fromisoformat(end)
        while d <= last:
            week_end = min(last, d + timedelta(days=6 - d.weekday()))
            days = [(d + timedelta(days=i)).isoformat() for i in range((week_end - d).days + 1)]
            minutes = sum(by_day.get(k, (0, 0))[0] for k in days)
            count = sum(by_day.get(k, (0, 0))[1] for k in days)
            label = f"{d.day}" + (f"–{week_end.day}" if week_end != d else "") + f" {utils.month_abbr(d.isoformat())}"
            data["weeks"].append({"label": label, "minutes": minutes, "jobs": count,
                                  "link": url_for("hours.my_hours", periodo="semana", data=d.isoformat()),
                                  "current": days[0] <= today <= days[-1]})
            d = week_end + timedelta(days=1)
    if period == "ano":
        for bar in data["chart"]["bars"]:
            data["months"].append({"label": bar["title"], "minutes": bar["minutes"], "jobs": bar["jobs"],
                                   "link": bar["link"], "current": bar["today"]})
    return data


def _owner_name():
    row = get_db().execute("SELECT name FROM users WHERE role = 'owner' AND active = 1 ORDER BY id LIMIT 1").fetchone()
    return row["name"].split(" ")[0] if row else ""


@bp.route("/minhas-horas")
@login_required
def my_hours():
    """Sempre as horas da própria pessoa: não existe parâmetro pra ver as de outra."""
    period, ref = _read_period(request.args)
    return render_template("my_hours.html", owner_name="" if g.user["role"] == "owner" else _owner_name(),
                           **summary(g.user["id"], period, ref))
