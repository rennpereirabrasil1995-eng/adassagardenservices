"""Cotações (orçamentos): quem tem o acesso "Cotações" monta, manda por WhatsApp ou e-mail, e o
cliente abre por um link secreto, sem login e sem senha, pra ver:
  - as fotos da visita, cada uma com um texto embaixo;
  - os itens com preço e o total;
  - e os botões pra aceitar ou recusar.

  /cotacoes/...   telas da equipe (dono e gerentes com o acesso "Cotações")
  /c/<token>      página do cliente. O token é longo e aleatório: ninguém chega nela sem o link.

Quando o cliente abre pela primeira vez, aceita ou recusa, quem fez a cotação e o dono recebem um
aviso (no sininho e, se ligado em Conta → Avisos por e-mail, por e-mail). Cotação aceita vira trabalho com um toque.
"""
import re
import secrets
import smtplib
import sqlite3
from datetime import date, timedelta
from urllib.parse import quote as url_quote

from flask import (Blueprint, abort, current_app, flash, g, redirect, render_template, request, url_for)

from . import branding, i18n, notifications, photos, utils
from .auth import can, permission_required
from .db import get_db

bp = Blueprint("quotes", __name__)

FREQUENCIES = ("once", "weekly", "fortnightly", "monthly")
VALID_DAYS = 30          # validade padrão de uma cotação nova
MAX_ITEMS = 40
# Quem abre o link sozinho só pra montar a pré-visualização (WhatsApp, iMessage, Slack...) não conta
# como "o cliente abriu".
PREVIEW_BOTS = re.compile(
    r"bot|crawl|spider|preview|whatsapp|facebookexternalhit|slack|telegram|discord|skype|embedly|linkedin", re.I)
_QTY = re.compile(r"^\d{1,4}(?:[.,]\d{1,2})?$")


# ---------- Ajudas ----------

def ref(number):
    return f"Q-{int(number):04d}"


def _line_pence(quantity, unit_pence):
    return int(quantity * unit_pence + 0.5)  # arredonda meio pence pra cima


def _fmt_qty(quantity):
    return f"{quantity:g}"  # 1.0 -> "1"; 1.5 -> "1.5"


def _date_full(value):
    """'2026-10-26' -> '26 Oct 2026' (no idioma atual)."""
    return f"{utils.day_num(value)} {utils.month_abbr(value)} {value[:4]}" if utils.parse_date(value) else ""


def status_of(q, today=None):
    """Situação pra mostrar: cancelled, accepted, declined, expired, viewed, sent ou draft."""
    if q["status"] != "open":
        return q["status"]
    if q["valid_until"] < (today or utils.today_iso()):
        return "expired"
    if q["viewed_at"]:
        return "viewed"
    return "sent" if q["sent_at"] else "draft"


def _load(row):
    """A cotação completa: itens (em "lines": "items" no Jinja seria o método do dicionário) com o total
    de cada linha, total geral, fotos e situação."""
    db = get_db()
    q = dict(row)
    q["ref"] = ref(q["number"])
    q["lines"] = []
    for it in db.execute("SELECT * FROM quote_items WHERE quote_id = ? ORDER BY position, id", (q["id"],)):
        item = dict(it)
        item["line_pence"] = _line_pence(item["quantity"], item["unit_pence"])
        item["qty_text"] = _fmt_qty(item["quantity"])
        q["lines"].append(item)
    q["total_pence"] = sum(i["line_pence"] for i in q["lines"])
    q["photos"] = db.execute("SELECT * FROM quote_photos WHERE quote_id = ? ORDER BY id", (q["id"],)).fetchall()
    q["state"] = status_of(q)
    q["recurring"] = q["frequency"] != "once"
    return q


def _get_or_404(quote_id):
    row = get_db().execute("SELECT * FROM quotes WHERE id = ?", (quote_id,)).fetchone()
    if row is None:
        abort(404)
    return _load(row)


def _by_token_or_404(token):
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", token or ""):
        abort(404)
    row = get_db().execute("SELECT * FROM quotes WHERE token = ?", (token,)).fetchone()
    if row is None:
        abort(404)
    return _load(row)


def _company():
    row = notifications.settings()
    return {"name": row["company_name"] or branding.app_name(), "phone": row["company_phone"],
            "email": row["company_email"], "address": row["company_address"]}


def _public_url(q):
    base = current_app.config.get("APP_URL") or request.host_url
    return base.rstrip("/") + url_for("quotes.public_quote", token=q["token"])


def _wa_number(phone):
    """Telefone do jeito que o link do WhatsApp quer: só números, com o 44 do Reino Unido na frente."""
    return utils.intl_phone(phone)


def _in_language(q, key, **kwargs):
    return i18n.translate(key, q["language"], **kwargs)


def _price_line(q, lang=None):
    """'£120' ou '£60 per visit, every 2 weeks' no idioma da cotação."""
    lang = lang or q["language"]
    total = utils.money(q["total_pence"]) or "£0"
    if not q["recurring"]:
        return total
    return i18n.translate("qp.per_visit_line", lang, total=total,
                          frequency=i18n.translate(f"quotes.freq_{q['frequency']}", lang).lower())


def _message(q, link):
    return _in_language(q, "quotes.share_message", name=q["to_name"].split(" ")[0], company=_company()["name"],
                        title=q["title"], price=_price_line(q), link=link)


def _recipients(q):
    """Quem é avisado quando o cliente responde: quem fez a cotação e o dono."""
    ids = [q["created_by"]] if q["created_by"] else []
    ids += [r[0] for r in get_db().execute("SELECT id FROM users WHERE role = 'owner' AND active = 1")]
    return list(dict.fromkeys(ids))


def _notify(q, kind, note=""):
    for user_id in _recipients(q):
        notifications.notify(user_id, kind, None, quote_id=q["id"], client=q["to_name"], ref=q["ref"],
                             title=q["title"], total=utils.money(q["total_pence"]), note=note)


# ---------- Formulário ----------

def _parse_quantity(text):
    raw = (text or "").strip().replace(" ", "") or "1"
    if not _QTY.match(raw):
        raise ValueError(text)
    value = float(raw.replace(",", "."))
    if value <= 0:
        raise ValueError(text)
    return value


def _read_form():
    f = request.form
    data = {key: f.get(key, "").strip() for key in
            ("client_id", "to_name", "to_email", "to_phone", "to_address", "to_postcode", "title", "intro",
             "notes", "frequency", "valid_until", "language")}
    for key, limit in (("to_name", 120), ("to_email", 200), ("to_phone", 40), ("to_address", 300),
                       ("to_postcode", 20), ("title", 160), ("intro", 3000), ("notes", 3000)):
        data[key] = data[key][:limit]
    data["to_phone"] = utils.read_phone(f, "to_phone")  # com o código do país escolhido
    errors = []
    db = get_db()
    client = company = None
    data["company_id"] = None
    if data["client_id"].startswith("company:"):  # uma empresa (Clientes → Empresas), no mesmo seletor
        company = _company_option(data["client_id"][8:])
        if company is None:
            errors.append(i18n.t("quotes.client_invalid"))
        else:
            data["company_id"] = company["id"]
            data["to_name"] = data["to_name"] or company["name"]
            data["to_email"] = data["to_email"] or company["email"]
        data["client_id"] = ""
    elif data["client_id"]:
        client = db.execute("SELECT * FROM clients WHERE id = ?",
                            (data["client_id"],)).fetchone() if data["client_id"].isdigit() else None
        if client is None:
            errors.append(i18n.t("quotes.client_invalid"))
    if client is not None:  # campos em branco vêm do cadastro do cliente
        for key, column in (("to_name", "name"), ("to_email", "email"), ("to_phone", "phone"),
                            ("to_address", "address"), ("to_postcode", "postcode")):
            data[key] = data[key] or client[column]
    if not data["to_name"]:
        errors.append(i18n.t("quotes.name_required"))
    if data["to_email"] and not utils.valid_email(data["to_email"]):
        errors.append(i18n.t("clients.email_invalid"))
    if not data["title"]:
        errors.append(i18n.t("quotes.title_required"))
    if data["frequency"] not in FREQUENCIES:
        data["frequency"] = "once"
    if data["language"] not in i18n.LANGUAGES:
        data["language"] = "en"
    if utils.parse_date(data["valid_until"]) is None:
        errors.append(i18n.t("quotes.date_invalid"))

    items, rows = [], list(zip(f.getlist("item_desc"), f.getlist("item_qty"), f.getlist("item_price")))
    for n, (desc, qty, price) in enumerate(rows[:MAX_ITEMS], start=1):
        desc = desc.strip()[:300]
        if not desc and not (price or "").strip():
            continue  # linha em branco
        item = {"description": desc, "qty_text": qty, "price_text": price}
        try:
            item["quantity"] = _parse_quantity(qty)
        except ValueError:
            errors.append(i18n.t("quotes.qty_invalid", n=n))
            item["quantity"] = 1
        try:
            item["unit_pence"] = utils.parse_money(price) or 0  # sem preço = incluído
        except ValueError:
            errors.append(i18n.t("quotes.price_invalid", n=n))
            item["unit_pence"] = 0
        if not desc:
            errors.append(i18n.t("quotes.item_needs_text", n=n))
        items.append(item)
    if not items:
        errors.append(i18n.t("quotes.items_required"))
    data["client_id"] = int(data["client_id"]) if client is not None else None
    return data, items, errors


def _save_items(db, quote_id, items):
    db.execute("DELETE FROM quote_items WHERE quote_id = ?", (quote_id,))
    db.executemany(
        "INSERT INTO quote_items (quote_id, description, quantity, unit_pence, position) VALUES (?, ?, ?, ?, ?)",
        [(quote_id, it["description"], it["quantity"], it["unit_pence"], pos) for pos, it in enumerate(items)])


_COMPANY_OPTIONS = ("SELECT co.id, co.name, co.color, COALESCE((SELECT cu.email FROM company_users cu WHERE cu.company_id = co.id "
                    "AND cu.active = 1 ORDER BY cu.id LIMIT 1), '') AS email FROM companies co")


def _company_option(value):
    """Uma empresa do seletor (id, nome, cor e o e-mail do primeiro acesso ativo), ou None."""
    if not str(value).isdigit():
        return None
    return get_db().execute(_COMPANY_OPTIONS + " WHERE co.id = ?", (int(value),)).fetchone()


def _form_options():
    db = get_db()
    clients = db.execute(
        "SELECT id, name, email, phone, address, postcode FROM clients WHERE active = 1 ORDER BY name COLLATE NOCASE"
    ).fetchall()
    companies = db.execute(_COMPANY_OPTIONS + " ORDER BY co.name COLLATE NOCASE").fetchall()
    data = {str(c["id"]): dict(c) for c in clients}
    data.update({f"company:{co['id']}": {"name": co["name"], "email": co["email"], "phone": "", "address": "", "postcode": ""}
                 for co in companies})
    return {"clients": clients, "companies": companies, "client_data": data,
            "frequencies": [(k, i18n.t(f"quotes.freq_{k}")) for k in FREQUENCIES]}


def _form_items(items):
    """Linhas do formulário (texto como a pessoa digitou) + uma em branco pra continuar."""
    rows = [{"description": it["description"],
             "qty": it.get("qty_text") if it.get("qty_text") is not None else _fmt_qty(it["quantity"]),
             "price": it.get("price_text") if it.get("price_text") is not None else utils.money_plain(it["unit_pence"])}
            for it in items]
    return rows or [{"description": "", "qty": "1", "price": ""}]


# ---------- Telas da equipe ----------

LIST_VIEWS = ("abertas", "aceitas", "recusadas", "todas")


@bp.route("/cotacoes/")
@permission_required("quotes")
def list_quotes():
    view = request.args.get("ver", "abertas")
    if view not in LIST_VIEWS:
        view = "abertas"
    where = {"abertas": "WHERE q.status = 'open'", "aceitas": "WHERE q.status = 'accepted'",
             "recusadas": "WHERE q.status = 'declined'", "todas": ""}[view]
    db = get_db()
    rows = db.execute(
        "SELECT q.*, (SELECT COALESCE(SUM(CAST(i.quantity * i.unit_pence + 0.5 AS INTEGER)), 0) "
        f"FROM quote_items i WHERE i.quote_id = q.id) AS total_pence FROM quotes q {where} "
        "ORDER BY q.id DESC LIMIT 300").fetchall()
    today = utils.today_iso()
    quotes = [dict(r, ref=ref(r["number"]), state=status_of(r, today)) for r in rows]
    counts = dict(db.execute("SELECT status, COUNT(*) FROM quotes GROUP BY status").fetchall())
    views = [(key, i18n.t(f"quotes.view_{key}"), counts.get(status) if status else None)
             for key, status in (("abertas", "open"), ("aceitas", "accepted"), ("recusadas", "declined"), ("todas", None))]
    return render_template("quotes_list.html", quotes=quotes, view=view, views=views)


@bp.route("/cotacoes/nova", methods=("GET", "POST"))
@permission_required("quotes")
def new_quote():
    if request.method == "POST":
        form, items, errors = _read_form()
        if not errors:
            db = get_db()
            for _attempt in range(3):  # número e link únicos (duas cotações ao mesmo tempo: tenta de novo)
                number = db.execute("SELECT COALESCE(MAX(number), 0) + 1 FROM quotes").fetchone()[0]
                try:
                    cur = db.execute(
                        "INSERT INTO quotes (number, token, client_id, company_id, to_name, to_email, to_phone, to_address, "
                        "to_postcode, title, intro, notes, frequency, valid_until, language, created_by) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (number, secrets.token_urlsafe(18), form["client_id"], form["company_id"], form["to_name"], form["to_email"],
                         form["to_phone"], form["to_address"], form["to_postcode"], form["title"], form["intro"],
                         form["notes"], form["frequency"], form["valid_until"], form["language"], g.user["id"]))
                    break
                except sqlite3.IntegrityError:
                    db.rollback()
            else:
                abort(500)
            _save_items(db, cur.lastrowid, items)
            db.commit()
            flash(i18n.t("quotes.created", ref=ref(number)), "ok")
            return redirect(url_for("quotes.quote_detail", quote_id=cur.lastrowid, _anchor="fotos"))
        for msg in errors:
            flash(msg, "error")
    else:
        last = get_db().execute("SELECT notes, language FROM quotes ORDER BY id DESC LIMIT 1").fetchone()
        form = {"client_id": request.args.get("client", ""), "frequency": "once",
                "valid_until": (date.fromisoformat(utils.today_iso()) + timedelta(days=VALID_DAYS)).isoformat(),
                "language": last["language"] if last else "en",
                "notes": last["notes"] if last else ""}  # as condições costumam se repetir
        client = get_db().execute("SELECT * FROM clients WHERE id = ?", (form["client_id"],)).fetchone() \
            if form["client_id"].isdigit() else None
        if client is not None:
            form.update(to_name=client["name"], to_email=client["email"], to_phone=client["phone"],
                        to_address=client["address"], to_postcode=client["postcode"])
        company = _company_option(request.args.get("company", ""))
        if company is not None:  # veio da página da empresa
            form.update(client_id=f"company:{company['id']}", to_name=company["name"], to_email=company["email"])
        items = []
    return render_template("quote_form.html", form=form, items=_form_items(items), quote=None, **_form_options())


@bp.route("/cotacoes/<int:quote_id>/editar", methods=("GET", "POST"))
@permission_required("quotes")
def edit_quote(quote_id):
    q = _get_or_404(quote_id)
    if q["status"] != "open":
        flash(i18n.t("quotes.locked"), "error")
        return redirect(url_for("quotes.quote_detail", quote_id=quote_id))
    if request.method == "POST":
        form, items, errors = _read_form()
        if not errors:
            db = get_db()
            db.execute(
                "UPDATE quotes SET client_id = ?, company_id = ?, to_name = ?, to_email = ?, to_phone = ?, to_address = ?, "
                "to_postcode = ?, title = ?, intro = ?, notes = ?, frequency = ?, valid_until = ?, language = ? "
                "WHERE id = ?",
                (form["client_id"], form["company_id"], form["to_name"], form["to_email"], form["to_phone"], form["to_address"],
                 form["to_postcode"], form["title"], form["intro"], form["notes"], form["frequency"],
                 form["valid_until"], form["language"], quote_id))
            _save_items(db, quote_id, items)
            db.commit()
            flash(i18n.t("common.changes_saved"), "ok")
            return redirect(url_for("quotes.quote_detail", quote_id=quote_id))
        for msg in errors:
            flash(msg, "error")
    else:
        form = dict(q)
        form["client_id"] = f"company:{q['company_id']}" if q["company_id"] else str(q["client_id"] or "")
        items = q["lines"]
    return render_template("quote_form.html", form=form, items=_form_items(items), quote=q, **_form_options())


def _photo_item(q, photo, public=False):
    endpoint, key = ("quotes.public_photo", {"token": q["token"]}) if public else ("quotes.photo_file", {"quote_id": q["id"]})
    item = {
        "id": photo["id"], "text": photo["caption"],
        "full": url_for(endpoint, filename=photo["filename"], **key),
        "mini": url_for(endpoint, filename=photo["filename"], mini=1, **key),
    }
    if not public:
        item["delete"] = url_for("quotes.delete_photo", quote_id=q["id"], photo_id=photo["id"])
        item["caption"] = url_for("quotes.save_caption", quote_id=q["id"], photo_id=photo["id"])
    return item


@bp.route("/cotacoes/<int:quote_id>")
@permission_required("quotes")
def quote_detail(quote_id):
    q = _get_or_404(quote_id)
    link = _public_url(q)
    job = get_db().execute("SELECT id, job_date FROM jobs WHERE id = ?", (q["job_id"],)).fetchone() if q["job_id"] else None
    company = _company_option(q["company_id"]) if q["company_id"] else None
    return render_template(
        "quote_detail.html", q=q, link=link, job=job, company=company, max_photos=photos.MAX_PHOTOS,
        photo_set={"upload": url_for("quotes.upload_photos", quote_id=quote_id),
                   "photos": [_photo_item(q, p) for p in q["photos"]]},
        email_ready=notifications.email_enabled(), price_line=_price_line(q, g.lang),
        created=_date_full(q["created_at"][:10]), valid_until=_date_full(q["valid_until"]))


def _mark_sent(quote_id):
    db = get_db()
    db.execute("UPDATE quotes SET sent_at = ? WHERE id = ?", (utils.now_utc_iso(), quote_id))
    db.commit()


@bp.route("/cotacoes/<int:quote_id>/whatsapp", methods=("POST",))
@permission_required("quotes")
def send_whatsapp(quote_id):
    """Abre o WhatsApp com a mensagem pronta (e o link) pro número do cliente."""
    q = _get_or_404(quote_id)
    if q["status"] != "open":
        flash(i18n.t("quotes.locked"), "error")
        return redirect(url_for("quotes.quote_detail", quote_id=quote_id))
    _mark_sent(quote_id)
    number = _wa_number(q["to_phone"])
    return redirect(f"https://wa.me/{number}?text={url_quote(_message(q, _public_url(q)))}")


@bp.route("/cotacoes/<int:quote_id>/email", methods=("POST",))
@permission_required("quotes")
def send_email(quote_id):
    q = _get_or_404(quote_id)
    back = redirect(url_for("quotes.quote_detail", quote_id=quote_id))
    if q["status"] != "open":
        flash(i18n.t("quotes.locked"), "error")
        return back
    if not q["to_email"]:
        flash(i18n.t("quotes.no_email"), "error")
        return back
    if not notifications.email_enabled():
        flash(i18n.t("quotes.email_not_set_up"), "error")
        return back
    company = _company()
    subject = _in_language(q, "quotes.email_subject", ref=q["ref"], company=company["name"])
    body = _in_language(q, "quotes.email_body", name=q["to_name"].split(" ")[0], company=company["name"],
                        title=q["title"], price=_price_line(q), link=_public_url(q),
                        date=_in_language_date(q, q["valid_until"]),
                        signature="\n".join(x for x in (company["name"], company["phone"]) if x))
    try:
        notifications.send_email(q["to_email"], subject, body)
    except smtplib.SMTPAuthenticationError:
        flash(i18n.t("prefs.test_auth_failed"), "error")
        return back
    except Exception as exc:  # rede fora do ar, Gmail bloqueando etc.
        flash(i18n.t("prefs.test_failed", error=exc.__class__.__name__), "error")
        return back
    _mark_sent(quote_id)
    flash(i18n.t("quotes.email_sent", to=q["to_email"]), "ok")
    return back


def _in_language_date(q, value):
    previous = g.get("lang")
    g.lang = q["language"]
    try:
        return _date_full(value)
    finally:
        g.lang = previous


@bp.route("/cotacoes/<int:quote_id>/cancelar", methods=("POST",))
@permission_required("quotes")
def cancel_quote(quote_id):
    """Cancela o link (o cliente passa a ver "não está mais disponível"). Dá pra reabrir."""
    db = get_db()
    if db.execute("UPDATE quotes SET status = 'cancelled' WHERE id = ? AND status = 'open'", (quote_id,)).rowcount:
        db.commit()
        flash(i18n.t("quotes.cancelled_msg"), "ok")
    return redirect(url_for("quotes.quote_detail", quote_id=quote_id))


@bp.route("/cotacoes/<int:quote_id>/reabrir", methods=("POST",))
@permission_required("quotes")
def reopen_quote(quote_id):
    """Cancelada ou recusada volta a valer (o cliente pode responder de novo)."""
    db = get_db()
    if db.execute("UPDATE quotes SET status = 'open', answered_at = NULL, answer_name = '', answer_note = '' "
                  "WHERE id = ? AND status IN ('cancelled', 'declined')", (quote_id,)).rowcount:
        db.commit()
        flash(i18n.t("quotes.reopened_msg"), "ok")
    return redirect(url_for("quotes.quote_detail", quote_id=quote_id))


@bp.route("/cotacoes/<int:quote_id>/excluir", methods=("POST",))
@permission_required("quotes")
def delete_quote(quote_id):
    q = _get_or_404(quote_id)
    db = get_db()
    db.execute("DELETE FROM quotes WHERE id = ?", (quote_id,))  # itens, fotos e avisos vão junto
    db.commit()
    photos.remove_quote_folder(quote_id)
    flash(i18n.t("quotes.deleted_msg", ref=q["ref"]), "ok")
    return redirect(url_for("quotes.list_quotes"))


@bp.route("/cotacoes/<int:quote_id>/agendar", methods=("POST",))
@permission_required("quotes")
def schedule_job(quote_id):
    """Cotação aceita vira trabalho: cadastra o cliente se ele for novo e abre o formulário de
    trabalho já com os itens como tarefas (e o texto das fotos como instruções)."""
    q = _get_or_404(quote_id)
    if not can("schedule"):
        abort(403)
    if q["status"] != "accepted":
        flash(i18n.t("quotes.not_accepted"), "error")
        return redirect(url_for("quotes.quote_detail", quote_id=quote_id))
    db = get_db()
    client_id = q["client_id"]
    if client_id is None or db.execute("SELECT 1 FROM clients WHERE id = ?", (client_id,)).fetchone() is None:
        cur = db.execute("INSERT INTO clients (name, address, postcode, phone, email, company_id) VALUES (?, ?, ?, ?, ?, ?)",
                         (q["to_name"], q["to_address"], q["to_postcode"], q["to_phone"], q["to_email"],
                          q["company_id"] if q["company_id"] and db.execute(
                              "SELECT 1 FROM companies WHERE id = ?", (q["company_id"],)).fetchone() else None))
        client_id = cur.lastrowid
        db.execute("UPDATE quotes SET client_id = ? WHERE id = ?", (client_id, quote_id))
        db.commit()
        flash(i18n.t("quotes.client_created", name=q["to_name"]), "ok")
    return redirect(url_for("jobs.new_job", client=client_id, cotacao=quote_id))


def job_prefill(quote_id):
    """Pro formulário de trabalho novo (jobs.new_job): título, tarefas e instruções vindos da cotação."""
    row = get_db().execute("SELECT * FROM quotes WHERE id = ? AND status = 'accepted'", (quote_id,)).fetchone()
    if row is None:
        return None
    q = _load(row)
    notes = [p["caption"].strip() for p in q["photos"] if p["caption"].strip()]
    instructions = "\n".join([i18n.t("quotes.job_from", ref=q["ref"])] + [f"- {n}" for n in notes]) if notes else ""
    return {"title": q["title"][:120], "tasks": "\n".join(i["description"] for i in q["lines"]),
            "description": instructions[:2000], "quote_id": q["id"]}


def link_job(quote_id, job_id):
    get_db().execute("UPDATE quotes SET job_id = ? WHERE id = ? AND job_id IS NULL", (job_id, quote_id))


# ---------- Fotos da cotação (cada uma com um texto embaixo) ----------

@bp.route("/cotacoes/<int:quote_id>/fotos", methods=("POST",))
@permission_required("quotes")
def upload_photos(quote_id):
    q = _get_or_404(quote_id)
    upload = photos.add_quote_photos(quote_id, request.files.getlist("photo"), g.user["id"])
    added = {pid for pid, _ in upload.added}
    rows = get_db().execute("SELECT * FROM quote_photos WHERE quote_id = ? ORDER BY id", (quote_id,)).fetchall()
    items = [_photo_item(q, r) for r in rows if r["id"] in added]
    return photos.respond(upload, items, url_for("quotes.quote_detail", quote_id=quote_id, _anchor="fotos"))


@bp.route("/cotacoes/<int:quote_id>/fotos/<int:photo_id>/texto", methods=("POST",))
@permission_required("quotes")
def save_caption(quote_id, photo_id):
    """O texto embaixo da foto (o photos.js salva sozinho enquanto a pessoa digita)."""
    text = request.form.get("caption", "").strip()[:600]
    db = get_db()
    ok = db.execute("UPDATE quote_photos SET caption = ? WHERE id = ? AND quote_id = ?",
                    (text, photo_id, quote_id)).rowcount > 0
    db.commit()
    message = i18n.t("quotes.caption_saved") if ok else i18n.t("photos.not_found")
    return photos.respond_delete(ok, message, url_for("quotes.quote_detail", quote_id=quote_id, _anchor="fotos"))


@bp.route("/cotacoes/<int:quote_id>/fotos/<int:photo_id>/excluir", methods=("POST",))
@permission_required("quotes")
def delete_photo(quote_id, photo_id):
    db = get_db()
    row = db.execute("SELECT * FROM quote_photos WHERE id = ? AND quote_id = ?", (photo_id, quote_id)).fetchone()
    back = url_for("quotes.quote_detail", quote_id=quote_id, _anchor="fotos")
    if row is None:
        return photos.respond_delete(False, i18n.t("photos.not_found"), back)
    db.execute("DELETE FROM quote_photos WHERE id = ?", (photo_id,))
    db.commit()
    photos.remove_photo_files(photos.quote_dir(quote_id), row["filename"])
    return photos.respond_delete(True, i18n.t("photos.deleted"), back)


def _send_quote_photo(quote_id, filename):
    if get_db().execute("SELECT 1 FROM quote_photos WHERE quote_id = ? AND filename = ?",
                        (quote_id, filename)).fetchone() is None:
        abort(404)
    return photos.send_photo(photos.quote_dir(quote_id), filename, mini=request.args.get("mini") == "1")


@bp.route("/cotacoes/<int:quote_id>/fotos/<filename>")
@permission_required("quotes")
def photo_file(quote_id, filename):
    return _send_quote_photo(quote_id, filename)


# ---------- Página do cliente (sem login) ----------

def _is_client_visit():
    """Conta como "o cliente abriu" só visita de gente: sem ninguém da equipe logado e sem robô de prévia."""
    return g.user is None and not PREVIEW_BOTS.search(request.headers.get("User-Agent", ""))


@bp.route("/c/<token>")
def public_quote(token):
    q = _by_token_or_404(token)
    g.lang = q["language"]
    if q["status"] != "cancelled" and q["viewed_at"] is None and _is_client_visit():
        db = get_db()
        if db.execute("UPDATE quotes SET viewed_at = ? WHERE id = ? AND viewed_at IS NULL",
                      (utils.now_utc_iso(), q["id"])).rowcount:
            _notify(q, notifications.QUOTE_VIEWED)
        db.commit()
    company = _company()
    items = [_photo_item(q, p, public=True) for p in q["photos"]]
    first = items[0]["mini"] if items else None
    base = (current_app.config.get("APP_URL") or request.host_url).rstrip("/")
    response = current_app.make_response(render_template(
        "quote_public.html", q=q, company=company, photos=items, price_line=_price_line(q),
        og_image=base + first if first else None, created=_date_full(q["created_at"][:10]),
        valid_until=_date_full(q["valid_until"]), answered=utils.dt_local(q["answered_at"]) if q["answered_at"] else "",
        wa_company=_wa_number(company["phone"]),
        wa_text=_in_language(q, "qp.whatsapp_question", ref=q["ref"])))
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


@bp.route("/c/<token>/resposta", methods=("POST",))
def public_answer(token):
    q = _by_token_or_404(token)
    g.lang = q["language"]
    back = redirect(url_for("quotes.public_quote", token=token, _anchor="resposta"))
    if q["state"] not in ("draft", "sent", "viewed"):  # já respondida, vencida ou cancelada
        flash(i18n.t("qp.not_open"), "error")
        return back
    answer, note = request.form.get("answer"), request.form.get("note", "").strip()[:600]
    name = request.form.get("name", "").strip()[:120]
    now = utils.now_utc_iso()
    db = get_db()
    if answer == "accept":
        if not name:
            flash(i18n.t("qp.name_required"), "error")
            return back
        changed = db.execute(
            "UPDATE quotes SET status = 'accepted', answered_at = ?, answer_name = ?, answer_note = ?, "
            "viewed_at = COALESCE(viewed_at, ?) WHERE id = ? AND status = 'open'", (now, name, note, now, q["id"])).rowcount
        kind = notifications.QUOTE_ACCEPTED
    elif answer == "decline":
        changed = db.execute(
            "UPDATE quotes SET status = 'declined', answered_at = ?, answer_name = ?, answer_note = ?, "
            "viewed_at = COALESCE(viewed_at, ?) WHERE id = ? AND status = 'open'", (now, name, note, now, q["id"])).rowcount
        kind = notifications.QUOTE_DECLINED
    else:
        abort(400)
    if changed:
        _notify(q, kind, note=note)
    db.commit()
    return back


@bp.route("/c/<token>/fotos/<filename>")
def public_photo(token, filename):
    q = _by_token_or_404(token)
    if q["status"] == "cancelled":
        abort(404)
    return _send_quote_photo(q["id"], filename)
