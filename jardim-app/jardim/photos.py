"""Fotos: a galeria do jardim de cada cliente e as fotos de Antes e Depois de cada trabalho.

Onde fica cada coisa:
  - galeria do cliente (até 12): tabela client_photos, arquivos em uploads/clients/<client_id>/
  - Antes e Depois do trabalho (até 12 em cada): tabela job_photos, arquivos em uploads/jobs/<job_id>/
  - fotos da cotação (até 12, cada uma com um texto): tabela quote_photos, arquivos em uploads/quotes/<quote_id>/

Cada foto vira dois arquivos JPEG: o grande (até 1600 px, abre ao tocar na foto) e a miniatura
(<nome>_mini.jpg), que é a que aparece na grade: a página carrega rápido mesmo no 4G.
O celular já reduz a foto antes de enviar (static/photos.js), mas aqui ela é processada de novo
de qualquer jeito: acerta a rotação, apaga os dados escondidos da foto (inclusive a localização
GPS da casa do cliente) e garante o tamanho.

As rotas de Antes e Depois ficam em jobs.py, porque dependem de quem pode ver cada trabalho.
Este módulo não importa jobs.py nem clients.py de propósito (os dois importam este).
"""
import os
import secrets
import shutil
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from flask import (Blueprint, abort, current_app, flash, g, jsonify, redirect, request,
                   send_from_directory, url_for)

from . import i18n
from .auth import permission_required
from .db import get_db

bp = Blueprint("photos", __name__)

MAX_PHOTOS = 12        # na galeria de cada cliente e em cada seção (Antes, Depois) de cada trabalho
MAX_DIMENSION = 1600   # lado maior da foto grande, em pixels (o mesmo que o WhatsApp usa)
MINI_DIMENSION = 480   # miniatura da grade
PHASES = {"antes": "before", "depois": "after"}  # como aparece no endereço -> como fica no banco


# ---------- Pastas e arquivos ----------

def _upload_root():
    return Path(current_app.config["UPLOAD_ROOT"])


def client_dir(client_id):
    return _upload_root() / "clients" / str(client_id)


def job_dir(job_id):
    return _upload_root() / "jobs" / str(job_id)


def quote_dir(quote_id):
    return _upload_root() / "quotes" / str(quote_id)


def comment_dir(job_id):
    """Anexos da conversa com a empresa sobre um trabalho (portal.py)."""
    return _upload_root() / "comments" / str(job_id)


def remove_comment_folder(job_id):
    shutil.rmtree(comment_dir(job_id), ignore_errors=True)


def store_photo(file_storage, folder):
    """Grava uma foto (reduzida, na posição certa e sem a localização) e a miniatura. None se não for foto."""
    return _store(file_storage, folder)


def _mini_name(filename):
    return f"{Path(filename).stem}_mini.jpg"


def _save_jpeg(image, path, quality):
    """Grava num arquivo temporário e troca de nome no fim: ninguém lê uma foto pela metade."""
    temp = path.with_name(f"{path.name}.{secrets.token_hex(4)}.tmp")
    extra = {"icc_profile": image.info["icc_profile"]} if image.info.get("icc_profile") else {}
    try:
        image.save(temp, "JPEG", quality=quality, optimize=True, progressive=True, **extra)
        os.replace(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def _save_mini(image, path):
    mini = image.copy()
    mini.thumbnail((MINI_DIMENSION, MINI_DIMENSION))
    _save_jpeg(mini, path, quality=78)


def _remove_files(folder, filename):
    for name in (filename, _mini_name(filename)):
        try:
            (folder / name).unlink(missing_ok=True)
        except OSError:
            pass


def remove_job_folder(job_id):
    """Chamado quando o trabalho é excluído (as linhas no banco já foram junto)."""
    shutil.rmtree(job_dir(job_id), ignore_errors=True)


def remove_quote_folder(quote_id):
    shutil.rmtree(quote_dir(quote_id), ignore_errors=True)


def remove_photo_files(folder, filename):
    """A foto grande e a miniatura."""
    _remove_files(folder, filename)


def space_used():
    """Quanto todas as fotos ocupam no disco, em bytes."""
    total = 0
    for folder, _subfolders, files in os.walk(_upload_root()):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(folder, name))
            except OSError:
                pass
    return total


def space_limit():
    """Teto para as fotos, em bytes (0 = sem teto). Muda com PHOTO_SPACE_MB; veja o README."""
    return int(float(current_app.config.get("PHOTO_SPACE_MB") or 0) * 1024 * 1024)


def send_photo(folder, filename, mini=False):
    """Manda o arquivo da foto. Com mini=True, a miniatura; fotos antigas não tinham e ganham uma agora."""
    if mini:
        mini_file = _mini_name(filename)
        if not (folder / mini_file).exists():
            try:
                from PIL import Image

                with Image.open(folder / filename) as image:
                    _save_mini(image.convert("RGB"), folder / mini_file)
            except Exception:
                mini_file = filename  # não deu pra criar: vai a foto grande mesmo
        filename = mini_file
    response = send_from_directory(folder, filename, max_age=7 * 24 * 3600)
    response.cache_control.public = False  # foto de cliente: só o navegador da pessoa guarda cópia
    response.cache_control.private = True
    return response


# ---------- Processar e gravar as fotos enviadas ----------

def _open_image(file_storage):
    """Abre a foto enviada (JPG, PNG, HEIC do iPhone...) já na posição certa. None se não for foto."""
    try:
        import pillow_heif

        pillow_heif.register_heif_opener()
    except ImportError:
        pass
    try:
        from PIL import Image, ImageOps

        image = Image.open(file_storage.stream)
        image = ImageOps.exif_transpose(image)  # corrige fotos giradas (a rotação vem só na metadata)
        icc = image.info.get("icc_profile") if image.mode in ("RGB", "RGBA") else None
        image = image.convert("RGB")
        image.info = {"icc_profile": icc} if icc else {}  # só o perfil de cor segue; GPS, modelo do celular etc. ficam pra trás
        return image
    except Exception:
        return None


def _store(file_storage, folder):
    """Grava a foto grande e a miniatura. Devolve o nome do arquivo, ou None se não abriu como foto."""
    image = _open_image(file_storage)
    if image is None:
        return None
    image.thumbnail((MAX_DIMENSION, MAX_DIMENSION))
    folder.mkdir(parents=True, exist_ok=True)
    filename = f"{secrets.token_hex(8)}.jpg"
    try:
        _save_jpeg(image, folder / filename, quality=80)
        _save_mini(image, folder / _mini_name(filename))
    except OSError:
        _remove_files(folder, filename)  # não deixa foto pela metade
        raise
    return filename


@dataclass
class Upload:
    """O que aconteceu num envio: as fotos que entraram e as que ficaram de fora (e por quê)."""
    full_key: str = "photos.limit_reached"   # mensagem de quando já estava tudo cheio
    added: list = field(default_factory=list)  # [(id, nome do arquivo)]
    over_limit: int = 0   # passaram do máximo de fotos
    bad: int = 0          # não abriram como foto
    no_space: bool = False  # o espaço reservado para as fotos acabou

    @property
    def ok(self):
        return bool(self.added)

    @property
    def category(self):
        if not self.added:
            return "error"
        return "info" if (self.over_limit or self.bad or self.no_space) else "ok"

    @property
    def message(self):
        t, n = i18n.t, len(self.added)
        if self.no_space and not n:
            return t("photos.no_space")
        if not (n or self.over_limit or self.bad):
            return t("photos.choose_first")
        if not (n or self.bad):
            return t(self.full_key, n=MAX_PHOTOS)
        parts = []
        if n:
            parts.append(t("photos.added") if n == 1 else t("photos.added_many", n=n))
        if self.over_limit:
            parts.append(t("photos.left_out_one", max=MAX_PHOTOS) if self.over_limit == 1
                         else t("photos.left_out_many", n=self.over_limit, max=MAX_PHOTOS))
        if self.bad:
            parts.append(t("photos.bad_file") if self.bad == 1 else t("photos.bad_files", n=self.bad))
        if self.no_space:
            parts.append(t("photos.no_space"))
        return " ".join(parts)


def _add(files, folder, count_sql, count_args, insert_sql, insert_args, full_key):
    """Grava as fotos enviadas até completar o máximo. Serve para a galeria e para Antes/Depois."""
    db = get_db()
    result = Upload(full_key=full_key)
    limit = space_limit()
    used = space_used() if limit else 0
    for file_storage in files:
        if not file_storage or not file_storage.filename:
            continue  # campo de foto que ficou vazio (ex.: escolheu só pela galeria)
        if result.no_space:
            continue
        if db.execute(count_sql, count_args).fetchone()[0] >= MAX_PHOTOS:
            result.over_limit += 1
            continue
        if limit and used >= limit:
            result.no_space = True
            continue
        try:
            filename = _store(file_storage, folder)
        except OSError:  # o disco encheu de verdade antes do teto: mesma mensagem, e para por aqui
            result.no_space = True
            continue
        if filename is None:
            result.bad += 1
            continue
        used += sum((folder / name).stat().st_size for name in (filename, _mini_name(filename)))
        try:
            cur = db.execute(insert_sql, (*insert_args, filename))
            db.commit()
        except sqlite3.IntegrityError:  # o trabalho foi excluído enquanto a foto subia
            db.rollback()
            _remove_files(folder, filename)
            try:
                folder.rmdir()  # só some se ficou vazia
            except OSError:
                pass
            abort(404)
        result.added.append((cur.lastrowid, filename))
    return result


def add_client_photos(client_id, files, uploaded_by):
    return _add(files, client_dir(client_id),
                "SELECT COUNT(*) FROM client_photos WHERE client_id = ?", (client_id,),
                "INSERT INTO client_photos (client_id, uploaded_by, filename) VALUES (?, ?, ?)",
                (client_id, uploaded_by), "photos.limit_reached")


def add_quote_photos(quote_id, files, uploaded_by):
    return _add(files, quote_dir(quote_id),
                "SELECT COUNT(*) FROM quote_photos WHERE quote_id = ?", (quote_id,),
                "INSERT INTO quote_photos (quote_id, uploaded_by, filename) VALUES (?, ?, ?)",
                (quote_id, uploaded_by), "quotes.photos_full")


def add_job_photos(job_id, phase, files, uploaded_by):
    return _add(files, job_dir(job_id),
                "SELECT COUNT(*) FROM job_photos WHERE job_id = ? AND phase = ?", (job_id, phase),
                "INSERT INTO job_photos (job_id, phase, uploaded_by, filename) VALUES (?, ?, ?, ?)",
                (job_id, phase, uploaded_by), "photos.section_full")


# ---------- Resposta: página normal (recarrega) ou JSON (photos.js, sem recarregar) ----------

def wants_json():
    return request.accept_mimetypes.best == "application/json"


def respond(upload, items, back_url):
    """Depois de enviar: o photos.js recebe JSON e põe as fotos na grade; sem ele, volta pra página."""
    if wants_json():
        return jsonify(ok=upload.ok, message=upload.message, photos=items,
                       over_limit=upload.over_limit, bad=upload.bad, stop=upload.no_space)
    flash(upload.message, upload.category)
    return redirect(back_url)


def respond_delete(ok, message, back_url):
    if wants_json():
        return jsonify(ok=ok, message=message)
    flash(message, "ok" if ok else "error")
    return redirect(back_url)


# ---------- Fotos de Antes e Depois (as rotas ficam em jobs.py) ----------

def fetch_job_photos(job_id):
    return get_db().execute(
        "SELECT * FROM job_photos WHERE job_id = ? ORDER BY id", (job_id,)
    ).fetchall()


def job_photo_exists(job_id, filename):
    return get_db().execute(
        "SELECT 1 FROM job_photos WHERE job_id = ? AND filename = ?", (job_id, filename)
    ).fetchone() is not None


def delete_job_photo(job_id, photo_id, user, manage_all):
    """Quem cuida da agenda (ou dos clientes) apaga qualquer foto; os outros, só as que enviaram."""
    db = get_db()
    row = db.execute("SELECT * FROM job_photos WHERE id = ? AND job_id = ?", (photo_id, job_id)).fetchone()
    if row is None:
        return False, i18n.t("photos.not_found")
    if not manage_all and row["uploaded_by"] != user["id"]:
        return False, i18n.t("photos.not_yours")
    db.execute("DELETE FROM job_photos WHERE id = ?", (photo_id,))
    db.commit()
    _remove_files(job_dir(job_id), row["filename"])
    return True, i18n.t("photos.deleted")


# ---------- Galeria do cliente (quem tem acesso a Clientes) ----------

def fetch_photos(client_id):
    return get_db().execute(
        "SELECT p.*, u.name AS uploaded_by_name FROM client_photos p "
        "LEFT JOIN users u ON u.id = p.uploaded_by "
        "WHERE p.client_id = ? ORDER BY p.id",  # da mais antiga pra mais nova: a que chega entra no fim
        (client_id,),
    ).fetchall()


def client_photo_item(client_id, photo_id, filename):
    """Uma foto da galeria, do jeito que a tela (e o photos.js) usa."""
    return {
        "full": url_for("photos.client_photo_file", client_id=client_id, filename=filename),
        "mini": url_for("photos.client_photo_file", client_id=client_id, filename=filename, mini=1),
        "delete": url_for("photos.delete_client_photo", client_id=client_id, photo_id=photo_id),
    }


def _client_exists(client_id):
    return get_db().execute("SELECT 1 FROM clients WHERE id = ?", (client_id,)).fetchone() is not None


@bp.route("/clientes/<int:client_id>/fotos", methods=("POST",))
@permission_required("clients")
def upload_client_photos(client_id):
    if not _client_exists(client_id):
        abort(404)
    upload = add_client_photos(client_id, request.files.getlist("photo"), g.user["id"])
    items = [client_photo_item(client_id, photo_id, filename) for photo_id, filename in upload.added]
    return respond(upload, items, url_for("clients.client_detail", client_id=client_id, _anchor="fotos"))


@bp.route("/clientes/<int:client_id>/fotos/<int:photo_id>/excluir", methods=("POST",))
@permission_required("clients")
def delete_client_photo(client_id, photo_id):
    db = get_db()
    row = db.execute(
        "SELECT * FROM client_photos WHERE id = ? AND client_id = ?", (photo_id, client_id)
    ).fetchone()
    back = url_for("clients.client_detail", client_id=client_id, _anchor="fotos")
    if row is None:
        return respond_delete(False, i18n.t("photos.not_found"), back)
    db.execute("DELETE FROM client_photos WHERE id = ?", (photo_id,))
    db.commit()
    _remove_files(client_dir(client_id), row["filename"])
    return respond_delete(True, i18n.t("photos.deleted"), back)


@bp.route("/clientes/<int:client_id>/fotos/<filename>")
@permission_required("clients")
def client_photo_file(client_id, filename):
    if get_db().execute("SELECT 1 FROM client_photos WHERE client_id = ? AND filename = ?",
                        (client_id, filename)).fetchone() is None:
        abort(404)
    return send_photo(client_dir(client_id), filename, mini=request.args.get("mini") == "1")
