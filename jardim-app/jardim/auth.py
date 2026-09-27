"""Login, logout, primeiro acesso, troca de senha e regras de permissão."""
import hmac
import os
import secrets
import time
from datetime import datetime, timezone
from functools import wraps

from flask import (Blueprint, abort, flash, g, redirect, render_template,
                   request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

from . import i18n
from .db import get_db
from .i18n import DEFAULT_LANGUAGE
from .utils import safe_next, valid_email

bp = Blueprint("auth", __name__)

MIN_PASSWORD = 8
MAX_ATTEMPTS = 5          # 5 erros de senha...
WINDOW_SECONDS = 600      # ...em 10 minutos bloqueiam novas tentativas por um tempo
_failed_logins = {}       # {(email, ip): [horários das falhas]}
_DUMMY_HASH = generate_password_hash("senha-falsa-para-igualar-o-tempo-de-resposta")


# ---------- Ajudas reutilizadas por outros módulos ----------

def hash_password(password):
    return generate_password_hash(password)


def password_error(password, confirm=None):
    if len(password) < MIN_PASSWORD:
        return i18n.t("auth.password_too_short", n=MIN_PASSWORD)
    if confirm is not None and password != confirm:
        return i18n.t("auth.password_mismatch")
    return None


def has_users():
    return get_db().execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None


def new_session_key():
    """Chave do login de uma empresa guardada na sessão: trocar a chave (senha nova, desativar) derruba
    quem estava logado com ela, em qualquer aparelho."""
    return secrets.token_urlsafe(16)


def email_in_use(email, user_id=None, company_user_id=None):
    """O e-mail já é o login de alguém da equipe ou da área de uma empresa (a tela de login é uma só)?"""
    return get_db().execute(
        "SELECT 1 FROM users WHERE email = ? AND id IS NOT ? "
        "UNION ALL SELECT 1 FROM company_users WHERE email = ? AND id IS NOT ? LIMIT 1",
        (email, user_id, email, company_user_id)).fetchone() is not None


def portal_required(view):
    """Telas da área da empresa: só pra quem entrou com um login de empresa (g.portal)."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.get("portal") is None:
            target = request.full_path.rstrip("?") if request.method == "GET" else None
            return redirect(url_for("auth.login", next=target))
        return view(*args, **kwargs)

    return wrapped


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            target = request.full_path.rstrip("?") if request.method == "GET" else None
            return redirect(url_for("auth.login", next=target))
        return view(*args, **kwargs)

    return wrapped


def owner_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if g.user["role"] != "owner":
            abort(403)
        return view(*args, **kwargs)

    return wrapped


# ---------- Cargos e acessos ----------
# Dono: tudo. Gerente: só as áreas que o dono marcou no cadastro dele (coluna users.permissions).
# Funcionário: só os trabalhos dele. Equipe e as configurações da empresa (em Conta) são sempre só do dono (owner_required),
# assim um gerente nunca consegue mudar cargos nem se dar mais acesso.
PERMISSIONS = ("schedule", "clients", "quotes", "report", "cash", "finance")
SENSITIVE_PERMISSIONS = ("report", "cash", "finance")


def permissions_of(user):
    if user is None:
        return set()
    if user["role"] == "owner":
        return set(PERMISSIONS)
    if user["role"] == "manager":
        return {p for p in (user["permissions"] or "").split(",") if p in PERMISSIONS}
    return set()


def can(permission):
    """No código e nos templates: can('report') = a pessoa logada pode ver o relatório?"""
    return permission in permissions_of(g.get("user"))


def permission_required(permission):
    def decorator(view):
        @wraps(view)
        @login_required
        def wrapped(*args, **kwargs):
            if not can(permission):
                abort(403)
            return view(*args, **kwargs)

        return wrapped

    return decorator


# ---------- Bloqueio simples contra tentativas em massa ----------

def _is_blocked(key):
    now = time.time()
    recent = [t for t in _failed_logins.get(key, []) if now - t < WINDOW_SECONDS]
    _failed_logins[key] = recent
    return len(recent) >= MAX_ATTEMPTS


def _register_failure(key):
    if len(_failed_logins) > 5000:  # evita crescer sem limite
        _failed_logins.clear()
    _failed_logins.setdefault(key, []).append(time.time())


# ---------- Carrega o usuário logado em toda requisição ----------

@bp.before_app_request
def load_user():
    if request.endpoint in ("static", "manifest"):
        return None
    g.user = g.portal = None
    row = get_db().execute("SELECT language FROM settings WHERE id = 1").fetchone()
    g.lang = row["language"] if row else DEFAULT_LANGUAGE
    user_id = session.get("user_id")
    if user_id is not None:
        g.user = get_db().execute(
            "SELECT * FROM users WHERE id = ? AND active = 1", (user_id,)
        ).fetchone()
        if g.user is None:  # usuário desativado ou apagado: derruba a sessão
            session.clear()
        else:
            if g.user["language"] in i18n.LANGUAGES:  # a pessoa escolheu o idioma dela em Conta
                g.lang = g.user["language"]
            from . import notifications  # importado aqui dentro para evitar import circular
            notifications.ensure_daily_reminders()
    elif session.get("portal_user_id") is not None:
        # Alguém de uma empresa, na área dela (portal.py). Não é da equipe: g.user continua vazio,
        # então todas as outras telas mandam pro login.
        g.portal = get_db().execute(
            "SELECT cu.*, co.name AS company_name FROM company_users cu JOIN companies co ON co.id = cu.company_id "
            "WHERE cu.id = ? AND cu.active = 1 AND cu.session_key != '' AND cu.session_key = ?",
            (session["portal_user_id"], session.get("portal_key", ""))).fetchone()
        if g.portal is None:  # desativado, apagado, senha trocada (a chave mudou) ou a empresa foi excluída
            session.clear()
        elif g.portal["language"] in i18n.LANGUAGES:
            g.lang = g.portal["language"]
    # Primeiro acesso: ainda não existe ninguém, então manda criar a conta do dono.
    if (g.user is None and request.endpoint not in (None, "auth.setup")
            and not has_users()):
        return redirect(url_for("auth.setup"))
    return None


# ---------- Telas ----------

@bp.route("/setup", methods=("GET", "POST"))
def setup():
    if has_users():
        return redirect(url_for("auth.login"))
    setup_key = os.environ.get("SETUP_KEY", "")
    if request.method == "POST":
        name = request.form.get("name", "").strip()[:120]
        email = request.form.get("email", "").strip().lower()[:200]
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")
        errors = []
        if setup_key and not hmac.compare_digest(
                setup_key.encode(), request.form.get("setup_key", "").encode()):
            errors.append(i18n.t("auth.setup_key_wrong"))
        if not name:
            errors.append(i18n.t("auth.name_required"))
        if not valid_email(email):
            errors.append(i18n.t("auth.email_invalid"))
        if (msg := password_error(password, confirm)):
            errors.append(msg)
        if not errors:
            db = get_db()
            cur = db.execute(
                "INSERT INTO users (name, email, password_hash, role) VALUES (?, ?, ?, 'owner')",
                (name, email, hash_password(password)),
            )
            db.commit()
            session.clear()
            session["user_id"] = cur.lastrowid
            session.permanent = True
            flash(i18n.t("auth.setup_success"), "ok")
            return redirect(url_for("index"))
        for msg in errors:
            flash(msg, "error")
    return render_template("setup.html", needs_key=bool(setup_key), form=request.form)


@bp.route("/login", methods=("GET", "POST"))
def login():
    if g.user is not None or g.portal is not None:
        return redirect(url_for("index"))
    email = ""
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        key = (email, request.remote_addr)
        if _is_blocked(key):
            flash(i18n.t("auth.throttled"), "error")
            return render_template("login.html", email=email), 429
        db = get_db()
        user = db.execute("SELECT * FROM users WHERE email = ? AND active = 1", (email,)).fetchone()
        # Não é da equipe? Pode ser alguém de uma empresa (área da empresa). O e-mail nunca está nos dois.
        portal = None if user else db.execute(
            "SELECT cu.* FROM company_users cu JOIN companies co ON co.id = cu.company_id "
            "WHERE cu.email = ? AND cu.active = 1", (email,)).fetchone()
        account = user or portal
        # Confere a senha mesmo se o e-mail não existir, para o tempo de resposta ser igual.
        password_ok = check_password_hash(account["password_hash"] if account else _DUMMY_HASH, password)
        if account is not None and password_ok:
            _failed_logins.pop(key, None)
            session.clear()
            session.permanent = True
            target = safe_next(request.args.get("next"))
            if user is not None:
                session["user_id"] = user["id"]
                return redirect(target or url_for("index"))
            key = portal["session_key"] or new_session_key()
            session["portal_user_id"], session["portal_key"] = portal["id"], key
            db.execute("UPDATE company_users SET last_login_at = ?, session_key = ? WHERE id = ?",
                       (datetime.now(timezone.utc).isoformat(timespec="seconds"), key, portal["id"]))
            db.commit()
            # só volta pra onde estava se for dentro da área da empresa
            return redirect(target if target and target.startswith("/portal/") else url_for("portal.days"))
        _register_failure(key)
        flash(i18n.t("auth.bad_credentials"), "error")
    return render_template("login.html", email=email)


@bp.route("/logout", methods=("POST",))
def logout():
    session.clear()
    return redirect(url_for("auth.login"))


# ---------- Minha conta: um menu com um assunto por linha, e uma página pra cada assunto ----------
# Todo mundo: senha e idioma. O dono também tem o perfil e as configurações da empresa
# (dados, aparência, avisos por e-mail, fotos e relatório), que ficam em preferences.py.

ROLE_KEYS = {"owner": "common.role_owner", "manager": "common.role_manager", "employee": "common.role_employee"}


def _app_language():
    row = get_db().execute("SELECT language FROM settings WHERE id = 1").fetchone()
    return row["language"] if row and row["language"] in i18n.LANGUAGES else i18n.DEFAULT_LANGUAGE


def _language_context():
    return {"languages": i18n.LANGUAGES, "my_language": g.user["language"],
            "default_language_name": i18n.LANGUAGES[_app_language()]}


@bp.route("/conta")
@login_required
def account():
    company = []
    if g.user["role"] == "owner":
        from . import preferences  # importado aqui: o preferences.py já importa este arquivo
        company = preferences.menu()
    mine = g.user["language"]
    language_value = (i18n.LANGUAGES[mine] if mine in i18n.LANGUAGES
                      else i18n.t("account.language_default", name=i18n.LANGUAGES[_app_language()]))
    return render_template("account.html", company=company, language_value=language_value,
                           role_label=i18n.t(ROLE_KEYS.get(g.user["role"], "common.role_employee")))


@bp.route("/conta/senha", methods=("GET", "POST"))
@login_required
def password():
    if request.method == "POST":
        current = request.form.get("current_password", "")
        new = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")
        if not check_password_hash(g.user["password_hash"], current):
            flash(i18n.t("auth.wrong_current_password"), "error")
        elif (msg := password_error(new, confirm)):
            flash(msg, "error")
        else:
            db = get_db()
            db.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                       (hash_password(new), g.user["id"]))
            db.commit()
            flash(i18n.t("auth.password_changed"), "ok")
            return redirect(url_for("auth.account"))
    return render_template("account_password.html")


@bp.route("/conta/idioma", methods=("GET", "POST"))
@login_required
def language():
    """Cada pessoa escolhe o idioma do app pra ela ('' = segue o padrão da equipe).
    O dono também escolhe aqui o padrão da equipe (vale pra quem não escolheu o seu)."""
    owner = g.user["role"] == "owner"
    if request.method == "POST":
        choice = request.form.get("language", "")
        if choice not in i18n.LANGUAGES:
            choice = ""
        db = get_db()
        db.execute("UPDATE users SET language = ? WHERE id = ?", (choice, g.user["id"]))
        team = request.form.get("app_language", "")
        if owner and team in i18n.LANGUAGES:
            db.execute("INSERT OR IGNORE INTO settings (id) VALUES (1)")
            db.execute("UPDATE settings SET language = ? WHERE id = 1", (team,))
        db.commit()
        flash(i18n.translate("account.language_saved", choice or _app_language()), "ok")  # a mensagem já no idioma novo
        return redirect(url_for("auth.language"))
    return render_template("account_language.html", owner=owner, app_language=_app_language(), **_language_context())

