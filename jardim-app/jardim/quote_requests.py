"""Pedido de cotação: a versão simples da cotação, pra quem está no jardim do cliente.

Qualquer pessoa da equipe (funcionário, gerente ou o dono) abre um formulário curto em Meus trabalhos:
  - fotos, vídeos ou documentos em cima, do mesmo jeito da conversa do trabalho;
  - os dados do cliente (da lista, ou nome, telefone e endereço novos) e o que ele pediu;
  - anotações, o tempo estimado e o valor estimado;
  - e o botão "Enviar pra análise".
Isso vira um pedido "aguardando análise": o dono e os gerentes com o acesso "Cotações" veem no Painel, em
Cotações e no sininho. Quem analisa abre o pedido (com as fotos e o vídeo), e:
  - "Criar cotação": nasce uma cotação já preenchida (cliente, o pedido, um item com o valor estimado e as fotos),
    que a pessoa acerta e manda pro cliente pelo jeito normal (quotes.py);
  - ou "Recusar", com um motivo se quiser.
Quem pediu recebe o aviso e vê a situação em Meus trabalhos.
"""
import secrets
from datetime import date, timedelta

from flask import (Blueprint, abort, flash, g, jsonify, redirect, render_template, request, send_from_directory, url_for)

from . import i18n, notifications, photos, portal, quotes, utils
from .auth import can, login_required, permission_required
from .db import get_db
from .jobs import duration_bounds

bp = Blueprint("quote_requests", __name__, url_prefix="/pedidos-de-cotacao")

MAX_FILES = 8      # por pedido
MAX_VIDEO_MB = 60  # por vídeo (um minuto de celular)
VIDEO_TYPES = {".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm", ".3gp": "video/3gpp"}


# ---------- Formulário ----------

def _read_form():
    f = request.form
    data = {
        "client_id": f.get("client_id", "").strip(),
        "client_name": " ".join(f.get("client_name", "").split())[:120],
        "phone": utils.read_phone(f, "phone")[:40],
        "address": " ".join(f.get("address", "").split())[:300],
        "postcode": " ".join(f.get("postcode", "").split())[:20],
        "request": f.get("request", "").strip()[:3000],
        "notes": f.get("notes", "").strip()[:2000],
        "time_text": " ".join(f.get("time_text", "").split())[:40],
        "price_text": f.get("price_text", "").strip()[:20],
    }
    errors, db = [], get_db()
    if data["client_id"]:
        client = db.execute("SELECT id, name, phone, address, postcode FROM clients WHERE id = ?",
                            (data["client_id"],)).fetchone() if data["client_id"].isdigit() else None
        if client is None:
            errors.append(i18n.t("extras.client_invalid"))
        else:  # os dados vêm do cadastro
            data.update(client_name=client["name"], phone=client["phone"], address=client["address"], postcode=client["postcode"])
    elif not data["client_name"]:
        errors.append(i18n.t("extras.client_required"))
    if not data["request"]:
        errors.append(i18n.t("qreq.request_required"))
    bounds = duration_bounds(data["time_text"]) if data["time_text"] else None
    data["minutes"] = bounds[1] if bounds else None
    if data["time_text"] and not data["minutes"]:
        errors.append(i18n.t("qreq.time_invalid"))
    try:
        data["price_pence"] = utils.parse_money(data["price_text"]) if data["price_text"] else None
    except ValueError:
        errors.append(i18n.t("qreq.price_invalid"))
        data["price_pence"] = None
    return data, errors


def _blank_form():
    return {"client_id": "", "client_name": "", "phone": "", "address": "", "postcode": "", "request": "", "notes": "",
            "time_text": "", "price_text": ""}


# ---------- Arquivos: fotos (reduzidas), vídeos e documentos ----------

def _is_video(path, ext):
    """Vídeo de celular de verdade: MP4/MOV/3GP começam com "ftyp" no 5º byte; WebM tem a assinatura própria."""
    if ext not in VIDEO_TYPES:
        return False
    with open(path, "rb") as fh:
        head = fh.read(12)
    return head[4:8] == b"ftyp" or head.startswith(b"\x1a\x45\xdf\xa3")


def save_files(request_id, files):
    """Grava os anexos do pedido (o commit fica com quem chamou). Devolve (quantos entraram, quantos ficaram de fora,
    acabou o espaço?)."""
    folder, db = photos.request_dir(request_id), get_db()
    limit = photos.space_limit()
    used = photos.space_used() if limit else 0
    saved = bad = 0
    no_space = False
    for file_storage in files:
        if limit and used >= limit:
            no_space = True
            continue
        original, ext = portal._original_name(file_storage), portal._ext(file_storage.filename)
        try:
            if ext in VIDEO_TYPES:
                folder.mkdir(parents=True, exist_ok=True)
                filename, kind = f"{secrets.token_hex(8)}{ext}", "video"
                path = folder / filename
                file_storage.save(path)
                size = path.stat().st_size
                if size > MAX_VIDEO_MB * 1024 * 1024 or not _is_video(path, ext):
                    path.unlink(missing_ok=True)
                    bad += 1
                    continue
            elif ext in portal.DOC_TYPES:
                data = file_storage.stream.read(portal.MAX_DOC_MB * 1024 * 1024 + 1)
                if len(data) > portal.MAX_DOC_MB * 1024 * 1024 or not data.startswith(portal.DOC_TYPES[ext][2]):
                    bad += 1
                    continue
                folder.mkdir(parents=True, exist_ok=True)
                filename, kind = f"{secrets.token_hex(8)}{ext}", "doc"
                (folder / filename).write_bytes(data)
                size = len(data)
            else:
                filename, kind = photos.store_photo(file_storage, folder), "photo"
                if filename is None:
                    bad += 1
                    continue
                size = (folder / filename).stat().st_size
        except OSError:  # o disco encheu de verdade
            no_space = True
            continue
        used += size
        db.execute("INSERT INTO quote_request_files (request_id, kind, filename, original, size) VALUES (?, ?, ?, ?, ?)",
                   (request_id, kind, filename, original, size))
        saved += 1
    return saved, bad, no_space


def files_of(request_id):
    """Os anexos do pedido, com o endereço de cada um (e da miniatura, nas fotos)."""
    items = []
    for f in get_db().execute("SELECT * FROM quote_request_files WHERE request_id = ? ORDER BY id", (request_id,)):
        item = dict(f, url=url_for("quote_requests.file", request_id=request_id, file_id=f["id"]),
                    size_text=portal._size_text(f["size"]))
        if f["kind"] == "photo":
            item["mini"] = url_for("quote_requests.file", request_id=request_id, file_id=f["id"], mini=1)
        elif f["kind"] == "video":
            item["mimetype"] = VIDEO_TYPES.get(portal._ext(f["filename"]), "video/mp4")
        else:
            item["label"] = portal.DOC_TYPES.get(portal._ext(f["filename"]), ("", "DOC"))[1]
        items.append(item)
    return items


# ---------- Ajudas ----------

def _row(request_id):
    row = get_db().execute(
        "SELECT r.*, u.name AS user_name FROM quote_requests r JOIN users u ON u.id = r.user_id WHERE r.id = ?",
        (request_id,)).fetchone()
    if row is None:
        abort(404)
    return row


def _can_see(row):
    return can("quotes") or row["user_id"] == g.user["id"]


def _reviewers():
    """Quem analisa: os donos e os gerentes com o acesso "Cotações"."""
    return [r["id"] for r in get_db().execute(
        "SELECT id, role, permissions FROM users WHERE active = 1 AND (role = 'owner' OR role = 'manager') ORDER BY id")
            if r["role"] == "owner" or "quotes" in (r["permissions"] or "").split(",")]


def _params(row):
    return {"client": row["client_name"], "author": row["user_name"], "request_id": row["id"],
            "price": utils.money(row["price_pence"]) if row["price_pence"] else "",
            "time": row["time_text"] or (utils.format_minutes(row["minutes"]) if row["minutes"] else "")}


def pending_count():
    return get_db().execute("SELECT COUNT(*) FROM quote_requests WHERE status = 'pending'").fetchone()[0]


def mine(user_id, limit=20):
    """Os pedidos da pessoa, mais novos primeiro (pra Meus trabalhos)."""
    return get_db().execute(
        "SELECT r.*, u.name AS user_name FROM quote_requests r JOIN users u ON u.id = r.user_id "
        "WHERE r.user_id = ? ORDER BY r.id DESC LIMIT ?", (user_id, limit)).fetchall()


def _with_counts(rows):
    out = []
    for r in rows:
        item = dict(r)
        item["files"] = files_of(r["id"])
        out.append(item)
    return out


# ---------- Telas ----------

@bp.route("/novo", methods=("GET", "POST"))
@login_required
def new_request():
    clients = get_db().execute("SELECT id, name, address FROM clients WHERE active = 1 ORDER BY name COLLATE NOCASE").fetchall()
    form = _blank_form()
    if request.method == "POST":
        form, errors = _read_form()
        files = portal.picked_files()
        if len(files) > MAX_FILES:
            errors.append(i18n.t("qreq.files_too_many", n=MAX_FILES))
        if errors:
            if photos.wants_json():  # o comments.js mostra os erros sem perder as fotos escolhidas
                return jsonify(ok=False, errors=errors), 400
            for msg in errors:
                flash(msg, "error")
        else:
            db = get_db()
            cur = db.execute(
                "INSERT INTO quote_requests (user_id, client_id, client_name, phone, address, postcode, request, notes, "
                "time_text, minutes, price_pence, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (g.user["id"], int(form["client_id"]) if form["client_id"].isdigit() else None, form["client_name"],
                 form["phone"], form["address"], form["postcode"], form["request"], form["notes"], form["time_text"],
                 form["minutes"], form["price_pence"], utils.now_utc_iso()))
            request_id = cur.lastrowid
            _saved, bad, no_space = save_files(request_id, files)
            row = _row(request_id)
            for user_id in _reviewers():  # no sininho de quem analisa (e por e-mail, se ligado)
                notifications.notify(user_id, notifications.QREQ_SENT, **_params(row))
            db.commit()
            flash(i18n.t("qreq.sent"), "ok")
            if bad:
                flash(i18n.t("qreq.files_bad_one", mb=MAX_VIDEO_MB) if bad == 1 else i18n.t("qreq.files_bad_many", n=bad, mb=MAX_VIDEO_MB), "info")
            if no_space:
                flash(i18n.t("photos.no_space"), "info")
            return redirect(url_for("quote_requests.detail", request_id=request_id))
    return render_template("qreq_form.html", form=form, clients=clients, max_files=MAX_FILES,
                           max_doc_mb=portal.MAX_DOC_MB, max_video_mb=MAX_VIDEO_MB)


@bp.route("/")
@login_required
def list_requests():
    db = get_db()
    if can("quotes"):
        pending = db.execute("SELECT r.*, u.name AS user_name FROM quote_requests r JOIN users u ON u.id = r.user_id "
                             "WHERE r.status = 'pending' ORDER BY r.id DESC").fetchall()
        decided = db.execute("SELECT r.*, u.name AS user_name FROM quote_requests r JOIN users u ON u.id = r.user_id "
                             "WHERE r.status != 'pending' ORDER BY r.decided_at DESC, r.id DESC LIMIT 30").fetchall()
        return render_template("qreq_list.html", pending=_with_counts(pending), decided=_with_counts(decided), own=None)
    return render_template("qreq_list.html", pending=None, decided=None, own=_with_counts(mine(g.user["id"], 50)))


@bp.route("/<int:request_id>")
@login_required
def detail(request_id):
    row = _row(request_id)
    if not _can_see(row):
        abort(403)
    quote = None
    if row["quote_id"]:
        q = get_db().execute("SELECT id, number, status FROM quotes WHERE id = ?", (row["quote_id"],)).fetchone()
        quote = dict(q, ref=quotes.ref(q["number"])) if q else None
    return render_template("qreq_detail.html", r=row, files=files_of(request_id), quote=quote,
                           reviewer=can("quotes"), time_label=_params(row)["time"])


@bp.route("/<int:request_id>/arquivo/<int:file_id>")
@login_required
def file(request_id, file_id):
    row = _row(request_id)
    if not _can_see(row):
        abort(403)
    f = get_db().execute("SELECT * FROM quote_request_files WHERE id = ? AND request_id = ?", (file_id, request_id)).fetchone()
    if f is None:
        abort(404)
    folder = photos.request_dir(request_id)
    if f["kind"] == "photo":
        return photos.send_photo(folder, f["filename"], mini=request.args.get("mini") == "1")
    if f["kind"] == "video":
        mimetype = VIDEO_TYPES.get(portal._ext(f["filename"]), "video/mp4")
        response = send_from_directory(folder, f["filename"], mimetype=mimetype, max_age=7 * 24 * 3600, conditional=True)
    else:
        mimetype = portal.DOC_TYPES.get(portal._ext(f["filename"]), ("application/octet-stream",))[0]
        response = send_from_directory(folder, f["filename"], mimetype=mimetype, max_age=7 * 24 * 3600,
                                       as_attachment=mimetype != "application/pdf", download_name=f["original"] or f["filename"])
    response.cache_control.public = False
    response.cache_control.private = True
    return response


@bp.route("/<int:request_id>/cotar", methods=("POST",))
@permission_required("quotes")
def make_quote(request_id):
    """Vira uma cotação já preenchida: cliente, o pedido como abertura, um item com o valor estimado e as fotos.
    Quem analisa acerta o resto na tela normal da cotação."""
    row = _row(request_id)
    if row["status"] != "pending":
        abort(404)
    db = get_db()
    client_id = row["client_id"] if row["client_id"] and db.execute(
        "SELECT 1 FROM clients WHERE id = ?", (row["client_id"],)).fetchone() else None
    first_line = next((line.strip() for line in row["request"].splitlines() if line.strip()), row["request"])
    form = {"client_id": client_id, "company_id": None, "to_name": row["client_name"], "to_phone": row["phone"],
            "to_address": row["address"], "to_postcode": row["postcode"], "title": first_line[:160],
            "intro": row["request"], "frequency": "once",
            "valid_until": (date.fromisoformat(utils.today_iso()) + timedelta(days=quotes.VALID_DAYS)).isoformat(),
            **quotes.last_defaults()}
    items = [{"description": first_line[:300], "quantity": 1, "unit_pence": row["price_pence"] or 0}]
    quote_id, number = quotes.create_quote(form, items, g.user["id"])
    src = photos.request_dir(request_id)
    for f in db.execute("SELECT * FROM quote_request_files WHERE request_id = ? AND kind = 'photo' ORDER BY id LIMIT ?",
                        (request_id, photos.MAX_PHOTOS)).fetchall():
        try:
            new_name = photos.copy_photo(src, f["filename"], photos.quote_dir(quote_id))
        except OSError:
            continue
        db.execute("INSERT INTO quote_photos (quote_id, filename, caption, uploaded_by) VALUES (?, ?, '', ?)",
                   (quote_id, new_name, row["user_id"]))
    now = utils.now_utc_iso()
    db.execute("UPDATE quote_requests SET status = 'quoted', decided_by = ?, decided_at = ?, quote_id = ? WHERE id = ?",
               (g.user["id"], now, quote_id, request_id))
    notifications.notify(row["user_id"], notifications.QREQ_QUOTED, quote_id=None, **_params(row), ref=quotes.ref(number))
    db.commit()
    flash(i18n.t("qreq.quoted_msg", ref=quotes.ref(number)), "ok")
    return redirect(url_for("quotes.edit_quote", quote_id=quote_id))


@bp.route("/<int:request_id>/recusar", methods=("POST",))
@permission_required("quotes")
def decline(request_id):
    row = _row(request_id)
    if row["status"] != "pending":
        abort(404)
    note = " ".join(request.form.get("note", "").split())[:300]
    db = get_db()
    db.execute("UPDATE quote_requests SET status = 'declined', decided_by = ?, decided_at = ?, decision_note = ? WHERE id = ?",
               (g.user["id"], utils.now_utc_iso(), note, request_id))
    notifications.notify(row["user_id"], notifications.QREQ_DECLINED, **_params(row), note=note)
    db.commit()
    flash(i18n.t("qreq.declined_msg"), "ok")
    return redirect(url_for("quote_requests.detail", request_id=request_id))


@bp.route("/<int:request_id>/excluir", methods=("POST",))
@login_required
def delete(request_id):
    """Quem pediu apaga o próprio pedido enquanto ele espera; quem analisa apaga qualquer um."""
    row = _row(request_id)
    if not (can("quotes") or (row["user_id"] == g.user["id"] and row["status"] == "pending")):
        abort(403)
    db = get_db()
    db.execute("DELETE FROM quote_requests WHERE id = ?", (request_id,))  # os arquivos no banco vão junto
    db.commit()
    photos.remove_request_folder(request_id)
    flash(i18n.t("qreq.deleted_msg"), "ok")
    return redirect(url_for("quote_requests.list_requests"))
