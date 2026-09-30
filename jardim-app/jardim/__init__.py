"""App de gestão de jardinagem (Flask + SQLite).

Mapa rápido do projeto:
  auth.py     login, logout, primeiro acesso, troca de senha e regras de permissão
  clients.py  cadastro de clientes
  team.py     equipe (o dono cria, edita e desativa funcionários)
  jobs.py     agenda de trabalhos, tarefas, iniciar/concluir
  photos.py   fotos: galeria de cada cliente, Antes/Depois de cada trabalho e fotos das cotações
  quotes.py   cotações: a equipe monta e manda o link; o cliente abre sem login e aceita ou recusa
  companies.py  empresas com vários jardins (lado do dono): jardins, logins e a prévia
  portal.py   área da empresa: quem é da empresa entra e vê (só vê) os serviços concluídos, e comenta
  portal_mail.py  resumo do dia por e-mail pra empresa
  notifications.py  avisos automáticos para a equipe (no app e, se ligado, por e-mail)
  reports.py  relatório da empresa (trabalhos, horas, dinheiro recebido, clientes)
  finance.py  finanças: transferências registradas pelo dono + dinheiro dos trabalhos
  db.py       conexão com o banco e migrações
  utils.py    datas, validações e filtros usados nos templates
  templates/  as telas (HTML)
  static/     visual (CSS e fontes)
"""
import hmac
import os
import secrets
from datetime import timedelta
from pathlib import Path

from flask import Flask, abort, g, jsonify, redirect, render_template, request, session, url_for
from markupsafe import Markup, escape
from werkzeug.exceptions import HTTPException
from werkzeug.routing import IntegerConverter, ValidationError

from . import (auth, branding, changes, clients, companies, db, extras, finance, hours, i18n, jobs, notifications, photos,
               portal, portal_mail, preferences, quote_requests, quotes, reminders, reports, team, utils)

ERROR_CODES = (400, 403, 404, 405, 413, 429, 500)


class _SafeInt(IntegerConverter):
    """O <int:...> das rotas, mas com no máximo 18 dígitos (cabe no SQLite): mais que isso é 404."""

    def to_python(self, value):
        if len(value) > 18:
            raise ValidationError()
        return super().to_python(value)


def _load_secret_key(app):
    """Usa SECRET_KEY do ambiente; se não houver, cria e guarda uma em instance/secret_key."""
    from_env = os.environ.get("SECRET_KEY")
    if from_env:
        return from_env
    key_file = Path(app.instance_path) / "secret_key"
    if not key_file.exists():
        key_file.write_text(secrets.token_hex(32))
        try:
            key_file.chmod(0o600)
        except OSError:
            pass
    return key_file.read_text().strip()


def create_app(test_config=None):
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    app = Flask(__name__, instance_relative_config=True)
    Path(app.instance_path).mkdir(parents=True, exist_ok=True)

    app.config.from_mapping(
        APP_NAME=os.environ.get("APP_NAME", "Gestão de Jardinagem"),
        APP_SHORT_NAME=os.environ.get("APP_SHORT_NAME", "Jardinagem"),  # nome sob o ícone na tela inicial
        TIMEZONE=os.environ.get("APP_TIMEZONE", "Europe/London"),
        DATABASE=os.path.join(app.instance_path, "jardim.db"),
        UPLOAD_ROOT=os.path.join(app.instance_path, "uploads"),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE") == "1",
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),  # funcionário não precisa logar todo dia
        MAX_CONTENT_LENGTH=80 * 1024 * 1024,  # um vídeo curto de celular, ou várias fotos de uma vez (o celular reduz antes)
        PHOTO_SPACE_MB=float(os.environ.get("PHOTO_SPACE_MB", "300")),  # teto para as fotos no disco (0 = sem teto)
        DAILY_EMAIL_HOUR=int(os.environ.get("DAILY_EMAIL_HOUR", "17")),  # a partir de que hora saem os e-mails de lembrete
    )
    if test_config:
        app.config.update(test_config)
    if not app.config.get("SECRET_KEY"):
        app.config["SECRET_KEY"] = _load_secret_key(app)

    if os.environ.get("BEHIND_PROXY") == "1":
        # Necessário quando o app roda atrás de um proxy HTTPS (Render, Railway, Nginx...)
        from werkzeug.middleware.proxy_fix import ProxyFix

        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    # Número no endereço (/trabalhos/123) grande demais pro banco vira "não encontrado", e não erro 500.
    app.url_map.converters["int"] = _SafeInt

    db.init_app(app)
    utils.init_app(app)
    app.register_blueprint(auth.bp)
    app.register_blueprint(clients.bp)
    app.register_blueprint(team.bp)
    app.register_blueprint(jobs.bp)
    app.register_blueprint(photos.bp)
    app.register_blueprint(preferences.bp)
    app.register_blueprint(notifications.bp)
    app.register_blueprint(reports.bp)
    app.register_blueprint(finance.bp)
    app.register_blueprint(quotes.bp)
    app.register_blueprint(branding.bp)
    app.register_blueprint(hours.bp)
    app.register_blueprint(reminders.bp)
    app.register_blueprint(companies.bp)
    app.register_blueprint(portal.bp)
    app.register_blueprint(portal_mail.bp)
    app.register_blueprint(extras.bp)
    app.register_blueprint(changes.bp)
    app.register_blueprint(quote_requests.bp)

    # --- Proteção contra CSRF: todo formulário POST leva um token da sessão ---
    def csrf_token():
        if "_csrf" not in session:
            session["_csrf"] = secrets.token_urlsafe(32)
        return session["_csrf"]

    def csrf_field():
        return Markup('<input type="hidden" name="_csrf" value="%s">') % escape(csrf_token())

    @app.before_request
    def csrf_protect():
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            expected = session.get("_csrf")
            sent = request.form.get("_csrf", "")
            if not expected or not hmac.compare_digest(expected.encode(), sent.encode()):
                abort(400)

    @app.context_processor
    def inject_globals():
        brand = branding.context()  # nome, logo e cores escolhidos em Conta → Aparência
        return {
            "current_user": g.get("user"),
            "portal_user": g.get("portal"),  # alguém de uma empresa, na área dela (portal.py)
            "brand": brand,
            "app_name": brand["name"],
            "app_short_name": brand["short_name"],
            "csrf_field": csrf_field,
            "today": utils.today_iso(),
            "t": i18n.t,
            "lang": g.get("lang", i18n.DEFAULT_LANGUAGE),
            "unread_notifications": notifications.unread_count(g.user["id"]) if g.get("user") else 0,
            "can": auth.can,
            "user_permissions": auth.permissions_of(g.get("user")),
        }

    @app.after_request
    def security_headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        if request.endpoint != "static":
            resp.headers.setdefault("Cache-Control", "no-store")
        return resp

    @app.errorhandler(HTTPException)
    def http_error(err):
        message = i18n.t(f"errors.{err.code}") if err.code in ERROR_CODES else err.name
        return render_template("error.html", code=err.code, message=message), err.code

    @app.errorhandler(OverflowError)
    def number_too_big(_err):
        """Um número gigante num formulário ou no endereço (?page=, client_id): não existe, então 404."""
        return render_template("error.html", code=404, message=i18n.t("errors.404")), 404

    @app.route("/manifest.webmanifest")
    def manifest():
        """Permite "Adicionar à tela inicial" no celular: o app abre em tela cheia, como um aplicativo."""
        brand = branding.context()
        logo = brand["logo"]
        resp = jsonify({
            "name": brand["name"],
            "short_name": brand["short_name"],
            "lang": "pt-BR",
            "start_url": url_for("index"),
            "scope": "/",
            "display": "standalone",
            "background_color": brand["theme_light"],
            "theme_color": brand["theme_light"],
            "icons": [
                {"src": logo["icon192"] if logo else url_for("static", filename="icons/icon-192.png"),
                 "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
                {"src": logo["icon512"] if logo else url_for("static", filename="icons/icon-512.png"),
                 "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
            ],
        })
        resp.mimetype = "application/manifest+json"
        return resp

    @app.route("/")
    def index():
        if g.get("portal") is not None:  # alguém de uma empresa: vai pra área dela
            return redirect(url_for("portal.days"))
        if g.user is None:
            return redirect(url_for("auth.login"))
        if auth.can("schedule"):  # dono, ou gerente com acesso à agenda
            return redirect(url_for("jobs.dashboard"))
        return redirect(url_for("jobs.my_jobs"))

    return app
