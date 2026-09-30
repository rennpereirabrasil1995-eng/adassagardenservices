"""Configurações do dono, dentro de Minha conta: cada assunto na sua página.

  /conta/perfil         nome e telefone do dono (moram na tabela users)
  /conta/empresa        nome, telefone, e-mail e endereço da empresa
  /conta/aparencia      nome no topo, logo e cores (branding.py)
  /conta/avisos         avisos por e-mail: Gmail, senha de app, teste e o link diário
  /conta/lembretes      lembrete pro cliente na véspera (com um toque ou SMS automático pelo Twilio)
  /conta/fotos          quanto espaço as fotos já ocupam
  /conta/relatorio-pdf  baixar o relatório da empresa de uma semana, mês ou ano

O resto fica na linha única da tabela settings (id sempre 1). A senha, o idioma e o menu de
Minha conta (que todo mundo tem) ficam em auth.py. O endereço antigo /preferencias leva pro menu.
"""
import json
import re
import smtplib

from flask import Blueprint, current_app, flash, g, jsonify, redirect, render_template, request, url_for

from . import branding, i18n, notifications, photos, reminders, utils
from .auth import login_required, owner_required
from .db import get_db
from .utils import valid_email

bp = Blueprint("preferences", __name__)

COMPANY_LIMITS = {"company_name": 120, "company_phone": 40, "company_email": 200, "company_address": 300}


def get_settings():
    return dict(notifications.settings())


def _megabytes(num_bytes):
    value = num_bytes / 1024 / 1024
    text = f"{value:.1f}" if value < 10 else f"{value:.0f}"
    return (text.replace(".", ",") if g.get("lang") == "pt_BR" else text) + " MB"


def _photo_space():
    """Quanto as fotos já ocupam e o teto (PHOTO_SPACE_MB). Quando encher, fotos novas são recusadas."""
    used, limit = photos.space_used(), photos.space_limit()
    percent = min(100, round(used * 100 / limit)) if limit else 0
    return {"used": _megabytes(used), "limit": _megabytes(limit) if limit else "", "percent": percent,
            "level": "full" if percent >= 95 else ("high" if percent >= 80 else "ok")}


def _photo_space_text(space):
    return (i18n.t("prefs.photos_used", used=space["used"], limit=space["limit"]) if space["limit"]
            else i18n.t("prefs.photos_used_nolimit", used=space["used"]))


def menu():
    """As linhas "Empresa" do menu de Minha conta, cada uma com o que está valendo agora."""
    row = notifications.settings()
    theme = row["theme"] if row["theme"] in branding.PRESETS else ("custom" if row["theme"] == "custom" else branding.DEFAULT_THEME)
    theme_name = i18n.t("prefs.look_custom") if theme == "custom" else i18n.t(f"prefs.theme_{theme}")
    look = i18n.t("account.look_value_logo" if row["logo_version"] else "account.look_value", theme=theme_name)
    if row["notify_email"] and row["mail_address"]:
        notices = i18n.t("account.notices_on", address=row["mail_address"])
    else:
        notices = i18n.t("account.notices_off")
    company = " · ".join(v for v in (row["company_name"], row["company_phone"]) if v) or i18n.t("account.company_empty")
    return [
        {"icon": "building", "title": i18n.t("account.company_title"), "value": company, "url": url_for("preferences.company")},
        {"icon": "palette", "title": i18n.t("prefs.look_title_short"), "value": look, "url": url_for("preferences.appearance")},
        {"icon": "mail", "title": i18n.t("account.notices_title"), "value": notices, "url": url_for("preferences.notices")},
        {"icon": "chat", "title": i18n.t("account.reminders_title"), "value": _reminder_value(row),
         "url": url_for("preferences.client_reminders")},
        {"icon": "image", "title": i18n.t("prefs.photos_title"), "value": _photo_space_text(_photo_space()),
         "url": url_for("preferences.photo_space")},
        {"icon": "file", "title": i18n.t("account.report_title"), "value": i18n.t("account.report_value"),
         "url": url_for("preferences.report_pdf")},
    ]


def _reminder_value(row):
    mode = row["reminder_mode"] if row["reminder_mode"] in reminders.MODES else "off"
    if mode == "sms":
        return i18n.t("account.reminders_value_sms", hour=row["reminder_hour"])
    return i18n.t(f"account.reminders_value_{mode}")


@bp.route("/preferencias", strict_slashes=False)
@login_required
def legacy():
    """Endereço antigo (aba Preferências): tudo agora fica em Minha conta."""
    return redirect(url_for("auth.account"))


# ---------- Perfil e empresa ----------

@bp.route("/conta/perfil", methods=("GET", "POST"))
@owner_required
def profile():
    form = {"name": g.user["name"], "phone": g.user["phone"]}
    if request.method == "POST":
        form = {"name": request.form.get("name", "").strip()[:120], "phone": utils.read_phone(request.form, "phone")}
        if not form["name"]:
            flash(i18n.t("auth.name_required"), "error")
        else:
            db = get_db()
            db.execute("UPDATE users SET name = ?, phone = ? WHERE id = ?", (form["name"], form["phone"], g.user["id"]))
            db.commit()
            flash(i18n.t("account.profile_saved"), "ok")
            return redirect(url_for("preferences.profile"))
    return render_template("account_profile.html", form=form)


@bp.route("/conta/empresa", methods=("GET", "POST"))
@owner_required
def company():
    row = notifications.settings()
    form = {key: row[key] for key in COMPANY_LIMITS}
    if request.method == "POST":
        form = {key: request.form.get(key, "").strip()[:limit] for key, limit in COMPANY_LIMITS.items()}
        form["company_phone"] = utils.read_phone(request.form, "company_phone")
        if form["company_email"] and not valid_email(form["company_email"]):
            flash(i18n.t("clients.email_invalid"), "error")
        else:
            db = get_db()
            db.execute("UPDATE settings SET company_name = ?, company_phone = ?, company_email = ?, company_address = ? "
                       "WHERE id = 1", tuple(form[key] for key in COMPANY_LIMITS))
            db.commit()
            flash(i18n.t("account.company_saved"), "ok")
            return redirect(url_for("preferences.company"))
    return render_template("account_company.html", form=form)


# ---------- Avisos por e-mail ----------

@bp.route("/conta/avisos", methods=("GET", "POST"))
@owner_required
def notices():
    row = notifications.settings()
    form = {"notify_email": row["notify_email"], "mail_address": row["mail_address"]}
    if request.method == "POST":
        form = {"notify_email": 1 if request.form.get("notify_email") else 0,
                "mail_address": request.form.get("mail_address", "").strip().lower()[:200]}
        new_password = request.form.get("mail_password", "").strip()[:100]
        errors = []
        if form["mail_address"] and not valid_email(form["mail_address"]):
            errors.append(i18n.t("prefs.mail_invalid"))
        if form["notify_email"] and not (form["mail_address"] and (new_password or row["mail_password"])):
            errors.append(i18n.t("prefs.mail_incomplete"))
        if not errors:
            db = get_db()
            db.execute("UPDATE settings SET notify_email = ?, mail_address = ? WHERE id = 1",
                       (form["notify_email"], form["mail_address"]))
            if new_password:  # em branco = mantém a senha que já estava salva
                db.execute("UPDATE settings SET mail_password = ? WHERE id = 1", (new_password,))
            db.commit()
            flash(i18n.t("account.notices_saved"), "ok")
            return redirect(url_for("preferences.notices"))
        for msg in errors:
            flash(msg, "error")
    daily_url = url_for("notifications.daily_link", key=notifications.ensure_cron_key(), _external=True)
    # a senha de app nunca volta pro navegador: a tela só sabe se ela existe
    return render_template("account_notices.html", form=form, has_mail_password=bool(row["mail_password"]),
                           daily_url=daily_url)


@bp.route("/conta/avisos/teste", methods=("POST",))
@owner_required
def send_test_email():
    row = notifications.settings()
    if not (row["mail_address"] and row["mail_password"]):
        flash(i18n.t("prefs.test_needs_config"), "error")
    else:
        try:
            notifications.send_test_email(g.user["email"])
            flash(i18n.t("prefs.test_sent", to=g.user["email"]), "ok")
        except smtplib.SMTPAuthenticationError:
            flash(i18n.t("prefs.test_auth_failed"), "error")
        except Exception as exc:  # rede fora do ar, Gmail bloqueando etc.
            flash(i18n.t("prefs.test_failed", error=exc.__class__.__name__), "error")
    return redirect(url_for("preferences.notices"))


# ---------- Lembrete pro cliente na véspera ----------

def _reminder_form(row):
    return {"reminder_mode": row["reminder_mode"] if row["reminder_mode"] in reminders.MODES else "off",
            "reminder_hour": row["reminder_hour"], "reminder_language": row["reminder_language"],
            "reminder_text": row["reminder_text"], "twilio_sid": row["twilio_sid"], "twilio_from": row["twilio_from"]}


def _reminder_sample(row):
    """Um exemplo pra prévia da mensagem: o primeiro cliente de amanhã (ou um nome qualquer)."""
    day = reminders.tomorrow()
    first = next(iter(reminders.clients_for(day)), None)
    name, start = (first["name"], first["first_time"]) if first else ("Sarah", "09:00")
    out = {}
    for lang in reminders.DEFAULT_TEXT:
        fake = dict(row)
        fake["reminder_language"] = lang
        out[lang] = reminders.values(fake, name, day, start)
    return out


@bp.route("/conta/lembretes", methods=("GET", "POST"))
@owner_required
def client_reminders():
    row = notifications.settings()
    form = _reminder_form(row)
    if request.method == "POST":
        f = request.form
        mode = f.get("reminder_mode", "off")
        hour = f.get("reminder_hour", "18")
        lang = f.get("reminder_language", "en")
        form = {"reminder_mode": mode if mode in reminders.MODES else "off",
                "reminder_hour": int(hour) if hour.isdigit() and int(hour) in reminders.HOURS else 18,
                "reminder_language": lang if lang in reminders.DEFAULT_TEXT else "en",
                "reminder_text": f.get("reminder_text", "").strip()[:600],
                "twilio_sid": f.get("twilio_sid", "").strip()[:40],
                "twilio_from": f.get("twilio_from", "").strip()[:20]}
        # o texto igual ao padrão não precisa ser guardado: assim ele acompanha a troca de idioma
        if form["reminder_text"] == reminders.DEFAULT_TEXT[form["reminder_language"]]:
            form["reminder_text"] = ""
        if re.fullmatch(r"[+\d\s()-]+", form["twilio_from"] or "x"):  # parece telefone: +44... do jeito do Twilio
            digits = utils.intl_phone(form["twilio_from"])
            form["twilio_from"] = "+" + digits if digits else form["twilio_from"]
        new_token = f.get("twilio_token", "").strip()[:64]
        errors = []
        if form["reminder_mode"] == "sms":
            if not reminders.SID.match(form["twilio_sid"]):
                errors.append(i18n.t("reminders.bad_sid"))
            if not (reminders.TOKEN.match(new_token) if new_token else row["twilio_token"]):
                errors.append(i18n.t("reminders.bad_token"))
            if not (reminders.E164.match(form["twilio_from"]) or reminders.SENDER_NAME.match(form["twilio_from"])):
                errors.append(i18n.t("reminders.bad_sender"))
        elif new_token and not reminders.TOKEN.match(new_token):
            errors.append(i18n.t("reminders.bad_token"))
        if not errors:
            db = get_db()
            db.execute("UPDATE settings SET reminder_mode = ?, reminder_hour = ?, reminder_language = ?, reminder_text = ?, "
                       "twilio_sid = ?, twilio_from = ? WHERE id = 1",
                       (form["reminder_mode"], form["reminder_hour"], form["reminder_language"], form["reminder_text"],
                        form["twilio_sid"], form["twilio_from"]))
            if new_token:  # em branco = mantém o que já estava salvo (o token nunca volta pra tela)
                db.execute("UPDATE settings SET twilio_token = ? WHERE id = 1", (new_token,))
            db.commit()
            flash(i18n.t("reminders.settings_saved"), "ok")
            return redirect(url_for("preferences.client_reminders"))
        for msg in errors:
            flash(msg, "error")
    owner_phone = g.user["phone"] or ""
    count = get_db().execute("SELECT COUNT(*) AS total, COALESCE(SUM(reminders), 0) AS on_ FROM clients WHERE active = 1").fetchone()
    return render_template("account_reminders.html", form=form, has_token=bool(row["twilio_token"]),
                           sms_ready=reminders.sms_ready(row), sample=_reminder_sample(row),
                           defaults=reminders.DEFAULT_TEXT, placeholders=reminders.PLACEHOLDERS,
                           hours=reminders.HOURS, test_to=owner_phone, clients_total=count["total"], clients_on=count["on_"])


@bp.route("/conta/lembretes/teste", methods=("POST",))
@owner_required
def reminder_test():
    """Manda um lembrete de exemplo pra um número (o do dono, por padrão), pelo Twilio."""
    row = notifications.settings()
    digits = utils.intl_phone(request.form.get("to", ""))
    if not reminders.sms_ready(row):
        flash(i18n.t("reminders.sms_not_ready"), "error")
    elif not digits:
        flash(i18n.t("reminders.test_bad_number"), "error")
    else:
        body = reminders.render(reminders.template_of(row),
                                reminders.values(row, g.user["name"].split(" ")[0], reminders.tomorrow(), "09:00"))
        try:
            reminders.send_sms(row, digits, body)
            flash(i18n.t("reminders.test_sent", to="+" + digits), "ok")
        except reminders.SmsError as exc:
            flash(i18n.t("reminders.test_failed", error=str(exc)), "error")
    return redirect(url_for("preferences.client_reminders"))


# ---------- Fotos e relatório ----------

@bp.route("/conta/fotos")
@owner_required
def photo_space():
    return render_template("account_photos.html", photo_space=_photo_space())


@bp.route("/conta/relatorio-pdf")
@owner_required
def report_pdf():
    return render_template("account_report.html")


# ---------- Aparência: nome no topo, logo e cores ----------

def _look(row):
    main, accent = branding.theme_colors(row)
    presets = []
    for key, (p_main, p_accent) in branding.PRESETS.items():
        light, dark = branding.preview(p_main, p_accent)
        presets.append({"key": key, "label": i18n.t(f"prefs.theme_{key}"), "main": p_main, "accent": p_accent,
                        "light": light, "dark": dark})
    light, dark = branding.preview(main, accent)
    return {"theme": row["theme"] if row["theme"] in branding.PRESETS or row["theme"] == "custom" else branding.DEFAULT_THEME,
            "main": main, "accent": accent, "app_name": row["app_name"], "app_short_name": row["app_short_name"],
            "show_name": row["show_name"], "presets": presets, "current": {"light": light, "dark": dark},
            "low": row["theme"] == "custom" and branding.header_hard_to_read(main),
            "name_default": row["company_name"] or current_app.config["APP_NAME"]}


def _refresh_logo(new_logo=False):
    """Gera o logo e os ícones de novo (logo novo, ou a cor de fundo do ícone mudou com a paleta) e
    anota a versão. Sem mudança nenhuma, fica tudo como está."""
    branding.forget()
    row = branding._settings()
    if not new_logo and not branding.needs_render(row):
        return
    result = branding.render_assets(row)
    version, meta = result if result else ("", {})
    db = get_db()
    db.execute("UPDATE settings SET logo_version = ?, logo_meta = ? WHERE id = 1",
               (version, json.dumps(meta) if meta else ""))
    db.commit()
    branding.forget()


@bp.route("/conta/aparencia", methods=("GET", "POST"))
@owner_required
def appearance():
    row = notifications.settings()  # garante a linha de configurações
    if request.method == "POST":
        _save_look()
        # volta pro topo da página: a mensagem e o topo do app (já com o nome, o logo e as cores novas) ficam à vista
        return redirect(url_for("preferences.appearance"))
    return render_template("account_appearance.html", look=_look(row))


def _save_look():
    f = request.form
    db = get_db()
    if f.get("reset"):
        db.execute("UPDATE settings SET app_name = '', app_short_name = '', show_name = 1, theme = ?, "
                   "color_main = '', color_accent = '' WHERE id = 1", (branding.DEFAULT_THEME,))
        db.commit()
        _refresh_logo()
        flash(i18n.t("prefs.look_reset_done"), "ok")
        return
    theme = f.get("theme", branding.DEFAULT_THEME)
    main, accent = f.get("color_main", "").strip().lower(), f.get("color_accent", "").strip().lower()
    if theme != "custom" and theme not in branding.PRESETS:
        theme = branding.DEFAULT_THEME
    if theme == "custom" and not (branding.HEX.match(main) and branding.HEX.match(accent)):
        flash(i18n.t("prefs.look_bad_color"), "error")
        return
    db.execute("UPDATE settings SET app_name = ?, app_short_name = ?, show_name = ?, theme = ?, color_main = ?, "
               "color_accent = ? WHERE id = 1",
               (f.get("app_name", "").strip()[:40], f.get("app_short_name", "").strip()[:12],
                1 if f.get("show_name") else 0, theme, main if theme == "custom" else "",
                accent if theme == "custom" else ""))
    db.commit()
    logo = request.files.get("logo")
    new_logo = False
    if f.get("remove_logo"):
        branding.remove_logo()
        new_logo = True
    elif logo and logo.filename:
        new_logo = branding.save_logo(logo)
        if not new_logo:
            flash(i18n.t("prefs.look_bad_logo"), "error")
    _refresh_logo(new_logo)
    flash(i18n.t("prefs.look_saved"), "ok")
    if theme == "custom" and branding.header_hard_to_read(main):
        flash(i18n.t("prefs.look_low_contrast"), "info")


@bp.route("/conta/aparencia/cores")
@owner_required
def look_colors():
    """Cores calculadas pra prévia da paleta personalizada (o formulário mostra antes de salvar)."""
    main, accent = request.args.get("main", "").lower(), request.args.get("accent", "").lower()
    if not (branding.HEX.match(main) and branding.HEX.match(accent)):
        return jsonify(error="bad color"), 400
    light, dark = branding.preview(main, accent)
    return jsonify(light=light, dark=dark, low=branding.header_hard_to_read(main))
