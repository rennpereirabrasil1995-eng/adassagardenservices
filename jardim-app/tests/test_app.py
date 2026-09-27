"""Testes automáticos. Rode com:  python -m unittest -v

Depois de mudar o código, rode de novo: se algo quebrou, os testes avisam.
"""
import html
import os
import re
import shutil
import smtplib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from datetime import date, datetime, timedelta, timezone
from unittest import mock
from zoneinfo import ZoneInfo

from flask import g

from jardim import auth, create_app, utils
from jardim.db import get_db


class Browser:
    """Um 'navegador' de teste: guarda cookies e cuida do token CSRF."""

    def __init__(self, app):
        self.c = app.test_client()

    def token(self):
        for url in ("/login", "/conta"):
            self.c.get(url, follow_redirects=True)
            with self.c.session_transaction() as s:
                if "_csrf" in s:
                    return s["_csrf"]
        raise AssertionError("não foi possível obter o token CSRF")

    def get(self, url, **kw):
        return self.c.get(url, **kw)

    def post(self, url, data=None, csrf=True, **kw):
        data = dict(data or {})
        if csrf:
            data["_csrf"] = self.token()
        return self.c.post(url, data=data, **kw)

    def login(self, email, password):
        return self.post("/login", {"email": email, "password": password})


class FakeSMTP:
    """Servidor de e-mail de mentira: guarda o que seria enviado, sem sair para a internet."""
    sent = []
    fail_login = False

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port

    def starttls(self):
        pass

    def login(self, user, password):
        if FakeSMTP.fail_login:
            raise smtplib.SMTPAuthenticationError(535, b"5.7.8 Username and Password not accepted")
        self.user, self.password = user, password

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)

    def quit(self):
        pass


class FakeTwilio:
    """Twilio de mentira: guarda os SMS que sairiam. errors = {"+44...": (status HTTP, resposta)} simula falhas."""
    sent = []
    errors = {}

    @staticmethod
    def post(url, data, user, password):
        FakeTwilio.sent.append({"url": url, "to": data["To"], "from": data["From"], "body": data["Body"],
                                "user": user, "password": password})
        if data["To"] in FakeTwilio.errors:
            return FakeTwilio.errors[data["To"]]
        return 201, {"sid": f"SM{len(FakeTwilio.sent):032d}"}


class AppTests(unittest.TestCase):
    def setUp(self):
        auth._failed_logins.clear()
        FakeSMTP.sent, FakeSMTP.fail_login = [], False
        self.smtp = mock.patch("jardim.notifications.smtplib.SMTP", FakeSMTP)  # nenhum teste manda e-mail de verdade
        self.smtp.start()
        FakeTwilio.sent, FakeTwilio.errors = [], {}
        self.twilio = mock.patch("jardim.reminders._post", FakeTwilio.post)  # nem SMS de verdade
        self.twilio.start()
        # relógio do resumo do dia pra empresa: 9h de hoje (nenhum resumo sai no meio dos outros testes)
        self.mail_now = [datetime.now(ZoneInfo("Europe/London")).replace(hour=9, minute=0, second=0, microsecond=0)]
        self.mail_clock = mock.patch("jardim.portal_mail._now", lambda: self.mail_now[0])
        self.mail_clock.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.app = create_app({
            "TESTING": True,
            "SECRET_KEY": "chave-de-teste",
            "DATABASE": os.path.join(self.tmp.name, "teste.db"),
            "UPLOAD_ROOT": os.path.join(self.tmp.name, "uploads"),
        })
        self.owner = Browser(self.app)

    def tearDown(self):
        self.smtp.stop()
        self.twilio.stop()
        self.mail_clock.stop()
        self.tmp.cleanup()

    # ---------- atalhos ----------
    def query(self, sql, params=()):
        with self.app.app_context():
            return [dict(r) for r in get_db().execute(sql, params).fetchall()]

    def create_owner(self):
        r = self.owner.post("/setup", {"name": "Dono Teste", "email": "dono@example.com",
                                       "password": "senha-do-dono-1", "confirm_password": "senha-do-dono-1"})
        self.assertEqual(r.status_code, 302)

    def create_client(self, name="Sítio das Flores"):
        self.owner.post("/clientes/novo", {"name": name, "address": "12 Rose Lane", "postcode": "N1 1AA",
                                           "phone": "07123 456789", "access_notes": "Portão azul, código 1234",
                                           "notes": "Só o dono deve ler isto"})
        return self.query("SELECT id FROM clients WHERE name = ?", (name,))[0]["id"]

    def create_employee(self, name="Ana", email="ana@example.com", password="senha-da-ana-1"):
        self.owner.post("/equipe/novo", {"name": name, "email": email, "password": password, "role": "employee"})
        return self.query("SELECT id FROM users WHERE email = ?", (email,))[0]["id"]

    def create_job(self, client_id, employee_id, day=None, tasks="Cortar a grama\nPodar a cerca-viva\nEnsacar resíduos"):
        self.owner.post("/trabalhos/novo", {
            "client_id": client_id, "assigned_to": employee_id or "", "title": "Manutenção",
            "job_date": day or utils_today(self.app), "start_time": "08:30", "tasks": tasks})
        return self.query("SELECT id FROM jobs ORDER BY id DESC LIMIT 1")[0]["id"]

    def day(self, n):
        return (date.fromisoformat(utils_today(self.app)) + timedelta(days=n)).isoformat()

    def notices(self, user_id, kind=None):
        sql, args = "SELECT * FROM notifications WHERE user_id = ?", [user_id]
        if kind:
            sql, args = sql + " AND kind = ?", args + [kind]
        return self.query(sql + " ORDER BY id", args)

    def people(self, job_id):
        return [r["user_id"] for r in self.query(
            "SELECT user_id FROM job_assignees WHERE job_id = ? ORDER BY user_id", (job_id,))]

    def edit_job(self, job_id, **changes):
        job = self.query("SELECT * FROM jobs WHERE id = ?", (job_id,))[0]
        data = {"client_id": job["client_id"], "assigned_to": self.people(job_id) or "", "title": job["title"],
                "job_date": job["job_date"], "start_time": job["start_time"], "description": job["description"],
                "tasks": "Cortar a grama", "status": job["status"]}
        data.update(changes)
        self.assertEqual(self.owner.post(f"/trabalhos/{job_id}/editar", data).status_code, 302)

    def backdate_notices(self, days=3):
        old = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        with self.app.app_context():
            db = get_db()
            db.execute("UPDATE notifications SET created_at = ?", (old,))
            db.commit()

    def enable_email(self, password="wxyz wxyz wxyz wxyz"):
        r = self.owner.post("/conta/avisos", {
            "notify_email": "1", "mail_address": "renan.jardins@gmail.com", "mail_password": password})
        self.assertEqual(r.status_code, 302)

    def employee_browser(self, email="ana@example.com", password="senha-da-ana-1"):
        b = Browser(self.app)
        r = b.login(email, password)
        self.assertEqual(r.status_code, 302, "login do funcionário deveria funcionar")
        return b

    # ---------- primeiro acesso e segurança básica ----------
    def test_first_visit_goes_to_setup_then_locks(self):
        r = self.owner.get("/")
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].endswith("/setup"))
        self.create_owner()
        self.assertEqual(self.owner.get("/setup").status_code, 302)
        self.assertEqual(self.query("SELECT role FROM users")[0]["role"], "owner")

    def test_post_without_csrf_is_rejected(self):
        self.create_owner()
        r = Browser(self.app).post("/login", {"email": "dono@example.com", "password": "senha-do-dono-1"}, csrf=False)
        self.assertEqual(r.status_code, 400)

    def test_login_wrong_password_and_throttle(self):
        self.create_owner()
        other = Browser(self.app)
        for _ in range(5):
            r = other.login("dono@example.com", "errada")
            self.assertEqual(r.status_code, 200)
        self.assertEqual(other.login("dono@example.com", "senha-do-dono-1").status_code, 429)

    def test_login_next_only_internal(self):
        self.create_owner()
        b = Browser(self.app)
        r = b.post("/login?next=//evil.example.com", {"email": "dono@example.com", "password": "senha-do-dono-1"})
        self.assertNotIn("evil", r.headers["Location"])
        b2 = Browser(self.app)
        r = b2.post("/login?next=/conta", {"email": "dono@example.com", "password": "senha-do-dono-1"})
        self.assertTrue(r.headers["Location"].endswith("/conta"))

    def test_logged_out_user_is_sent_to_login(self):
        self.create_owner()
        r = Browser(self.app).get("/painel")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers["Location"])

    def test_html_is_escaped(self):
        self.create_owner()
        self.create_client("<script>alert(1)</script>")
        html = self.owner.get("/clientes/").get_data(as_text=True)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)

    # ---------- permissões ----------
    def test_employee_permissions(self):
        self.create_owner()
        client_id = self.create_client()
        ana = self.create_employee()
        bia = self.create_employee("Bia", "bia@example.com", "senha-da-bia-1")
        job_id = self.create_job(client_id, ana)

        ana_b, bia_b = self.employee_browser(), self.employee_browser("bia@example.com", "senha-da-bia-1")

        # A funcionária vê o próprio trabalho e o acesso ao local, mas não as notas internas do cliente
        html = ana_b.get(f"/trabalhos/{job_id}").get_data(as_text=True)
        self.assertIn("Sítio das Flores", html)
        self.assertIn("Portão azul", html)
        self.assertNotIn("Só o dono deve ler isto", html)

        # Páginas do dono estão bloqueadas
        for url in ("/painel", "/trabalhos", "/trabalhos/novo", "/clientes/", "/clientes/novo",
                    f"/clientes/{client_id}", "/equipe/", "/equipe/novo", f"/trabalhos/{job_id}/editar"):
            self.assertEqual(ana_b.get(url).status_code, 403, url)
        self.assertEqual(ana_b.post(f"/trabalhos/{job_id}/excluir").status_code, 403)
        self.assertEqual(ana_b.post(f"/trabalhos/{job_id}/repetir", {"every": 1, "times": 2}).status_code, 403)
        self.assertEqual(self.query("SELECT COUNT(*) AS n FROM jobs")[0]["n"], 1)

        # Outra funcionária não enxerga nem mexe no trabalho da Ana
        self.assertEqual(bia_b.get(f"/trabalhos/{job_id}").status_code, 404)
        self.assertEqual(bia_b.post(f"/trabalhos/{job_id}/atualizar", {"action": "finish"}).status_code, 404)
        self.assertEqual(self.query("SELECT status FROM jobs")[0]["status"], "scheduled")
        self.assertNotIn("Sítio das Flores", bia_b.get("/meus-trabalhos").get_data(as_text=True))

    def test_deactivated_user_loses_access(self):
        self.create_owner()
        ana = self.create_employee()
        ana_b = self.employee_browser()
        self.assertEqual(ana_b.get("/meus-trabalhos").status_code, 200)
        self.owner.post(f"/equipe/{ana}/editar", {"name": "Ana", "email": "ana@example.com", "role": "employee"})
        self.assertEqual(ana_b.get("/meus-trabalhos").status_code, 302)  # sessão derrubada
        self.assertEqual(Browser(self.app).login("ana@example.com", "senha-da-ana-1").status_code, 200)  # não entra

    def test_owner_cannot_lock_self_out(self):
        self.create_owner()
        owner_id = self.query("SELECT id FROM users")[0]["id"]
        self.owner.post(f"/equipe/{owner_id}/editar", {"name": "Dono Teste", "email": "dono@example.com"})
        row = self.query("SELECT role, active FROM users WHERE id = ?", (owner_id,))[0]
        self.assertEqual((row["role"], row["active"]), ("owner", 1))

    def test_duplicate_email_and_short_password_rejected(self):
        self.create_owner()
        self.create_employee()
        self.owner.post("/equipe/novo", {"name": "Outra", "email": "ANA@example.com", "password": "senha-longa-1"})
        self.owner.post("/equipe/novo", {"name": "Curta", "email": "curta@example.com", "password": "123"})
        self.assertEqual(self.query("SELECT COUNT(*) AS n FROM users")[0]["n"], 2)

    # ---------- fluxo do trabalho ----------
    def test_job_lifecycle(self):
        self.create_owner()
        client_id, ana = self.create_client(), None
        ana = self.create_employee()
        job_id = self.create_job(client_id, ana)
        ana_b = self.employee_browser()
        tasks = self.query("SELECT id FROM job_tasks WHERE job_id = ? ORDER BY position", (job_id,))
        self.assertEqual(len(tasks), 3)

        ana_b.post(f"/trabalhos/{job_id}/atualizar", {"action": "start"})
        job = self.query("SELECT * FROM jobs WHERE id = ?", (job_id,))[0]
        self.assertEqual(job["status"], "in_progress")
        self.assertTrue(job["started_at"])

        ana_b.post(f"/trabalhos/{job_id}/atualizar", {
            "action": "finish", f"task_{tasks[0]['id']}": "done", f"task_{tasks[1]['id']}": "done",
            "materials": "2 sacos de resíduo verde", "employee_notes": "Cerca-viva muito alta"})
        job = self.query("SELECT * FROM jobs WHERE id = ?", (job_id,))[0]
        self.assertEqual(job["status"], "done")
        self.assertTrue(job["finished_at"])
        self.assertEqual(job["materials"], "2 sacos de resíduo verde")
        done = [t["done"] for t in self.query("SELECT done FROM job_tasks WHERE job_id = ? ORDER BY position", (job_id,))]
        self.assertEqual(done, [1, 1, 0])

        # Depois de concluído, o funcionário não altera mais
        ana_b.post(f"/trabalhos/{job_id}/atualizar", {"action": "save", "employee_notes": "mudei"})
        self.assertEqual(self.query("SELECT employee_notes FROM jobs")[0]["employee_notes"], "Cerca-viva muito alta")

        # O dono enxerga as anotações
        html = self.owner.get(f"/trabalhos/{job_id}").get_data(as_text=True)
        self.assertIn("Cerca-viva muito alta", html)
        self.assertIn("2 sacos de resíduo verde", html)

    def test_edit_job_keeps_done_flags_and_status_resets_times(self):
        self.create_owner()
        client_id, ana = self.create_client(), self.create_employee()
        job_id = self.create_job(client_id, ana)
        ana_b = self.employee_browser()
        first = self.query("SELECT id FROM job_tasks WHERE job_id = ? ORDER BY position", (job_id,))[0]["id"]
        ana_b.post(f"/trabalhos/{job_id}/atualizar", {"action": "start", f"task_{first}": "done"})

        self.owner.post(f"/trabalhos/{job_id}/editar", {
            "client_id": client_id, "assigned_to": ana, "title": "Manutenção", "job_date": utils_today(self.app),
            "status": "in_progress", "tasks": "Cortar a grama\nRegar os vasos"})
        tasks = self.query("SELECT description, done FROM job_tasks WHERE job_id = ? ORDER BY position", (job_id,))
        self.assertEqual([(t["description"], t["done"]) for t in tasks], [("Cortar a grama", 1), ("Regar os vasos", 0)])

        self.owner.post(f"/trabalhos/{job_id}/editar", {
            "client_id": client_id, "assigned_to": ana, "title": "Manutenção", "job_date": utils_today(self.app),
            "status": "scheduled", "tasks": "Cortar a grama"})
        job = self.query("SELECT status, started_at, finished_at FROM jobs")[0]
        self.assertEqual((job["status"], job["started_at"], job["finished_at"]), ("scheduled", None, None))

    def test_repeat_job(self):
        self.create_owner()
        client_id, ana = self.create_client(), self.create_employee()
        base = date.today()
        job_id = self.create_job(client_id, ana, base.isoformat())
        self.owner.post(f"/trabalhos/{job_id}/repetir", {"every": 2, "times": 3})
        jobs = self.query("SELECT id, job_date FROM jobs ORDER BY job_date")
        self.assertEqual([j["job_date"] for j in jobs],
                         [(base + timedelta(weeks=2 * k)).isoformat() for k in range(4)])
        self.assertTrue(all(self.people(j["id"]) == [ana] for j in jobs))
        self.assertEqual(self.query("SELECT COUNT(*) AS n FROM job_tasks WHERE done = 0")[0]["n"], 12)
        self.owner.post(f"/trabalhos/{job_id}/repetir", {"every": 1, "times": 99})  # fora do limite
        self.assertEqual(self.query("SELECT COUNT(*) AS n FROM jobs")[0]["n"], 4)

    def test_delete_job_removes_tasks(self):
        self.create_owner()
        job_id = self.create_job(self.create_client(), None)
        self.owner.post(f"/trabalhos/{job_id}/excluir")
        self.assertEqual(self.query("SELECT COUNT(*) AS n FROM jobs")[0]["n"], 0)
        self.assertEqual(self.query("SELECT COUNT(*) AS n FROM job_tasks")[0]["n"], 0)

    def test_job_validation(self):
        self.create_owner()
        client_id = self.create_client()
        self.owner.post("/trabalhos/novo", {"client_id": client_id, "title": "X", "job_date": "31/02/2026"})
        self.owner.post("/trabalhos/novo", {"client_id": "999", "title": "X", "job_date": "2026-09-21"})
        self.owner.post("/trabalhos/novo", {"client_id": client_id, "title": "X", "job_date": "2026-09-21", "start_time": "25:99"})
        self.assertEqual(self.query("SELECT COUNT(*) AS n FROM jobs")[0]["n"], 0)

    # ---------- conta ----------
    def test_change_password(self):
        self.create_owner()
        self.create_employee()
        ana_b = self.employee_browser()
        ana_b.post("/conta/senha", {"current_password": "errada", "new_password": "nova-senha-9", "confirm_password": "nova-senha-9"})
        self.assertEqual(Browser(self.app).login("ana@example.com", "nova-senha-9").status_code, 200)
        r = ana_b.post("/conta/senha", {"current_password": "senha-da-ana-1", "new_password": "nova-senha-9", "confirm_password": "nova-senha-9"})
        self.assertTrue(r.headers["Location"].endswith("/conta"))  # volta pro menu da conta
        self.assertEqual(Browser(self.app).login("ana@example.com", "nova-senha-9").status_code, 302)
        self.assertEqual(Browser(self.app).login("ana@example.com", "senha-da-ana-1").status_code, 200)

    # ---------- telas e filtros ----------
    def test_all_pages_render(self):
        self.create_owner()
        client_id, ana = self.create_client(), self.create_employee()
        job_id = self.create_job(client_id, ana)
        yesterday = (date.today() - timedelta(days=3)).isoformat()
        self.create_job(client_id, None, yesterday)  # atrasado e sem funcionário
        urls = ["/painel", "/trabalhos", "/trabalhos?ver=hoje", "/trabalhos?ver=atrasados",
                "/trabalhos?ver=concluidos", "/trabalhos?ver=todos", f"/trabalhos?func={ana}", "/trabalhos?func=none",
                "/trabalhos?ver=invalido", "/trabalhos/novo", f"/trabalhos/novo?client={client_id}",
                f"/trabalhos/{job_id}", f"/trabalhos/{job_id}/editar", "/clientes/", "/clientes/?q=Rose",
                "/clientes/?arquivados=1", "/clientes/novo", f"/clientes/{client_id}",
                f"/clientes/{client_id}/editar", "/equipe/", "/equipe/novo", f"/equipe/{ana}/editar",
                "/conta", "/conta/senha", "/conta/idioma", "/conta/perfil", "/conta/empresa", "/conta/aparencia",
                "/conta/avisos", "/conta/lembretes", "/conta/fotos", "/conta/relatorio-pdf", "/lembretes",
                "/meus-trabalhos", "/minhas-horas",
                "/avisos/", "/relatorio/", "/financas/", "/rota-que-nao-existe"]
        for url in urls:
            expected = 404 if "nao-existe" in url else 200
            self.assertEqual(self.owner.get(url).status_code, expected, url)
        html = self.owner.get("/painel").get_data(as_text=True)
        self.assertIn("Late", html)  # inglês é o idioma padrão
        self.assertIn("No employee assigned", html)
        for url in ("/meus-trabalhos", f"/trabalhos/{job_id}", "/conta", "/conta/senha", "/conta/idioma", "/minhas-horas"):
            self.assertEqual(self.employee_browser().get(url).status_code, 200, url)

    def test_manifest_lets_phone_install_the_app(self):
        # Funciona mesmo antes do primeiro acesso (ninguém logado, banco vazio) e depois dele.
        for step in ("antes do setup", "depois do setup"):
            r = self.owner.get("/manifest.webmanifest")
            self.assertEqual(r.status_code, 200, step)
            self.assertEqual(r.mimetype, "application/manifest+json", step)
            data = r.get_json()
            self.assertEqual(data["display"], "standalone")
            self.assertEqual({i["sizes"] for i in data["icons"]}, {"192x192", "512x512"})
            for icon in data["icons"]:  # os arquivos dos ícones existem de verdade
                with self.owner.get(icon["src"]) as icon_response:
                    self.assertEqual(icon_response.status_code, 200, icon["src"])
            if step == "antes do setup":
                self.create_owner()
        self.assertIn('rel="manifest"', self.owner.get("/painel").get_data(as_text=True))

    def test_archive_client_hides_from_job_form(self):
        self.create_owner()
        client_id = self.create_client()
        self.owner.post(f"/clientes/{client_id}/arquivar")
        self.assertNotIn("Sítio das Flores", self.owner.get("/trabalhos/novo").get_data(as_text=True))
        self.assertIn("Sítio das Flores", self.owner.get("/clientes/?arquivados=1").get_data(as_text=True))

    # ---------- categoria e fotos do cliente ----------

    def _tiny_jpeg(self):
        import io

        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", (20, 20), color=(10, 120, 60)).save(buf, "JPEG")
        buf.seek(0)
        return buf

    def test_client_tier_saved_and_shown(self):
        self.create_owner()
        client_id = self.create_client()
        self.owner.post(f"/clientes/{client_id}/editar", {
            "name": "Sítio das Flores", "address": "12 Rose Lane", "postcode": "N1 1AA",
            "phone": "07123 456789", "access_notes": "", "notes": "", "tier": "ouro",
        })
        self.assertEqual(self.query("SELECT tier FROM clients WHERE id = ?", (client_id,))[0]["tier"], "ouro")
        html = self.owner.get(f"/clientes/{client_id}").get_data(as_text=True)
        self.assertIn("Gold", html)  # inglês é o idioma padrão
        self.assertIn("Active", html)

    def test_owner_can_upload_and_delete_photo(self):
        self.create_owner()
        client_id = self.create_client()
        r = self.owner.post(f"/clientes/{client_id}/fotos", {"photo": (self._tiny_jpeg(), "foto.jpg")})
        self.assertEqual(r.status_code, 302)
        rows = self.query("SELECT * FROM client_photos WHERE client_id = ?", (client_id,))
        self.assertEqual(len(rows), 1)
        filename, photo_id = rows[0]["filename"], rows[0]["id"]

        with self.owner.get(f"/clientes/{client_id}/fotos/{filename}") as img:
            self.assertEqual(img.status_code, 200)
            self.assertEqual(img.mimetype, "image/jpeg")

        self.owner.post(f"/clientes/{client_id}/fotos/{photo_id}/excluir")
        self.assertEqual(len(self.query("SELECT * FROM client_photos WHERE client_id = ?", (client_id,))), 0)
        self.assertEqual(self.owner.get(f"/clientes/{client_id}/fotos/{filename}").status_code, 404)

    def test_photo_limit_of_twelve(self):
        self.create_owner()
        client_id = self.create_client()
        for _ in range(12):
            self.assertEqual(
                self.owner.post(f"/clientes/{client_id}/fotos", {"photo": (self._tiny_jpeg(), "f.jpg")}).status_code,
                302)
        self.assertEqual(len(self.query("SELECT * FROM client_photos WHERE client_id = ?", (client_id,))), 12)

        self.owner.post(f"/clientes/{client_id}/fotos", {"photo": (self._tiny_jpeg(), "f13.jpg")})
        self.assertEqual(len(self.query("SELECT * FROM client_photos WHERE client_id = ?", (client_id,))), 12)
        html = self.owner.get(f"/clientes/{client_id}").get_data(as_text=True)
        self.assertIn("maximum of 12 photos", html)

    # ---------- trabalho Share: várias pessoas no mesmo trabalho ----------

    def share_setup(self):
        self.create_owner()
        client_id = self.create_client()
        ana = self.create_employee("Ana", "ana@example.com")
        bruno = self.create_employee("Bruno", "bruno@example.com", "senha-do-bruno-1")
        dan = self.create_employee("Dan", "dan@example.com", "senha-do-dan-1")
        return client_id, ana, bruno, dan

    def test_share_job_goes_to_everyone_ticked(self):
        client_id, ana, bruno, dan = self.share_setup()
        self.create_employee("Nikolly", "nik@example.com", "senha-da-nik-1")
        r = self.owner.post("/trabalhos/novo", {"client_id": client_id, "assigned_to": [ana, bruno, dan],
                                                "title": "Poda grande", "job_date": self.day(2), "start_time": "09:00"})
        self.assertEqual(r.status_code, 302)
        job_id = self.query("SELECT id FROM jobs ORDER BY id DESC LIMIT 1")[0]["id"]
        self.assertEqual(self.people(job_id), sorted([ana, bruno, dan]))
        for uid in (ana, bruno, dan):
            self.assertEqual([n["kind"] for n in self.notices(uid)], ["job_assigned"])

        # cada um vê na lista dele e abre o trabalho; quem não está nele, não
        for email, password in (("ana@example.com", "senha-da-ana-1"), ("bruno@example.com", "senha-do-bruno-1")):
            b = self.employee_browser(email, password)
            self.assertIn("Poda grande", b.get("/meus-trabalhos").get_data(as_text=True))
            page = b.get(f"/trabalhos/{job_id}").get_data(as_text=True)
            self.assertIn("Ana, Bruno, Dan", page)
            self.assertIn("<dt>Team</dt>", page)
        nik = self.employee_browser("nik@example.com", "senha-da-nik-1")
        self.assertEqual(nik.get(f"/trabalhos/{job_id}").status_code, 404)
        self.assertNotIn("Poda grande", nik.get("/meus-trabalhos").get_data(as_text=True))

        # na agenda do dono: os nomes e a etiqueta Share; o filtro por pessoa acha o trabalho
        agenda = self.owner.get("/trabalhos").get_data(as_text=True)
        self.assertIn("Ana, Bruno, Dan", agenda)
        self.assertIn('<span class="pill pill-share">Share</span>', agenda)
        self.assertIn("Poda grande", self.owner.get(f"/trabalhos?func={bruno}").get_data(as_text=True))
        self.assertNotIn("Poda grande", self.owner.get("/trabalhos?func=none").get_data(as_text=True))
        # pra editar, o formulário já vem com os três marcados
        form = self.owner.get(f"/trabalhos/{job_id}/editar").get_data(as_text=True)
        for uid in (ana, bruno, dan):
            self.assertIn(f'name="assigned_to" value="{uid}" checked', form)

    def test_changing_the_team_tells_who_joined_left_and_stayed(self):
        client_id, ana, bruno, dan = self.share_setup()
        job_id = self.create_job(client_id, [ana, bruno], self.day(3))
        self.edit_job(job_id, assigned_to=[bruno, dan], job_date=self.day(4))  # Ana sai, Dan entra, Bruno fica
        self.assertEqual(self.people(job_id), sorted([bruno, dan]))
        self.assertEqual([n["kind"] for n in self.notices(ana)], ["job_assigned", "job_unassigned"])
        self.assertEqual([n["kind"] for n in self.notices(dan)], ["job_assigned"])
        self.assertEqual([n["kind"] for n in self.notices(bruno)], ["job_assigned", "job_rescheduled"])
        self.assertIn("no longer on this job", self.employee_browser().get("/avisos/").get_data(as_text=True))
        self.assertEqual(self.employee_browser().get(f"/trabalhos/{job_id}").status_code, 404)  # a Ana saiu

        self.edit_job(job_id, assigned_to="")  # ninguém: fica para decidir depois
        self.assertEqual(self.people(job_id), [])
        self.assertEqual([n["kind"] for n in self.notices(dan)], ["job_assigned", "job_unassigned"])
        self.assertIn("No employee assigned", self.owner.get("/trabalhos").get_data(as_text=True))
        self.assertIn("Manutenção", self.owner.get("/trabalhos?func=none").get_data(as_text=True))

    def test_share_job_hours_count_for_each_person(self):
        from jardim import reports

        client_id, ana, bruno, dan = self.share_setup()
        today = utils_today(self.app)
        job_id = self.create_job(client_id, [ana, bruno], today)
        self.set_job(job_id, status="done", started_at=f"{today}T09:00:00+00:00", finished_at=f"{today}T11:00:00+00:00")
        for email, password in (("ana@example.com", "senha-da-ana-1"), ("bruno@example.com", "senha-do-bruno-1")):
            page = self.employee_browser(email, password).get("/meus-trabalhos").get_data(as_text=True)
            self.assertIn("Hours this week: 2h00", page)
        with self.app.test_request_context():
            data = reports.report_data("dia", today)
        self.assertEqual((data["done_count"], data["total_minutes"], data["has_share"]), (1, 240, True))
        self.assertEqual([(p["name"], p["done"], p["minutes"]) for p in data["people"]], [("Ana", 1, 120), ("Bruno", 1, 120)])
        self.assertIn("each job counts once", self.owner.get("/relatorio/?periodo=dia").get_data(as_text=True))
        with self.owner.get("/relatorio/pdf?periodo=dia") as pdf:
            self.assertEqual(pdf.mimetype, "application/pdf")

    def test_cash_stays_with_whoever_wrote_it_down(self):
        from jardim import reports

        client_id, ana, bruno, dan = self.share_setup()
        job_id = self.create_job(client_id, [ana, bruno], utils_today(self.app))
        holder = lambda: self.query("SELECT cash_by FROM jobs WHERE id = ?", (job_id,))[0]["cash_by"]
        bruno_b, ana_b = self.employee_browser("bruno@example.com", "senha-do-bruno-1"), self.employee_browser()
        bruno_b.post(f"/trabalhos/{job_id}/atualizar", {"cash": "60", "cash_in_report": "1", "action": "save"})
        self.assertEqual(holder(), bruno)
        ana_b.post(f"/trabalhos/{job_id}/atualizar", {"cash": "60", "cash_in_report": "1", "action": "save"})
        self.assertEqual(holder(), bruno)  # a Ana salvou sem mexer no valor: o dinheiro continua com o Bruno
        with self.app.test_request_context():
            self.assertEqual([(r["name"], r["pending"]) for r in reports.team_cash("2000-01-01", "2100-01-01")],
                             [("Bruno", 6000)])
            self.assertEqual([r["cash_name"] for r in reports.pending_cash()], ["Bruno"])
        ana_b.post(f"/trabalhos/{job_id}/atualizar", {"cash": "65", "cash_in_report": "1", "action": "save"})
        self.assertEqual(holder(), ana)  # mudou o valor: quem anotou por último está com o dinheiro
        ana_b.post(f"/trabalhos/{job_id}/atualizar", {"cash": "", "cash_in_report": "1", "action": "save"})
        self.assertIsNone(holder())

    def test_share_job_reminders_repeats_and_deletion_reach_everyone(self):
        client_id, ana, bruno, dan = self.share_setup()
        job_id = self.create_job(client_id, [ana, bruno], self.day(1))
        self.backdate_notices(days=3)
        self.employee_browser().get("/meus-trabalhos")  # a primeira visita do dia cria os lembretes
        for uid in (ana, bruno):
            self.assertEqual([n["job_id"] for n in self.notices(uid, "job_tomorrow")], [job_id])
        self.owner.post(f"/trabalhos/{job_id}/repetir", {"every": "1", "times": "2"})
        copies = self.query("SELECT id FROM jobs WHERE id != ? ORDER BY id", (job_id,))
        self.assertEqual([self.people(c["id"]) for c in copies], [sorted([ana, bruno])] * 2)
        for uid in (ana, bruno):
            self.assertEqual(len(self.notices(uid, "jobs_repeated")), 1)
        self.owner.post(f"/trabalhos/{job_id}/excluir")
        for uid in (ana, bruno):
            self.assertEqual(len(self.notices(uid, "job_deleted")), 1)
        self.assertEqual(self.people(job_id), [])

    def test_team_rules_with_share_jobs(self):
        client_id, ana, bruno, dan = self.share_setup()
        job_id = self.create_job(client_id, [ana, bruno], self.day(2))
        self.owner.post(f"/equipe/{bruno}/excluir")
        self.assertEqual(len(self.query("SELECT 1 FROM users WHERE id = ?", (bruno,))), 1)  # está num trabalho
        # o Dan anotou o dinheiro de um trabalho e depois saiu dele: continua sem poder excluir
        self.edit_job(job_id, assigned_to=[ana, bruno, dan])
        self.employee_browser("dan@example.com", "senha-do-dan-1").post(
            f"/trabalhos/{job_id}/atualizar", {"cash": "40", "cash_in_report": "1", "action": "save"})
        self.edit_job(job_id, assigned_to=[ana, bruno])
        self.owner.post(f"/equipe/{dan}/excluir")
        self.assertEqual(len(self.query("SELECT 1 FROM users WHERE id = ?", (dan,))), 1)
        free = self.create_employee("Zé", "ze@example.com", "senha-do-ze-1")
        self.owner.post(f"/equipe/{free}/excluir")
        self.assertEqual(self.query("SELECT 1 FROM users WHERE id = ?", (free,)), [])

        # desativado continua no trabalho quando alguém edita (não some sem querer)
        r = self.owner.post(f"/equipe/{bruno}/editar", {"name": "Bruno", "email": "bruno@example.com", "phone": "",
                                                        "role": "employee", "password": ""}, follow_redirects=True)
        self.assertIn("still has 1 open job", r.get_data(as_text=True))
        form = self.owner.get(f"/trabalhos/{job_id}/editar").get_data(as_text=True)
        self.assertIn(f'name="assigned_to" value="{bruno}" checked', form)
        self.assertIn("Bruno (deactivated)", form)
        self.edit_job(job_id)  # salvar sem mexer mantém o Bruno
        self.assertEqual(self.people(job_id), sorted([ana, bruno]))
        self.assertNotIn("Bruno", self.owner.get("/trabalhos/novo").get_data(as_text=True))

    # ---------- aparência: nome, logo e cores ----------

    def save_look(self, browser=None, **fields):
        data = {"app_name": "", "app_short_name": "", "show_name": "1", "theme": "floresta",
                "color_main": "#12301f", "color_accent": "#f4c430"}
        data.update(fields)
        return (browser or self.owner).post("/conta/aparencia", data, follow_redirects=True)

    def _logo(self, color=(20, 20, 20, 255)):
        import io

        from PIL import Image, ImageDraw

        img = Image.new("RGBA", (400, 300), (0, 0, 0, 0))
        ImageDraw.Draw(img).ellipse((100, 50, 300, 250), fill=color)  # sobra transparente em volta
        buf = io.BytesIO()
        img.save(buf, "PNG")
        buf.seek(0)
        return buf

    def test_name_and_ready_made_palette(self):
        self.create_owner()
        page = self.save_look(app_name="Renan Gardening", theme="oceano").get_data(as_text=True)
        self.assertIn("Appearance saved.", page)
        page = self.owner.get("/painel").get_data(as_text=True)
        self.assertIn("<span>Renan Gardening</span>", page)
        self.assertIn("Dashboard – Renan Gardening</title>", page)
        self.assertIn("--mast-bg: #0f3550;", page)  # as cores da paleta Oceano entram na página
        self.assertIn('<meta name="theme-color" media="(prefers-color-scheme: light)" content="#0f3550">', page)
        manifest = self.owner.get("/manifest.webmanifest").get_json()
        self.assertEqual((manifest["name"], manifest["short_name"], manifest["theme_color"]),
                         ("Renan Gardening", "Renan", "#0f3550"))
        # em branco: usa o nome da empresa; voltar ao original tira as cores
        self.save_look(theme="oceano")
        self.owner.post("/conta/empresa", {"company_name": "Jardins do Renan"})
        self.assertIn("<span>Jardins do Renan</span>", self.owner.get("/painel").get_data(as_text=True))
        self.owner.post("/conta/aparencia", {"reset": "1"})
        page = self.owner.get("/painel").get_data(as_text=True)
        self.assertNotIn("--mast-bg:", page)
        self.assertEqual(self.query("SELECT theme FROM settings")[0]["theme"], "floresta")

    def test_custom_colours_always_stay_readable(self):
        from jardim import branding

        c = branding.contrast
        mains = ("#12301f", "#0f3550", "#ffffff", "#f4c430", "#8a8a8a", "#767676", "#ff0000", "#000000",
                 "#7fdcc0", "#3b2f5c", "#3498db", "#00a650")
        for main in mains:
            for accent in ("#f4c430", "#ffffff", "#000000", "#808080", "#767676", "#2f6b86", "#ff5a5f", "#12301f"):
                light, dark = branding.palette(main, accent)
                self.assertGreaterEqual(c(light["--ink"], light["--bg"]), 7, (main, accent))
                self.assertGreaterEqual(c(light["--muted"], light["--bg"]), 4.5, (main, accent))
                self.assertGreaterEqual(c(light["--link"], "#ffffff"), 4.5, (main, accent))
                self.assertGreaterEqual(c(light["--link"], light["--bg"]), 4.5, (main, accent))
                self.assertGreaterEqual(c(light["--on-marker"], accent), 4.5, (main, accent))
                self.assertGreaterEqual(c(light["--on-marker"], light["--marker-hover"]), 4.5, (main, accent))
                self.assertGreaterEqual(c(light["--mast-ink"], main), 4.5, (main, accent))  # até no cinza médio
                self.assertGreaterEqual(c(light["--mast-muted"], main), 4.5, (main, accent))
                self.assertGreaterEqual(c(dark["--ink"], dark["--surface"]), 7, (main, accent))
                self.assertGreaterEqual(c(dark["--muted"], dark["--surface"]), 4.5, (main, accent))
                self.assertGreaterEqual(c(dark["--link"], dark["--surface"]), 4.5, (main, accent))
                self.assertGreaterEqual(c(dark["--mast-ink"], dark["--mast-bg"]), 7, (main, accent))
                self.assertGreaterEqual(c(dark["--mast-muted"], dark["--mast-bg"]), 4.5, (main, accent))
        # as paletas prontas nunca dão aviso de pouco contraste; o topo delas fica bem legível
        for key, (main, accent) in branding.PRESETS.items():
            self.assertFalse(branding.header_hard_to_read(main), key)
            if key != branding.DEFAULT_THEME:
                self.assertGreaterEqual(c(branding.palette(main, accent)[0]["--mast-ink"], main), 7, key)
        self.create_owner()
        self.save_look(theme="custom", color_main="#6a2e1c", color_accent="#7fdcc0")
        row = self.query("SELECT theme, color_main, color_accent FROM settings")[0]
        self.assertEqual(row, {"theme": "custom", "color_main": "#6a2e1c", "color_accent": "#7fdcc0"})
        self.assertIn("--marker: #7fdcc0;", self.owner.get("/painel").get_data(as_text=True))
        colors = self.owner.get("/conta/aparencia/cores?main=%236a2e1c&accent=%237fdcc0").get_json()
        self.assertEqual(colors["light"]["--mast-bg"], "#6a2e1c")
        page = self.save_look(theme="custom", color_main="vermelho", color_accent="#7fdcc0").get_data(as_text=True)
        self.assertIn("Pick valid colours", page)
        self.assertEqual(self.query("SELECT color_main FROM settings")[0]["color_main"], "#6a2e1c")  # nada mudou
        self.assertFalse(colors["low"])
        self.assertIn('id="look-warn" role="status" hidden', self.owner.get("/conta/aparencia").get_data(as_text=True))
        page = self.save_look(theme="custom", color_main="#767676", color_accent="#f4c430").get_data(as_text=True)
        warning = '<div class="flash flash-info" role="status">Heads-up'
        self.assertIn(warning, page)  # cinza médio: nem texto claro nem escuro ficam bem legíveis
        self.assertNotIn('id="look-warn" role="status" hidden', page)  # e o aviso fica junto das cores
        self.assertTrue(self.owner.get("/conta/aparencia/cores?main=%23767676&accent=%23f4c430").get_json()["low"])
        page = self.save_look(theme="custom", color_main="#8a8a8a", color_accent="#f4c430").get_data(as_text=True)
        self.assertNotIn(warning, page)  # um pouco mais claro já dá texto escuro bem legível
        self.assertIn('id="look-warn" role="status" hidden', page)

    def test_logo_becomes_header_icon_and_quote_logo(self):
        from PIL import Image

        from jardim import branding

        self.create_owner()
        self.save_look(app_name="Renan Gardening", logo=(self._logo(), "logo.png"))
        row = self.query("SELECT logo_version, logo_meta FROM settings")[0]
        v = row["logo_version"]
        self.assertRegex(v, r"^[0-9a-f]{8}$")
        folder = Path(self.app.config["UPLOAD_ROOT"]) / "brand"
        with Image.open(folder / "logo-original.png") as original:
            self.assertEqual(original.size, (201, 201))  # a sobra transparente foi cortada
        page = self.owner.get("/painel").get_data(as_text=True)
        self.assertIn(f'src="/marca/logo-{v}.png"', page)
        self.assertIn("chip-l-light", page)  # logo escuro no topo escuro: ganha fundinho claro
        self.assertIn(f'<link rel="apple-touch-icon" href="/marca/apple-{v}.png">', page)
        self.assertIn(f'href="/marca/favicon-{v}.png"', page)
        self.assertEqual(self.owner.get("/manifest.webmanifest").get_json()["icons"][0]["src"], f"/marca/icon-192-{v}.png")
        visitor = Browser(self.app)  # sem login (tela de login, página da cotação)
        with visitor.get(f"/marca/icon-192-{v}.png") as icon:
            self.assertEqual((icon.status_code, icon.mimetype), (200, "image/png"))
            with Image.open(__import__("io").BytesIO(icon.data)) as im:
                self.assertEqual(im.size, (192, 192))
                self.assertEqual(im.getpixel((2, 2))[:3], (255, 255, 255))  # fundo branco: o logo é escuro
        for bad in ("logo-original.png", "..%2Fjardim.db", f"logo-{v}.jpg"):
            self.assertEqual(visitor.get(f"/marca/{bad}").status_code, 404)

        q = self.new_quote()
        self.assertIn(f'src="/marca/logo-{v}.png"', self.visitor().get(f"/c/{q['token']}").get_data(as_text=True))

        # logo escuro: o ícone tem fundo branco em qualquer paleta, então trocar a paleta não mexe nele
        self.save_look(app_name="Renan Gardening", theme="menta", show_name="")
        self.assertEqual(self.query("SELECT logo_version FROM settings")[0]["logo_version"], v)
        page = self.owner.get("/painel").get_data(as_text=True)
        self.assertIn('alt="Renan Gardening"', page)  # sem o nome ao lado: o logo diz o nome
        self.assertNotIn("<span>Renan Gardening</span>", page)

        # logo claro: o fundo do ícone é a cor do topo; logo novo = ícones novos, e os velhos são apagados
        self.save_look(app_name="Renan Gardening", theme="menta", show_name="", logo=(self._logo((245, 240, 225, 255)), "l.png"))
        v2 = self.query("SELECT logo_version FROM settings")[0]["logo_version"]
        self.assertNotEqual(v2, v)
        self.assertFalse((folder / f"logo-{v}.png").exists())
        self.assertNotIn("chip-", self.owner.get("/painel").get_data(as_text=True))
        with Image.open(folder / f"icon-192-{v2}.png") as im:
            self.assertEqual("#%02x%02x%02x" % im.getpixel((2, 2))[:3], branding.PRESETS["menta"][0])
        # trocar a paleta gera os ícones de novo (o fundo acompanha o topo)
        self.save_look(app_name="Renan Gardening", theme="oceano", show_name="")
        v3 = self.query("SELECT logo_version FROM settings")[0]["logo_version"]
        self.assertNotEqual(v3, v2)
        with Image.open(folder / f"icon-192-{v3}.png") as im:
            self.assertEqual("#%02x%02x%02x" % im.getpixel((2, 2))[:3], branding.PRESETS["oceano"][0])
        # mudar só o nome não mexe nos ícones (o celular não precisa baixar nada de novo)
        self.save_look(app_name="Renan Garden Care", theme="oceano", show_name="")
        self.assertEqual(self.query("SELECT logo_version FROM settings")[0]["logo_version"], v3)
        self.assertTrue((folder / f"icon-192-{v3}.png").exists())

        page = self.save_look(logo=(__import__("io").BytesIO(b"isto nao e imagem"), "x.png")).get_data(as_text=True)
        self.assertIn("couldn&#39;t open that file as an image", page)
        self.save_look(remove_logo="1")
        self.assertEqual(self.query("SELECT logo_version FROM settings")[0]["logo_version"], "")
        self.assertEqual(list(folder.glob("*.png")), [])
        self.assertIn('<path d="M12 2C7 6', self.owner.get("/painel").get_data(as_text=True))  # a folhinha voltou

    def test_only_the_owner_changes_the_appearance(self):
        self.create_owner()
        self.create_manager(perms=("schedule", "clients", "quotes", "report", "cash", "finance"))
        gil = self.employee_browser("gil@example.com", "senha-do-gil-1")
        self.assertEqual(gil.post("/conta/aparencia", {"app_name": "Hacked"}).status_code, 403)
        self.assertEqual(gil.get("/conta/aparencia").status_code, 403)
        self.assertEqual(gil.get("/conta/aparencia/cores?main=%23000000&accent=%23ffffff").status_code, 403)
        self.enable_email()
        self.save_look(app_name="Renan Gardening")
        self.owner.post("/conta/avisos/teste")
        self.assertIn("Renan Gardening", FakeSMTP.sent[-1]["Subject"])  # e-mails usam o nome escolhido

    # ---------- cotações ----------

    def new_quote(self, browser=None, **changes):
        data = {"client_id": "", "to_name": "John Smith", "to_phone": "07700 900111", "to_email": "john@example.com",
                "to_address": "5 Oak Road", "to_postcode": "SW4 7EP", "title": "Hedge trimming and clear-up",
                "intro": "Thanks for having me round.", "notes": "Payment by bank transfer.",
                "frequency": "once", "valid_until": self.day(30), "language": "en",
                "item_desc": ["Trim front hedge", "Green waste bags", "Leaf clear-up"],
                "item_qty": ["1", "3", "1"], "item_price": ["120", "15", ""]}
        data.update(changes)
        r = (browser or self.owner).post("/cotacoes/nova", data)
        self.assertEqual(r.status_code, 302, r.get_data(as_text=True)[-3000:])
        return self.query("SELECT * FROM quotes ORDER BY id DESC LIMIT 1")[0]

    def visitor(self, agent="Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)"):
        """Alguém sem login (o cliente), com o navegador do celular."""
        b = Browser(self.app)
        b.c.environ_base["HTTP_USER_AGENT"] = agent
        return b

    def test_quote_is_created_with_items_and_total(self):
        self.create_owner()
        q = self.new_quote()
        self.assertEqual((q["number"], q["status"], q["to_name"]), (1, "open", "John Smith"))
        self.assertGreaterEqual(len(q["token"]), 20)
        items = self.query("SELECT description, quantity, unit_pence FROM quote_items WHERE quote_id = ? ORDER BY position", (q["id"],))
        self.assertEqual([(i["description"], i["quantity"], i["unit_pence"]) for i in items],
                         [("Trim front hedge", 1, 12000), ("Green waste bags", 3, 1500), ("Leaf clear-up", 1, 0)])
        page = self.owner.get(f"/cotacoes/{q['id']}").get_data(as_text=True)
        for text in ("Q-0001", "£165", "Not sent", "Included", "3 × £15", f"/c/{q['token']}", "Send by WhatsApp"):
            self.assertIn(text, page)
        listing = self.owner.get("/cotacoes/").get_data(as_text=True)
        self.assertIn("John Smith", listing)
        self.assertIn("£165", listing)
        self.assertEqual(self.new_quote(to_name="Mary")["number"], 2)
        self.assertIn(">Quotes<", self.owner.get("/painel").get_data(as_text=True))  # a aba no menu

        # editar: o formulário vem com os itens; mudar o preço muda o total
        form = self.owner.get(f"/cotacoes/{q['id']}/editar").get_data(as_text=True)
        self.assertIn('value="Trim front hedge"', form)
        self.assertIn('name="item_price" value="120"', form)
        self.owner.post(f"/cotacoes/{q['id']}/editar", {
            "to_name": "John Smith", "title": "Hedge trimming", "valid_until": self.day(30), "language": "en",
            "frequency": "monthly", "item_desc": ["Trim front hedge", "Green waste bags"], "item_qty": ["1", "2,5"],
            "item_price": ["130,50", "10"]})
        page = self.owner.get(f"/cotacoes/{q['id']}").get_data(as_text=True)
        self.assertIn("£155.50 per visit, every month", page)  # 130.50 + 2.5 x 10
        self.assertIn("2.5 × £10", page)

    def test_quote_form_checks_what_matters(self):
        self.create_owner()
        r = self.owner.post("/cotacoes/nova", {"to_name": "", "title": "", "valid_until": "amanhã",
                                               "item_desc": ["", "Poda"], "item_qty": ["1", "x"], "item_price": ["50", "abc"]})
        page = r.get_data(as_text=True)
        self.assertEqual(r.status_code, 200)
        for text in ("Fill in who the quote is for.", "Describe what will be done.", "Pick a valid date",
                     "Line 1 has a price but no description.", "Check the quantity on line 2",
                     "Check the price on line 2"):
            self.assertIn(html.escape(text, quote=True), page)
        self.assertEqual(self.query("SELECT * FROM quotes"), [])
        self.assertIn("Poda", page)  # o que foi digitado continua no formulário

    def test_client_opens_the_link_without_login(self):
        self.create_owner()
        client_id = self.create_client()  # tem anotação interna e código do portão
        q = self.new_quote(client_id=str(client_id), to_name="", to_address="", to_phone="", to_email="")
        self.assertEqual(q["to_name"], "Sítio das Flores")  # campos em branco vêm do cadastro
        client = self.visitor()
        r = client.get(f"/c/{q['token']}")
        page = r.get_data(as_text=True)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["X-Robots-Tag"], "noindex, nofollow")
        for text in ("Hedge trimming and clear-up", "Prepared for Sítio das Flores", "£165", "Accept quote",
                     "Payment by bank transfer.", "Thanks for having me round."):
            self.assertIn(html.escape(text, quote=True), page)
        for private in ("Só o dono deve ler isto", "Portão azul", "código 1234", "Log out"):
            self.assertNotIn(private, page)
        owner_id = self.query("SELECT id FROM users WHERE role = 'owner'")[0]["id"]
        self.assertIsNotNone(self.query("SELECT viewed_at FROM quotes WHERE id = ?", (q["id"],))[0]["viewed_at"])
        self.assertEqual([n["kind"] for n in self.notices(owner_id)], ["quote_viewed"])
        self.assertIn("Sítio das Flores opened quote Q-0001", self.owner.get("/avisos/").get_data(as_text=True))
        self.assertEqual(client.get("/c/" + "x" * 24).status_code, 404)
        self.assertEqual(client.get("/c/abc").status_code, 404)

    def test_link_previews_and_the_team_do_not_count_as_opened(self):
        self.create_owner()
        q = self.new_quote()
        for agent in ("WhatsApp/2.23.20.0 A", "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)",
                      "TelegramBot (like TwitterBot)"):
            self.assertEqual(self.visitor(agent).get(f"/c/{q['token']}").status_code, 200)
        self.owner.get(f"/c/{q['token']}")  # o dono conferindo o link
        viewed = lambda: self.query("SELECT viewed_at FROM quotes WHERE id = ?", (q["id"],))[0]["viewed_at"]
        self.assertIsNone(viewed())
        client = self.visitor()
        client.get(f"/c/{q['token']}")
        client.get(f"/c/{q['token']}")
        self.assertIsNotNone(viewed())
        self.assertEqual(len(self.query("SELECT * FROM notifications WHERE kind = 'quote_viewed'")), 1)  # um aviso só
        self.assertIn("Opened", self.owner.get(f"/cotacoes/{q['id']}").get_data(as_text=True))

    def test_client_accepts_the_quote(self):
        self.create_owner()
        q = self.new_quote()
        client = self.visitor()
        url = f"/c/{q['token']}/resposta"
        page = client.post(url, {"answer": "accept", "name": ""}, follow_redirects=True).get_data(as_text=True)
        self.assertIn("Type your name to accept.", page)
        client.post(url, {"answer": "accept", "name": "John Smith", "note": "Mornings please"})
        row = self.query("SELECT * FROM quotes WHERE id = ?", (q["id"],))[0]
        self.assertEqual((row["status"], row["answer_name"], row["answer_note"]), ("accepted", "John Smith", "Mornings please"))
        owner_id = self.query("SELECT id FROM users WHERE role = 'owner'")[0]["id"]
        self.assertEqual([n["kind"] for n in self.notices(owner_id, "quote_accepted")], ["quote_accepted"])
        page = client.get(f"/c/{q['token']}").get_data(as_text=True)
        self.assertIn("Accepted by John Smith", page)
        self.assertNotIn('value="accept"', page)  # sem formulário depois de respondida
        client.post(url, {"answer": "decline", "note": "mudei de ideia"})
        self.assertEqual(self.query("SELECT status FROM quotes WHERE id = ?", (q["id"],))[0]["status"], "accepted")
        detail = self.owner.get(f"/cotacoes/{q['id']}").get_data(as_text=True)
        self.assertIn("Accepted by John Smith", detail)
        self.assertIn("Schedule the job", detail)

    def test_declined_quote_can_be_reopened(self):
        self.create_owner()
        q = self.new_quote()
        client = self.visitor()
        client.post(f"/c/{q['token']}/resposta", {"answer": "decline", "note": "Too expensive"})
        self.assertEqual(self.query("SELECT status, answer_note FROM quotes")[0], {"status": "declined", "answer_note": "Too expensive"})
        self.assertIn("Too expensive", self.owner.get("/avisos/").get_data(as_text=True))
        self.owner.post(f"/cotacoes/{q['id']}/reabrir")
        self.assertEqual(self.query("SELECT status, answer_note FROM quotes")[0], {"status": "open", "answer_note": ""})
        client.post(f"/c/{q['token']}/resposta", {"answer": "accept", "name": "John"})
        self.assertEqual(self.query("SELECT status FROM quotes")[0]["status"], "accepted")

    def test_expired_or_cancelled_quote_cannot_be_answered(self):
        self.create_owner()
        q = self.new_quote()
        with self.app.app_context():
            db = get_db()
            db.execute("UPDATE quotes SET valid_until = ? WHERE id = ?", (self.day(-1), q["id"]))
            db.commit()
        client = self.visitor()
        self.assertIn("This quote expired on", client.get(f"/c/{q['token']}").get_data(as_text=True))
        client.post(f"/c/{q['token']}/resposta", {"answer": "accept", "name": "John"})
        self.assertEqual(self.query("SELECT status FROM quotes")[0]["status"], "open")
        self.assertIn("Expired", self.owner.get("/cotacoes/").get_data(as_text=True))

        q2 = self.new_quote(to_name="Mary")
        self.owner.post(f"/cotacoes/{q2['id']}/cancelar")
        page = client.get(f"/c/{q2['token']}").get_data(as_text=True)
        self.assertIn("Quote no longer available", page)
        self.assertNotIn("£165", page)
        client.post(f"/c/{q2['token']}/resposta", {"answer": "accept", "name": "Mary"})
        self.assertEqual(self.query("SELECT status FROM quotes WHERE id = ?", (q2["id"],))[0]["status"], "cancelled")
        r = self.owner.post(f"/cotacoes/{q2['id']}/whatsapp", follow_redirects=True)
        self.assertIn("can&#39;t be edited or sent", r.get_data(as_text=True))
        self.assertEqual(self.owner.get(f"/cotacoes/{q2['id']}/editar").status_code, 302)

    def test_sending_by_whatsapp_and_email(self):
        from urllib.parse import unquote

        self.create_owner()
        q = self.new_quote()
        r = self.owner.post(f"/cotacoes/{q['id']}/whatsapp")
        location = r.headers["Location"]
        self.assertTrue(location.startswith("https://wa.me/447700900111?text="), location)
        message = unquote(location.split("text=", 1)[1])
        self.assertIn("Hi John, here is your quote", message)
        self.assertIn("Hedge trimming and clear-up – £165", message)
        self.assertIn(f"/c/{q['token']}", message)
        self.assertIsNotNone(self.query("SELECT sent_at FROM quotes")[0]["sent_at"])

        r = self.owner.post(f"/cotacoes/{q['id']}/email", follow_redirects=True)
        self.assertIn("set up the Gmail in Account → Email notices", r.get_data(as_text=True))
        self.assertIn('<a href="/conta/avisos">Set it up now</a>', r.get_data(as_text=True))  # o dono vai direto pra lá
        self.enable_email()
        r = self.owner.post(f"/cotacoes/{q['id']}/email", follow_redirects=True)
        self.assertIn("Quote sent to john@example.com.", r.get_data(as_text=True))
        mail = FakeSMTP.sent[-1]
        self.assertEqual(mail["To"], "john@example.com")
        self.assertTrue(mail["Subject"].startswith("Quote Q-0001 from "))
        self.assertIn(f"/c/{q['token']}", mail.get_content())

        pt = self.new_quote(to_name="Maria Souza", to_phone="+44 7700 900222", language="pt_BR", frequency="fortnightly")
        location = self.owner.post(f"/cotacoes/{pt['id']}/whatsapp").headers["Location"]
        self.assertTrue(location.startswith("https://wa.me/447700900222?text="), location)
        self.assertIn("Olá Maria, segue a cotação", unquote(location))
        self.assertIn("£165 por visita, a cada 15 dias", unquote(location))

    def test_quote_photos_with_text_underneath(self):
        self.create_owner()
        q = self.new_quote()
        r = self.owner.post(f"/cotacoes/{q['id']}/fotos", {"photo": [(self._photo(), "a.jpg"), (self._photo(), "b.jpg")]},
                            headers=self.JSON)
        first, second = r.get_json()["photos"]
        self.assertTrue(first["caption"].endswith("/texto"))
        r = self.owner.post(first["caption"], {"caption": "Back hedge: take it down 50 cm"}, headers=self.JSON)
        self.assertEqual(r.get_json(), {"ok": True, "message": "Saved ✓"})
        self.owner.post(second["caption"], {"caption": "Clear the leaves by the shed"})  # sem o script: formulário normal
        captions = [p["caption"] for p in self.query("SELECT caption FROM quote_photos ORDER BY id")]
        self.assertEqual(captions, ["Back hedge: take it down 50 cm", "Clear the leaves by the shed"])
        detail = self.owner.get(f"/cotacoes/{q['id']}").get_data(as_text=True)
        self.assertIn("Back hedge: take it down 50 cm</textarea>", detail)

        client = self.visitor()
        page = client.get(f"/c/{q['token']}").get_data(as_text=True)
        self.assertIn("<figcaption class=\"pre\">Back hedge: take it down 50 cm</figcaption>", page)
        name = self.query("SELECT filename FROM quote_photos ORDER BY id")[0]["filename"]
        with client.get(f"/c/{q['token']}/fotos/{name}") as img:
            self.assertEqual((img.status_code, img.mimetype), (200, "image/jpeg"))
        self.assertEqual(client.get(f"/c/{'y' * 24}/fotos/{name}").status_code, 404)
        self.assertEqual(client.get(f"/cotacoes/{q['id']}/fotos/{name}").status_code, 302)  # tela da equipe: pede login
        self.assertIn('og:image', page)

        folder = Path(self.app.config["UPLOAD_ROOT"]) / "quotes" / str(q["id"])
        self.owner.post(first["delete"], headers=self.JSON)
        self.assertEqual(len(self.query("SELECT * FROM quote_photos")), 1)
        self.owner.post(f"/cotacoes/{q['id']}/excluir")
        self.assertEqual(self.query("SELECT * FROM quotes"), [])
        self.assertEqual(self.query("SELECT * FROM quote_photos"), [])
        self.assertFalse(folder.exists())

    def test_accepted_quote_becomes_a_job(self):
        self.create_owner()
        ana = self.create_employee()
        q = self.new_quote()
        r = self.owner.post(f"/cotacoes/{q['id']}/fotos", {"photo": (self._photo(), "a.jpg")}, headers=self.JSON)
        self.owner.post(r.get_json()["photos"][0]["caption"], {"caption": "Take the ivy off the back wall"})
        self.assertEqual(self.owner.post(f"/cotacoes/{q['id']}/agendar").status_code, 302)  # ainda não aceita: volta
        self.assertEqual(self.query("SELECT COUNT(*) AS n FROM clients")[0]["n"], 0)
        self.visitor().post(f"/c/{q['token']}/resposta", {"answer": "accept", "name": "John Smith"})

        r = self.owner.post(f"/cotacoes/{q['id']}/agendar")
        client = self.query("SELECT * FROM clients")[0]
        self.assertEqual((client["name"], client["phone"], client["email"], client["postcode"]),
                         ("John Smith", "07700 900111", "john@example.com", "SW4 7EP"))
        self.assertTrue(r.headers["Location"].endswith(f"/trabalhos/novo?client={client['id']}&cotacao={q['id']}"))
        form = self.owner.get(r.headers["Location"]).get_data(as_text=True)
        self.assertIn('value="Hedge trimming and clear-up"', form)
        self.assertIn("Trim front hedge\nGreen waste bags\nLeaf clear-up", form)
        self.assertIn("From quote Q-0001:\n- Take the ivy off the back wall", form)
        self.assertIn(f'name="quote_id" value="{q["id"]}"', form)

        self.owner.post("/trabalhos/novo", {"client_id": client["id"], "assigned_to": ana, "title": "Hedge trimming",
                                            "job_date": self.day(3), "tasks": "Trim front hedge", "quote_id": q["id"]})
        job_id = self.query("SELECT id FROM jobs ORDER BY id DESC LIMIT 1")[0]["id"]
        self.assertEqual(self.query("SELECT job_id FROM quotes")[0]["job_id"], job_id)
        self.assertIn(f'href="/trabalhos/{job_id}"', self.owner.get(f"/cotacoes/{q['id']}").get_data(as_text=True))
        # na tela do cliente cadastrado, a cotação aparece
        self.assertIn("Q-0001", self.owner.get(f"/clientes/{client['id']}").get_data(as_text=True))

    def test_who_can_use_quotes(self):
        self.create_owner()
        self.create_employee()
        ana = self.employee_browser()
        self.assertEqual(ana.get("/cotacoes/").status_code, 403)
        self.assertEqual(ana.post("/cotacoes/nova", {"to_name": "X"}).status_code, 403)
        self.assertNotIn(">Quotes<", ana.get("/meus-trabalhos").get_data(as_text=True))
        self.create_manager(perms=("quotes",))
        gil = self.employee_browser("gil@example.com", "senha-do-gil-1")
        self.assertEqual(gil.get("/cotacoes/").status_code, 200)
        q = self.new_quote(browser=gil)
        self.visitor().post(f"/c/{q['token']}/resposta", {"answer": "accept", "name": "John"})
        gil_id = self.query("SELECT id FROM users WHERE email = 'gil@example.com'")[0]["id"]
        self.assertEqual([n["kind"] for n in self.notices(gil_id, "quote_accepted")], ["quote_accepted"])  # quem fez é avisado
        self.assertEqual(gil.post(f"/cotacoes/{q['id']}/agendar").status_code, 403)  # sem a agenda, não agenda

    # ---------- fotos de Antes e Depois de cada trabalho ----------

    JSON = {"Accept": "application/json"}

    def _photo(self, size=(20, 20), exif=None):
        import io

        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", size, color=(10, 120, 60)).save(buf, "JPEG", **({"exif": exif} if exif else {}))
        buf.seek(0)
        return buf

    def job_folder(self, job_id):
        return Path(self.app.config["UPLOAD_ROOT"]) / "jobs" / str(job_id)

    def job_photos(self, job_id):
        return self.query("SELECT * FROM job_photos WHERE job_id = ? ORDER BY id", (job_id,))

    def test_before_and_after_photos_several_at_once(self):
        self.create_owner()
        ana_id = self.create_employee()
        job_id = self.create_job(self.create_client(), ana_id)
        ana = self.employee_browser()

        page = ana.post(f"/trabalhos/{job_id}/fotos/antes", {"photo": [(self._photo(), "a1.jpg"), (self._photo(), "a2.jpg")]},
                        follow_redirects=True).get_data(as_text=True)
        self.assertIn("2 photos added.", page)
        page = ana.post(f"/trabalhos/{job_id}/fotos/depois", {"photo": [(self._photo(), "d1.jpg")]},
                        follow_redirects=True).get_data(as_text=True)
        self.assertIn("Photo added.", page)

        rows = self.job_photos(job_id)
        self.assertEqual([r["phase"] for r in rows], ["before", "before", "after"])
        self.assertTrue(all(r["uploaded_by"] == ana_id for r in rows))
        for row in rows:  # a foto grande e a miniatura da grade
            self.assertTrue((self.job_folder(job_id) / row["filename"]).is_file())
            self.assertTrue((self.job_folder(job_id) / (Path(row["filename"]).stem + "_mini.jpg")).is_file())

        # a tela: duas seções, com os dois botões (câmera e galeria com várias de uma vez)
        self.assertIn(">Before<", page)
        self.assertIn(">After<", page)
        self.assertIn("2 of 12", page)
        self.assertIn("1 of 12", page)
        self.assertEqual(page.count('capture="environment"'), 2)
        self.assertEqual(page.count('accept="image/*" multiple'), 2)
        self.assertIn("Take photo", page)
        self.assertIn("From gallery", page)

        # arquivos: grande e miniatura, guardados só no navegador da pessoa
        name = rows[0]["filename"]
        with ana.get(f"/trabalhos/{job_id}/fotos/{name}") as full, ana.get(f"/trabalhos/{job_id}/fotos/{name}?mini=1") as mini:
            self.assertEqual((full.status_code, mini.status_code), (200, 200))
            self.assertEqual(full.mimetype, "image/jpeg")
            self.assertIn("private", full.headers["Cache-Control"])
            self.assertNotIn("public", full.headers["Cache-Control"])

    def test_phone_script_gets_json_and_no_leftover_message(self):
        """O photos.js manda uma foto por vez e recebe JSON; a página não recarrega."""
        self.create_owner()
        self.create_employee()
        job_id = self.create_job(self.create_client(), 2)
        ana = self.employee_browser()
        r = ana.post(f"/trabalhos/{job_id}/fotos/antes", {"photo": (self._photo(), "x.jpg")}, headers=self.JSON)
        data = r.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["message"], "Photo added.")
        self.assertEqual((data["over_limit"], data["bad"], data["stop"]), (0, 0, False))
        photo = data["photos"][0]
        self.assertTrue(photo["mini"].endswith("mini=1"))
        with ana.get(photo["mini"]) as img:
            self.assertEqual(img.status_code, 200)

        r = ana.post(photo["delete"], headers=self.JSON)
        self.assertEqual(r.get_json(), {"ok": True, "message": "Photo deleted."})
        self.assertEqual(self.job_photos(job_id), [])
        self.assertEqual(list(self.job_folder(job_id).iterdir()), [])  # a miniatura foi junto
        page = ana.get(f"/trabalhos/{job_id}").get_data(as_text=True)
        self.assertNotIn('class="flash', page)  # com JSON, nenhuma mensagem fica guardada pra aparecer depois

    def test_twelve_per_section_and_what_did_not_fit(self):
        self.create_owner()
        job_id = self.create_job(self.create_client(), None)
        files = [(self._photo(), f"f{i}.jpg") for i in range(13)]
        page = self.owner.post(f"/trabalhos/{job_id}/fotos/antes", {"photo": files},
                               follow_redirects=True).get_data(as_text=True)
        self.assertIn("12 photos added. 1 photo didn&#39;t fit: the limit is 12.", page)
        self.assertEqual(len(self.job_photos(job_id)), 12)
        self.assertIn("12 of 12", page)

        r = self.owner.post(f"/trabalhos/{job_id}/fotos/antes", {"photo": (self._photo(), "x.jpg")}, headers=self.JSON)
        self.assertEqual(r.get_json()["over_limit"], 1)
        self.assertIn("already has the maximum of 12 photos", r.get_json()["message"])
        # o Depois tem as 12 dele
        r = self.owner.post(f"/trabalhos/{job_id}/fotos/depois", {"photo": (self._photo(), "y.jpg")}, headers=self.JSON)
        self.assertTrue(r.get_json()["ok"])

    def test_empty_field_bad_file_and_nothing_chosen(self):
        """Sem o photos.js, o formulário manda os dois campos (câmera e galeria), um deles vazio."""
        import io

        self.create_owner()
        job_id = self.create_job(self.create_client(), None)
        url = f"/trabalhos/{job_id}/fotos/depois"
        page = self.owner.post(url, {"photo": [(io.BytesIO(b""), ""), (self._photo(), "ok.jpg")]},
                               follow_redirects=True).get_data(as_text=True)
        self.assertIn("Photo added.", page)
        page = self.owner.post(url, {"photo": [(io.BytesIO(b"isto nao e foto"), "nota.txt"), (self._photo(), "ok.jpg")]},
                               follow_redirects=True).get_data(as_text=True)
        self.assertIn("Photo added. I couldn&#39;t open that file as a photo.", page)
        page = self.owner.post(url, {"photo": (io.BytesIO(b""), "")}, follow_redirects=True).get_data(as_text=True)
        self.assertIn("Choose a photo before uploading.", page)
        self.assertEqual(len(self.job_photos(job_id)), 2)

    def test_photo_is_resized_turned_upright_and_loses_its_location(self):
        import io

        from PIL import ExifTags, Image, ImageCms

        exif = Image.Exif()
        exif[ExifTags.Base.Orientation] = 6  # celular deitado: a foto "de pé" vem girada na metadata
        exif[ExifTags.Base.Make] = "Celular"
        exif[ExifTags.IFD.GPSInfo] = {ExifTags.GPS.GPSLatitudeRef: "N", ExifTags.GPS.GPSLatitude: (51.0, 30.0, 12.0)}
        self.create_owner()
        job_id = self.create_job(self.create_client(), None)
        srgb = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
        big = io.BytesIO()
        Image.new("RGB", (4000, 3000), (10, 120, 60)).save(big, "JPEG", exif=exif, icc_profile=srgb)
        big.seek(0)
        self.owner.post(f"/trabalhos/{job_id}/fotos/antes", {"photo": (big, "grande.jpg")})
        name = self.job_photos(job_id)[0]["filename"]
        with Image.open(self.job_folder(job_id) / name) as full:
            self.assertEqual(full.size, (1200, 1600))  # de pé, com no máximo 1600 px
            self.assertEqual(dict(full.getexif()), {})  # sem GPS, sem modelo do celular
            self.assertEqual(full.info.get("icc_profile"), srgb)  # o perfil de cor fica (cores certas no iPhone)
        with Image.open(self.job_folder(job_id) / (Path(name).stem + "_mini.jpg")) as mini:
            self.assertEqual(mini.size, (360, 480))

    def test_who_can_see_and_delete_job_photos(self):
        self.create_owner()
        client_id = self.create_client()
        ana_id = self.create_employee("Ana", "ana@example.com")
        self.create_employee("Bruno", "bruno@example.com", "senha-do-bruno-1")
        job_ana = self.create_job(client_id, ana_id)
        ana = self.employee_browser()
        bruno = self.employee_browser("bruno@example.com", "senha-do-bruno-1")

        ana.post(f"/trabalhos/{job_ana}/fotos/antes", {"photo": [(self._photo(), "a.jpg"), (self._photo(), "b.jpg")]})
        self.owner.post(f"/trabalhos/{job_ana}/fotos/depois", {"photo": (self._photo(), "c.jpg")})
        mine, mine_too, owners = self.job_photos(job_ana)

        # o Bruno não vê nem mexe no trabalho da Ana (nem sabendo o nome do arquivo)
        self.assertEqual(bruno.post(f"/trabalhos/{job_ana}/fotos/antes", {"photo": (self._photo(), "x.jpg")}).status_code, 404)
        self.assertEqual(bruno.get(f"/trabalhos/{job_ana}/fotos/{mine['filename']}").status_code, 404)
        self.assertEqual(bruno.post(f"/trabalhos/{job_ana}/fotos/{mine['id']}/excluir").status_code, 404)
        # e a galeria geral do cliente é só de quem tem acesso a Clientes
        self.assertEqual(ana.post(f"/clientes/{client_id}/fotos", {"photo": (self._photo(), "x.jpg")}).status_code, 403)

        # a Ana apaga a dela, mas não a do dono
        r = ana.post(f"/trabalhos/{job_ana}/fotos/{owners['id']}/excluir", headers=self.JSON)
        self.assertEqual(r.get_json(), {"ok": False, "message": "You can only delete photos you uploaded yourself."})
        ana.post(f"/trabalhos/{job_ana}/fotos/{mine['id']}/excluir")
        self.assertEqual([r["id"] for r in self.job_photos(job_ana)], [mine_too["id"], owners["id"]])
        self.assertFalse((self.job_folder(job_ana) / mine["filename"]).exists())
        # a foto de um trabalho não abre pelo endereço de outro trabalho
        other_job = self.create_job(client_id, ana_id)
        self.assertEqual(ana.get(f"/trabalhos/{other_job}/fotos/{mine_too['filename']}").status_code, 404)

        # gerente com a agenda apaga qualquer uma
        self.create_manager(perms=("schedule",))
        gil = self.employee_browser("gil@example.com", "senha-do-gil-1")
        gil.post(f"/trabalhos/{job_ana}/fotos/{mine_too['id']}/excluir")
        self.assertEqual([r["id"] for r in self.job_photos(job_ana)], [owners["id"]])

    def test_deleting_the_job_deletes_its_photo_files(self):
        self.create_owner()
        job_id = self.create_job(self.create_client(), None)
        self.owner.post(f"/trabalhos/{job_id}/fotos/antes", {"photo": [(self._photo(), "a.jpg"), (self._photo(), "b.jpg")]})
        self.assertTrue(self.job_folder(job_id).is_dir())
        self.owner.post(f"/trabalhos/{job_id}/excluir")
        self.assertEqual(self.job_photos(job_id), [])
        self.assertFalse(self.job_folder(job_id).exists())

    def test_photos_stop_when_their_space_is_full(self):
        self.create_owner()
        job_id = self.create_job(self.create_client(), None)
        self.app.config["PHOTO_SPACE_MB"] = 0.001  # ~1 KB: cabe só a primeira foto
        r = self.owner.post(f"/trabalhos/{job_id}/fotos/antes", {"photo": (self._photo(), "a.jpg")}, headers=self.JSON)
        self.assertTrue(r.get_json()["ok"])
        r = self.owner.post(f"/trabalhos/{job_id}/fotos/antes", {"photo": (self._photo(), "b.jpg")}, headers=self.JSON)
        data = r.get_json()
        self.assertFalse(data["ok"])
        self.assertTrue(data["stop"])  # o photos.js para de mandar o resto da fila
        self.assertIn("The space for photos is full", data["message"])
        self.assertEqual(len(self.job_photos(job_id)), 1)
        # Conta → Espaço das fotos mostra quanto já foi usado (e o menu da conta também)
        self.app.config["PHOTO_SPACE_MB"] = 300
        self.assertIn("MB of 300 MB used", self.owner.get("/conta/fotos").get_data(as_text=True))
        self.assertIn("MB of 300 MB used", self.owner.get("/conta").get_data(as_text=True))

    def test_disk_really_full_keeps_nothing_half_saved(self):
        import errno

        self.create_owner()
        job_id = self.create_job(self.create_client(), None)
        real_save = __import__("PIL.Image", fromlist=["Image"]).Image.save

        def full_disk(image, fp, *args, **kwargs):
            if str(fp).endswith(".tmp") and "_mini" in str(fp):  # a grande grava, a miniatura não cabe
                raise OSError(errno.EDQUOT, "Disk quota exceeded")
            return real_save(image, fp, *args, **kwargs)

        with mock.patch("PIL.Image.Image.save", full_disk):
            r = self.owner.post(f"/trabalhos/{job_id}/fotos/antes", {"photo": (self._photo(), "a.jpg")}, headers=self.JSON)
        data = r.get_json()
        self.assertEqual((data["ok"], data["stop"]), (False, True))
        self.assertEqual(data["message"], "The space for photos is full. Delete old photos to free some up.")
        self.assertEqual(self.job_photos(job_id), [])
        self.assertEqual(list(self.job_folder(job_id).iterdir()), [])  # nem a grande, nem arquivo temporário

    def test_gallery_several_at_once_and_mini_for_old_photos(self):
        self.create_owner()
        client_id = self.create_client()
        r = self.owner.post(f"/clientes/{client_id}/fotos", {"photo": [(self._photo(), "a.jpg"), (self._photo(), "b.jpg")]},
                            headers=self.JSON)
        self.assertEqual(len(r.get_json()["photos"]), 2)
        name = self.query("SELECT filename FROM client_photos WHERE client_id = ? ORDER BY id", (client_id,))[0]["filename"]
        folder = Path(self.app.config["UPLOAD_ROOT"]) / "clients" / str(client_id)
        (folder / (Path(name).stem + "_mini.jpg")).unlink()  # como as fotos de antes desta versão
        with self.owner.get(f"/clientes/{client_id}/fotos/{name}?mini=1") as img:
            self.assertEqual(img.status_code, 200)
        self.assertTrue((folder / (Path(name).stem + "_mini.jpg")).is_file())
        self.assertEqual(self.owner.get(f"/clientes/{client_id}/fotos/nao-existe.jpg?mini=1").status_code, 404)

    def test_existing_database_gets_the_new_photo_table(self):
        """Banco de uma versão anterior (sem job_photos): a tabela é criada sozinha ao iniciar."""
        self.create_owner()
        job_id = self.create_job(self.create_client(), None)
        with self.app.app_context():
            db = get_db()
            db.execute("DROP TABLE job_photos")
            db.commit()
        app = create_app({"TESTING": True, "SECRET_KEY": "chave-de-teste", "DATABASE": self.app.config["DATABASE"],
                          "UPLOAD_ROOT": self.app.config["UPLOAD_ROOT"]})
        with app.app_context():
            db = get_db()
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
            db.execute("INSERT INTO job_photos (job_id, phase, filename) VALUES (?, 'before', 'x.jpg')", (job_id,))
            with self.assertRaises(Exception):  # só aceita Antes ou Depois
                db.execute("INSERT INTO job_photos (job_id, phase, filename) VALUES (?, 'during', 'y.jpg')", (job_id,))

    def test_every_text_exists_in_both_languages(self):
        from jardim import i18n

        self.assertEqual(set(i18n.TRANSLATIONS["en"]), set(i18n.TRANSLATIONS["pt_BR"]))

    # ---------- preferências: idioma e dados da empresa ----------

    def test_default_language_is_english(self):
        self.create_owner()
        html = self.owner.get("/painel").get_data(as_text=True)
        self.assertIn("Dashboard", html)
        self.assertNotIn("Painel", html)

    def test_switching_language_changes_whole_app(self):
        self.create_owner()
        r = self.owner.post("/conta/idioma", {"language": "", "app_language": "pt_BR"})
        self.assertEqual(r.status_code, 302)
        html = self.owner.get("/painel").get_data(as_text=True)
        self.assertIn("Painel", html)
        self.assertNotIn(">Dashboard<", html)
        # afeta todo mundo, inclusive quem já tinha logado antes da troca
        employee_id = self.create_employee()
        ana = self.employee_browser()
        self.assertIn("Meus trabalhos", ana.get("/meus-trabalhos").get_data(as_text=True))

    def test_profile_and_company_pages_save_their_own_data(self):
        self.create_owner()
        self.assertEqual(self.owner.post("/conta/perfil", {"name": "Renan Silva", "phone": "07700 900999"}).status_code, 302)
        r = self.owner.post("/conta/empresa", {"company_name": "Renan Gardening", "company_phone": "020 7000 0000",
                                               "company_email": "contact@renangardening.co.uk",
                                               "company_address": "1 Garden Row, London"}, follow_redirects=True)
        self.assertIn("Company details saved.", r.get_data(as_text=True))
        row = self.query("SELECT * FROM settings WHERE id = 1")[0]
        self.assertEqual((row["company_name"], row["company_email"], row["company_address"]),
                         ("Renan Gardening", "contact@renangardening.co.uk", "1 Garden Row, London"))
        user = self.query("SELECT name, phone FROM users WHERE role = 'owner'")[0]
        self.assertEqual((user["name"], user["phone"]), ("Renan Silva", "07700 900999"))
        # cada página reaparece preenchida com o que foi salvo, e o menu da conta mostra o que vale agora
        self.assertIn('value="Renan Gardening"', self.owner.get("/conta/empresa").get_data(as_text=True))
        self.assertIn('value="Renan Silva"', self.owner.get("/conta/perfil").get_data(as_text=True))
        hub = self.owner.get("/conta").get_data(as_text=True)
        self.assertIn("<small>Renan Gardening · 020 7000 0000</small>", hub)
        self.assertIn("<small>Renan Silva · 07700 900999</small>", hub)
        # salvar a empresa não mexe nos avisos por e-mail, e vice-versa
        self.enable_email()
        self.assertEqual(self.query("SELECT company_name FROM settings")[0]["company_name"], "Renan Gardening")

    def test_profile_and_company_pages_check_what_was_typed(self):
        self.create_owner()
        r = self.owner.post("/conta/perfil", {"name": "", "phone": "07700 900999"})
        self.assertEqual(r.status_code, 200)  # não redireciona: fica na página com o erro
        self.assertIn("Enter your name.", r.get_data(as_text=True))
        self.assertEqual(self.query("SELECT name FROM users WHERE role = 'owner'")[0]["name"], "Dono Teste")
        r = self.owner.post("/conta/empresa", {"company_name": "X", "company_email": "not-an-email"})
        self.assertEqual(r.status_code, 200)
        self.assertIn('value="X"', r.get_data(as_text=True))  # o que foi digitado continua no formulário
        self.assertEqual(self.query("SELECT company_name FROM settings")[0]["company_name"], "")  # nada foi salvo

    def test_account_menu_has_a_page_for_each_topic(self):
        self.create_owner()
        self.create_employee()
        painel = self.owner.get("/painel").get_data(as_text=True)
        self.assertNotIn("Preferences", painel)  # a aba saiu do menu de cima: tudo fica em Conta
        hub = self.owner.get("/conta").get_data(as_text=True)
        for url in ("/conta/perfil", "/conta/senha", "/conta/idioma", "/conta/empresa", "/conta/aparencia",
                    "/conta/avisos", "/conta/lembretes", "/conta/fotos", "/conta/relatorio-pdf"):
            self.assertIn(f'href="{url}"', hub)
            page = self.owner.get(url).get_data(as_text=True)
            self.assertIn('<p class="crumb"><a href="/conta">‹ My account</a></p>', page)  # volta pro menu
            self.assertIn('href="/conta" aria-current="page">Account</a>', page)  # "Conta" fica marcada no topo
        self.assertIn("<small>Forest palette</small>", hub)
        self.assertIn("apply(data.current);", self.owner.get("/conta/aparencia").get_data(as_text=True))  # prévia ao vivo
        self.assertIn("<small>Off (the team is notified in the app)</small>", hub)
        self.enable_email()
        self.assertIn("<small>On · renan.jardins@gmail.com</small>", self.owner.get("/conta").get_data(as_text=True))
        # o endereço antigo das Preferências leva pro menu da conta
        for old in ("/preferencias", "/preferencias/"):
            r = self.owner.get(old)
            self.assertEqual((r.status_code, r.headers["Location"]), (302, "/conta"))

    def test_employee_account_has_only_password_and_language(self):
        self.create_owner()
        self.create_employee()
        ana = self.employee_browser()
        hub = ana.get("/conta").get_data(as_text=True)
        self.assertIn('href="/conta/senha"', hub)
        self.assertIn('href="/conta/idioma"', hub)
        for url in ("/conta/perfil", "/conta/empresa", "/conta/aparencia", "/conta/avisos", "/conta/lembretes",
                    "/conta/fotos", "/conta/relatorio-pdf"):
            self.assertNotIn(f'href="{url}"', hub)
            self.assertEqual(ana.get(url).status_code, 403, url)
        for url, data in (("/conta/perfil", {"name": "x"}), ("/conta/empresa", {"company_name": "x"}),
                          ("/conta/avisos", {"notify_email": "1"}), ("/conta/aparencia", {"app_name": "x"}),
                          ("/conta/avisos/teste", {}), ("/conta/lembretes", {"reminder_mode": "tap"}),
                          ("/conta/lembretes/teste", {"to": "07700 900999"})):
            self.assertEqual(ana.post(url, data).status_code, 403, url)
        # no idioma, a funcionária escolhe só o dela; o padrão da equipe é do dono
        page = ana.get("/conta/idioma").get_data(as_text=True)
        self.assertNotIn('name="app_language"', page)
        ana.post("/conta/idioma", {"language": "pt_BR", "app_language": "pt_BR"})
        self.assertEqual(self.query("SELECT language FROM settings")[0]["language"], "en")
        self.assertIn('name="app_language"', self.owner.get("/conta/idioma").get_data(as_text=True))

    # ---------- lembrete pro cliente na véspera ----------

    SID, TOKEN = "AC" + "0123456789abcdef" * 2, "fedcba9876543210" * 2

    def at(self, hour, minute=0):
        """Faz o módulo dos lembretes achar que agora são hour:minute (a data continua a de hoje)."""
        patcher = mock.patch("jardim.reminders._now", lambda: datetime.now(timezone.utc).replace(hour=hour, minute=minute))
        patcher.start()
        self.addCleanup(patcher.stop)

    def visit(self, browser=None):
        (browser or self.owner).get("/painel").close()  # a visita que dispara o envio (depois da resposta)

    def reminder_setup(self, mode="sms", **changes):
        self.create_owner()
        self.owner.post("/conta/empresa", {"company_name": "Renan Gardening", "company_phone": "07700 900123"})
        data = {"reminder_mode": mode, "reminder_hour": "18", "reminder_language": "en", "reminder_text": "",
                "twilio_sid": self.SID, "twilio_token": self.TOKEN, "twilio_from": "RenanGarden"}
        data.update(changes)
        self.assertEqual(self.owner.post("/conta/lembretes", data).status_code, 302)
        ana = self.create_employee()

        def client(name, phone, reminders=True):
            self.owner.post("/clientes/novo", {"name": name, "phone": phone, **({"reminders": "1"} if reminders else {})})
            return self.query("SELECT id FROM clients WHERE name = ?", (name,))[0]["id"]

        tomorrow = self.day(1)
        ids = {"okafor": client("Grace Okafor", "07700 900111"), "nophone": client("No Phone Ltd", ""),
               "optout": client("Coachmaker Mews", "07700 900222", reminders=False),
               "cancelled": client("Mr Cancel", "07700 900333"), "later": client("Mr Later", "07700 900444")}
        late, early = self.create_job(ids["okafor"], ana, tomorrow), self.create_job(ids["okafor"], ana, tomorrow)
        self.set_job(late, start_time="14:00")
        self.set_job(early, start_time="09:00")  # dois trabalhos no mesmo cliente: um SMS só, com o horário mais cedo
        for key in ("nophone", "optout"):
            self.create_job(ids[key], ana, tomorrow)
        self.set_job(self.create_job(ids["cancelled"], ana, tomorrow), status="cancelled")
        self.create_job(ids["later"], ana, self.day(2))
        return ids, client, ana

    def expected_when(self, day, time_="9:00"):
        d = date.fromisoformat(day)
        return f"{d:%A} {d.day} {d:%B} at {time_}"

    def test_automatic_sms_goes_once_the_evening_before(self):
        ids, client, ana = self.reminder_setup()
        tomorrow = self.day(1)
        self.at(17, 30)
        self.visit()
        self.assertEqual(FakeTwilio.sent, [])  # antes da hora escolhida (18h) não sai nada
        self.at(18, 5)
        self.visit()
        self.assertEqual(len(FakeTwilio.sent), 1)
        sms = FakeTwilio.sent[0]
        self.assertEqual((sms["to"], sms["from"], sms["user"], sms["password"]), ("+447700900111", "RenanGarden", self.SID, self.TOKEN))
        self.assertIn(f"/Accounts/{self.SID}/Messages.json", sms["url"])
        self.assertEqual(sms["body"], f"Hi Grace Okafor, Renan Gardening here: see you tomorrow, {self.expected_when(tomorrow)}, "
                                      "for your garden. To change anything, call or text 07700 900123. Thanks!")
        from jardim import reminders
        self.assertEqual(reminders.sms_parts(sms["body"])[0], 1)  # cabe num SMS só
        row = self.query("SELECT * FROM client_reminders")[0]
        self.assertEqual((row["client_id"], row["job_date"], row["status"], row["channel"]), (ids["okafor"], tomorrow, "sent", "sms"))
        self.visit()
        with self.app.test_request_context():
            self.assertEqual(reminders.run(force=True), 0)  # nem o link diário manda de novo
        self.assertEqual(len(FakeTwilio.sent), 1)

        # número que o Twilio recusa: fica como "não saiu", com o motivo, e tenta no máximo 3 vezes
        bad = client("Bad Number", "07700 900555")
        self.create_job(bad, ana, tomorrow)
        FakeTwilio.errors["+447700900555"] = (400, {"code": 21211, "message": "The 'To' number is not a valid phone number."})
        with self.app.test_request_context():
            for _ in range(5):
                reminders.run(force=True)
        self.assertEqual(len([m for m in FakeTwilio.sent if m["to"] == "+447700900555"]), 3)
        failed = self.query("SELECT status, attempts, detail FROM client_reminders WHERE client_id = ?", (bad,))[0]
        self.assertEqual((failed["status"], failed["attempts"]), ("failed", 3))
        self.assertEqual(failed["detail"], "This number can't get SMS: check the client's phone.")

        # sem internet (proxy fora do ar): anota a falha, não fica tentando os outros e tenta de novo depois
        def offline(*args):
            raise __import__("urllib.error", fromlist=["URLError"]).URLError("proxy down")
        other = client("Other Client", "07700 900777")
        self.create_job(other, ana, tomorrow)
        with mock.patch("jardim.reminders._post", offline), self.app.test_request_context():
            reminders.run(force=True)
        self.assertIn("Couldn't reach Twilio", self.query("SELECT detail FROM client_reminders WHERE client_id = ?", (other,))[0]["detail"])
        with self.app.test_request_context():
            reminders.run(force=True)
        self.assertEqual(self.query("SELECT status FROM client_reminders WHERE client_id = ?", (other,))[0]["status"], "sent")

        # depois das 21h não sai mais nada, nem pelo link diário
        self.create_job(client("Late Add", "07700 900666"), ana, tomorrow)
        self.at(21, 30)
        with self.app.test_request_context():
            self.assertEqual(reminders.run(force=True), 0)
        self.assertFalse([m for m in FakeTwilio.sent if m["to"] == "+447700900666"])

        page = self.owner.get("/lembretes").get_data(as_text=True)
        self.assertIn("✓ SMS", page)
        self.assertIn("This number can&#39;t get SMS", page)
        self.assertIn("No phone", page)
        self.assertIn("Reminder off for this client", page)
        self.assertNotIn("Mr Cancel", page)  # trabalho cancelado não tem lembrete
        self.assertNotIn("Mr Later", page)   # depois de amanhã: ainda não
        painel = self.owner.get("/painel").get_data(as_text=True)
        self.assertIn("2 of 4 clients reminded", painel)
        self.assertIn("Failed: 1", painel)

        # mandar de novo, na mão, depois de arrumar o problema
        del FakeTwilio.errors["+447700900555"]
        r = self.owner.post(f"/lembretes/{bad}/{tomorrow}/sms", follow_redirects=True)
        self.assertIn("SMS sent to Bad Number.", r.get_data(as_text=True))
        self.assertEqual(self.query("SELECT status FROM client_reminders WHERE client_id = ?", (bad,))[0]["status"], "sent")

    def test_daily_link_sends_the_client_sms_in_the_evening(self):
        self.reminder_setup()
        self.at(18, 10)
        self.owner.get("/conta/avisos")  # cria a chave do link diário
        key = self.query("SELECT cron_key FROM settings")[0]["cron_key"]
        FakeTwilio.sent.clear()
        text = Browser(self.app).get(f"/avisos/diario/{key}").get_data(as_text=True)
        self.assertIn("1 SMS pro cliente", text)
        self.assertEqual([m["to"] for m in FakeTwilio.sent], ["+447700900111"])

    def test_one_tap_mode_lists_tomorrow_with_whatsapp_and_sms(self):
        ids, client, ana = self.reminder_setup(mode="tap")
        tomorrow = self.day(1)
        self.at(18, 5)
        self.visit()
        self.assertEqual(FakeTwilio.sent, [])  # com um toque, nada sai sozinho
        painel = self.owner.get("/painel").get_data(as_text=True)
        self.assertIn("0 of 1 clients reminded", painel)
        self.assertIn("Tap to send by WhatsApp or SMS.", painel)
        page = self.owner.get("/lembretes").get_data(as_text=True)
        when = __import__("urllib.parse", fromlist=["quote"]).quote(self.expected_when(tomorrow))
        self.assertIn(f'href="https://wa.me/447700900111?text=Hi%20Grace%20Okafor%2C%20Renan%20Gardening%20here%3A%20see%20you%20tomorrow%2C%20{when}%2C', page)
        self.assertIn('href="sms:+447700900111?&amp;body=Hi%20Grace%20Okafor', page)
        r = self.owner.post(f"/lembretes/{ids['okafor']}/{tomorrow}/feito", {"channel": "whatsapp"},
                            headers={"Accept": "application/json"})
        self.assertEqual(r.get_json()["label"], "WhatsApp")
        self.assertIn("✓ WhatsApp", self.owner.get("/lembretes").get_data(as_text=True))
        self.assertIn("1 of 1 clients reminded", self.owner.get("/painel").get_data(as_text=True))
        self.owner.post(f"/lembretes/{ids['okafor']}/{tomorrow}/desfazer")
        self.assertEqual(self.query("SELECT COUNT(*) AS n FROM client_reminders")[0]["n"], 0)
        self.assertEqual(self.owner.post(f"/lembretes/{ids['okafor']}/2020-01-01/feito", {"channel": "manual"}).status_code, 404)
        self.assertEqual(self.owner.post(f"/lembretes/{ids['later']}/{tomorrow}/feito", {"channel": "manual"}).status_code, 404)
        ana_b = self.employee_browser()
        self.assertEqual(ana_b.get("/lembretes").status_code, 403)
        self.assertEqual(ana_b.post(f"/lembretes/{ids['okafor']}/{tomorrow}/feito", {"channel": "manual"}).status_code, 403)
        self.assertEqual(ana_b.get("/conta/lembretes").status_code, 403)
        # com o lembrete desligado, o card some do Painel
        self.owner.post("/conta/lembretes", {"reminder_mode": "off", "reminder_hour": "18", "reminder_language": "en"})
        self.assertNotIn("clients reminded", self.owner.get("/painel").get_data(as_text=True))

    def test_reminder_settings_check_twilio_and_hide_the_token(self):
        self.create_owner()
        self.assertIn("<small>Off</small>", self.owner.get("/conta").get_data(as_text=True))
        r = self.owner.post("/conta/lembretes", {"reminder_mode": "sms", "reminder_hour": "18", "reminder_language": "en",
                                                 "twilio_sid": "123", "twilio_token": "abc", "twilio_from": "Renan's Garden Co"})
        page = r.get_data(as_text=True)
        self.assertEqual(r.status_code, 200)
        for msg in ("starts with AC", "has 32 characters", "up to 11 letters"):
            self.assertIn(msg, page)
        self.assertEqual(self.query("SELECT reminder_mode FROM settings")[0]["reminder_mode"], "off")  # nada salvo
        self.owner.post("/conta/lembretes", {"reminder_mode": "sms", "reminder_hour": "19", "reminder_language": "pt_BR",
                                             "reminder_text": "", "twilio_sid": self.SID, "twilio_token": self.TOKEN,
                                             "twilio_from": "07700 900000"})
        row = self.query("SELECT * FROM settings")[0]
        self.assertEqual((row["reminder_mode"], row["reminder_hour"], row["reminder_language"], row["twilio_from"], row["twilio_token"]),
                         ("sms", 19, "pt_BR", "+447700900000", self.TOKEN))  # o número vira +44...
        page = self.owner.get("/conta/lembretes").get_data(as_text=True)
        self.assertNotIn(self.TOKEN, page)  # o token nunca volta pra tela
        self.assertIn("saved — leave blank to keep it", page)
        self.owner.post("/conta/lembretes", {"reminder_mode": "sms", "reminder_hour": "19", "reminder_language": "pt_BR",
                                             "twilio_sid": self.SID, "twilio_token": "", "twilio_from": "+447700900000"})
        self.assertEqual(self.query("SELECT twilio_token FROM settings")[0]["twilio_token"], self.TOKEN)  # em branco: mantém
        self.assertIn("<small>Automatic SMS from 19:00</small>", self.owner.get("/conta").get_data(as_text=True))
        # SMS de teste, em português, com o nome do dono de exemplo
        r = self.owner.post("/conta/lembretes/teste", {"to": "07700 900999"}, follow_redirects=True)
        self.assertIn("Test SMS sent to +447700900999", r.get_data(as_text=True))
        self.assertTrue(FakeTwilio.sent[-1]["body"].startswith("Olá, Dono! Aqui é da "), FakeTwilio.sent[-1]["body"])
        FakeTwilio.errors["+447700900998"] = (401, {"code": 20003, "message": "Authenticate"})
        r = self.owner.post("/conta/lembretes/teste", {"to": "07700 900998"}, follow_redirects=True)
        self.assertIn("Twilio refused the login", r.get_data(as_text=True))
        # o cadastro de cliente novo já vem com o lembrete ligado
        self.assertIn('name="reminders" value="1" checked', self.owner.get("/clientes/novo").get_data(as_text=True))

    def test_reminder_message_pieces(self):
        from jardim import reminders
        with self.app.test_request_context():
            g.lang = "en"
            self.assertEqual(reminders.when_text("2026-09-25", "09:30", "en"), "Friday 25 September at 9:30")
            self.assertEqual(reminders.when_text("2026-09-25", "", "pt_BR"), "sexta-feira, 25 de setembro")
            self.assertEqual(reminders.when_text("2026-09-25", "14:00", "pt_BR"), "sexta-feira, 25 de setembro, às 14:00")
            vals = {"client": "Ana", "when": "amanhã", "date": "25", "time": "9:00", "company": "RG", "phone": "0770"}
            self.assertEqual(reminders.render("Oi {cliente}, {quando} ({empresa}, {telefone}) {client}", vals),
                             "Oi Ana, amanhã (RG, 0770) Ana")
        self.assertEqual(reminders.sms_parts("a" * 160), (1, 160, False))
        self.assertEqual(reminders.sms_parts("a" * 161), (2, 161, False))
        self.assertEqual(reminders.sms_parts("ç" * 70), (1, 70, True))  # acento fora do padrão do SMS: 70 por SMS
        self.assertEqual(reminders.sms_parts("ã" * 71), (2, 71, True))
        self.assertEqual(reminders.sms_parts("€"), (1, 2, False))  # conta como dois
        for phone, expected in (("07700 900111", "447700900111"), ("+44 7700 900111", "447700900111"),
                                ("0044 7700 900111", "447700900111"), ("+55 11 91234-5678", "5511912345678"),
                                ("123", ""), ("", "")):
            self.assertEqual(utils.intl_phone(phone), expected, phone)


    # ---------- exclusão de colaborador, foto própria, horas e histórico ----------

    def test_delete_member_blocked_with_jobs_allowed_without(self):
        self.create_owner()
        client_id = self.create_client()
        busy_id = self.create_employee("Ana", "ana@example.com")
        free_id = self.create_employee("Bruno", "bruno@example.com")
        self.create_job(client_id, busy_id)

        r = self.owner.post(f"/equipe/{busy_id}/excluir")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(len(self.query("SELECT 1 FROM users WHERE id = ?", (busy_id,))), 1)  # não apagou

        r = self.owner.post(f"/equipe/{free_id}/excluir")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(len(self.query("SELECT 1 FROM users WHERE id = ?", (free_id,))), 0)  # apagou

    def test_owner_cannot_delete_self(self):
        self.create_owner()
        owner_id = self.query("SELECT id FROM users WHERE role = 'owner'")[0]["id"]
        self.owner.post(f"/equipe/{owner_id}/excluir")
        self.assertEqual(len(self.query("SELECT 1 FROM users WHERE id = ?", (owner_id,))), 1)

    def test_weekly_hours_sums_only_this_week(self):
        self.create_owner()
        client_id = self.create_client()
        employee_id = self.create_employee()
        today = utils_today(self.app)
        job_id = self.create_job(client_id, employee_id, today)
        with self.app.app_context():
            from jardim.db import get_db
            db = get_db()
            db.execute("UPDATE jobs SET status='done', started_at=?, finished_at=? WHERE id=?",
                      (f"{today}T09:00:00+00:00", f"{today}T11:00:00+00:00", job_id))
            db.commit()
        html = self.employee_browser().get("/meus-trabalhos").get_data(as_text=True)
        self.assertIn("2h00", html)

    # ---------- minhas horas (só consulta) ----------

    def hours_setup(self):
        """Semana de 14 a 20/09/2026 (segunda a domingo). Horários em Londres (+01:00 no verão)."""
        self.create_owner()
        flores = self.create_client()
        oak = self.create_client("Oak House")
        ana = self.create_employee("Ana", "ana@example.com")
        bruno = self.create_employee("Bruno", "bruno@example.com", "senha-do-bruno-1")
        def done(job, day, a, b):
            self.set_job(job, status="done", started_at=f"{day}T{a}:00+01:00", finished_at=f"{day}T{b}:00+01:00")
            return job
        jobs = {
            "mon": done(self.create_job(flores, ana, "2026-09-14"), "2026-09-14", "09:00", "11:30"),       # 2h30
            "wed1": done(self.create_job(oak, ana, "2026-09-16"), "2026-09-16", "08:00", "12:15"),         # 4h15
            "share": done(self.create_job(flores, [ana, bruno], "2026-09-16"), "2026-09-16", "13:00", "15:00"),  # 2h cada
            "bruno": done(self.create_job(oak, bruno, "2026-09-15"), "2026-09-15", "09:00", "17:00"),      # só do Bruno
            "old": done(self.create_job(flores, ana, "2020-01-15"), "2020-01-15", "09:00", "10:00"),
            "cancelled": self.create_job(oak, ana, "2026-09-17"),
            "running": self.create_job(oak, ana, "2026-09-18"),
            "no_start": self.create_job(flores, ana, "2026-09-19"),
        }
        self.set_job(jobs["cancelled"], status="cancelled")
        self.set_job(jobs["running"], status="in_progress", started_at="2026-09-18T10:00:00+01:00")
        self.set_job(jobs["no_start"], status="done", finished_at="2026-09-19T12:00:00+01:00")
        return ana, bruno, jobs

    def test_my_hours_week_month_year_and_day(self):
        ana_id, bruno_id, jobs = self.hours_setup()
        ana = self.employee_browser()

        week = ana.get("/minhas-horas?periodo=semana&data=2026-09-16").get_data(as_text=True)
        self.assertIn("<b>8h45</b><span>Hours worked</span>", week)  # 2h30 + 4h15 + 2h (Share conta inteiro)
        self.assertIn("in 2 days", week)
        self.assertIn("<b>4</b><span>Jobs done</span>", week)  # o concluído sem Iniciar conta como trabalho, sem horas
        self.assertIn("<b>4h22</b><span>Average per day</span>", week)
        self.assertIn('aria-label="Mon, 14 Sep: 2h30, 1 job"', week)
        self.assertIn('aria-label="Wed, 16 Sep: 6h15, 2 jobs"', week)
        self.assertIn('<span class="hc-val">6h15</span>', week)
        self.assertIn('href="/minhas-horas?periodo=dia&amp;data=2026-09-16"', week)  # tocar na coluna abre o dia
        for key in ("mon", "wed1", "share", "no_start"):
            self.assertIn(f'href="/trabalhos/{jobs[key]}"', week)
        for key in ("bruno", "old", "cancelled"):
            self.assertNotIn(f'href="/trabalhos/{jobs[key]}"', week)
        self.assertIn("Still in progress", week)  # o em andamento aparece, mas não soma horas
        self.assertIn(f'<a href="/trabalhos/{jobs["running"]}">Oak House</a> · Fri, 18 Sep · since 10:00', week)
        self.assertIn("Finished without Start", week)
        self.assertIn("Hours per client", week)
        self.assertIn("In a Share job, the whole time counts for each person who went.", week)
        self.assertIn("Let Dono know.", week)  # quem corrige é o dono

        month = ana.get("/minhas-horas?periodo=mes&data=2026-09-16").get_data(as_text=True)
        self.assertIn("<b>8h45</b><span>Hours worked</span>", month)
        self.assertIn('<a href="/minhas-horas?periodo=semana&amp;data=2026-09-14">14–20 Sep</a>', month)
        self.assertIn('<a href="/minhas-horas?periodo=semana&amp;data=2026-09-01">1–6 Sep</a>', month)
        self.assertIn('class="hc-grid"', month)  # mês: linhas de grade no lugar do número em cada coluna
        self.assertNotIn('class="hc-val"', month)

        year = ana.get("/minhas-horas?periodo=ano&data=2026-09-16").get_data(as_text=True)
        self.assertIn('<a href="/minhas-horas?periodo=mes&amp;data=2026-09-01">Sep 2026</a>', year)
        self.assertIn("Hours per month", year)
        self.assertIn('aria-label="Sep 2026: 8h45, 4 jobs"', year)
        self.assertIn('<span>J</span><span>F</span><span>M</span>', year)  # só a inicial do mês embaixo da coluna

        day = ana.get("/minhas-horas?periodo=dia&data=2026-09-16").get_data(as_text=True)
        self.assertIn("<b>08:00</b><span>Started</span>", day)
        self.assertIn("<b>15:00</b><span>Finished</span>", day)
        self.assertIn("08:00 – 12:15", day)
        self.assertIn('class="dl-bar" style="left: 0.0%; width: 53.12%"', day)  # régua 08:00–16:00, marcas de 2 em 2h
        self.assertIn('<span style="left: 100.0%">16:00</span>', day)
        empty = ana.get("/minhas-horas?periodo=dia&data=2026-09-20").get_data(as_text=True)
        self.assertIn("No jobs finished on this day.", empty)
        self.assertIn("No hours in this period.", ana.get("/minhas-horas?periodo=semana&data=2026-01-07").get_data(as_text=True))
        self.assertIn("in 1 day", ana.get("/minhas-horas?periodo=ano&data=2020-06-01").get_data(as_text=True))

        bruno = self.employee_browser("bruno@example.com", "senha-do-bruno-1")
        page = bruno.get("/minhas-horas?periodo=semana&data=2026-09-16").get_data(as_text=True)
        self.assertIn("<b>10h00</b><span>Hours worked</span>", page)  # 8h dele + 2h do Share

    def test_my_hours_is_view_only_and_always_your_own(self):
        ana_id, bruno_id, jobs = self.hours_setup()
        ana = self.employee_browser()
        page = ana.get(f"/minhas-horas?periodo=semana&data=2026-09-16&user={bruno_id}&pessoa={bruno_id}").get_data(as_text=True)
        self.assertIn("<b>8h45</b><span>Hours worked</span>", page)  # parâmetro nenhum mostra as horas de outra pessoa
        main = page.split("<main", 1)[1]
        self.assertNotIn("<form", main)  # nada pra digitar, trocar ou salvar
        self.assertNotIn("<input", main)
        self.assertEqual(ana.post("/minhas-horas", {}).status_code, 405)
        # a funcionária agora tem duas abas: os trabalhos e as horas
        self.assertIn('<a href="/meus-trabalhos">My jobs</a>', page)
        self.assertIn('<a href="/minhas-horas" class="active" aria-current="page">My hours</a>', page)
        self.assertIn('href="/minhas-horas"', ana.get("/meus-trabalhos").get_data(as_text=True))
        # o endereço antigo do histórico leva pra cá, no mesmo período
        r = ana.get("/meus-trabalhos/historico?periodo=mes")
        self.assertEqual((r.status_code, r.headers["Location"]), (302, "/minhas-horas?periodo=mes"))
        self.assertEqual(Browser(self.app).get("/minhas-horas").status_code, 302)  # sem login: vai pro login
        # o dono também tem as horas dele (e não aparece "fale com o dono" pra ele mesmo)
        own = self.owner.get("/minhas-horas").get_data(as_text=True)
        self.assertIn("View only", own)
        self.assertNotIn("Let Dono know", own)

    # ---------- avisos automáticos para a equipe ----------

    def test_scheduling_a_job_notifies_the_assignee_in_the_app(self):
        self.create_owner()
        client_id = self.create_client()
        ana_id = self.create_employee()
        self.create_job(client_id, ana_id, self.day(5))
        self.assertEqual([n["kind"] for n in self.notices(ana_id)], ["job_assigned"])
        owner_id = self.query("SELECT id FROM users WHERE role = 'owner'")[0]["id"]
        self.assertEqual(self.notices(owner_id), [])  # quem agendou não é avisado do que ele mesmo fez
        ana = self.employee_browser()
        self.assertIn('class="badge">1<', ana.get("/meus-trabalhos").get_data(as_text=True))
        html = ana.get("/avisos/").get_data(as_text=True)
        self.assertIn("New job: Sítio das Flores", html)
        self.assertIn("notice unread", html)
        self.assertIsNotNone(self.notices(ana_id)[0]["read_at"])  # abrir a tela de avisos marca como lido
        self.assertNotIn('class="badge"', ana.get("/meus-trabalhos").get_data(as_text=True))

    def test_changes_notify_the_right_people(self):
        self.create_owner()
        client_id = self.create_client()
        ana_id = self.create_employee("Ana", "ana@example.com")
        bruno_id = self.create_employee("Bruno", "bruno@example.com", "senha-do-bruno-1")
        job_id = self.create_job(client_id, ana_id, self.day(5))
        self.edit_job(job_id, assigned_to=bruno_id)  # passou da Ana para o Bruno
        self.assertEqual([n["kind"] for n in self.notices(ana_id)], ["job_assigned", "job_unassigned"])
        self.edit_job(job_id, job_date=self.day(6))  # remarcado
        self.edit_job(job_id, title="Poda")          # mudou só o título: ninguém precisa ser avisado
        self.edit_job(job_id, status="cancelled")
        self.assertEqual([n["kind"] for n in self.notices(bruno_id)],
                         ["job_assigned", "job_rescheduled", "job_cancelled"])
        other = self.create_job(client_id, bruno_id, self.day(7))
        self.owner.post(f"/trabalhos/{other}/excluir")
        deleted = self.notices(bruno_id, "job_deleted")
        self.assertEqual(len(deleted), 1)
        self.assertIsNone(deleted[0]["job_id"])  # o trabalho sumiu, mas o aviso continua

    def test_day_before_reminder_is_created_once(self):
        self.create_owner()
        client_id = self.create_client()
        ana_id = self.create_employee()
        job_id = self.create_job(client_id, ana_id, self.day(1))
        self.create_job(client_id, ana_id, self.day(3))  # ainda longe: sem lembrete
        ana = self.employee_browser()
        ana.get("/meus-trabalhos")
        self.assertEqual(self.notices(ana_id, "job_tomorrow"), [])  # acabou de ser avisada: sem lembrete em dobro
        self.backdate_notices(days=3)  # como se o trabalho tivesse sido agendado há 3 dias
        ana.get("/meus-trabalhos")
        ana.get("/meus-trabalhos")
        self.assertEqual([n["job_id"] for n in self.notices(ana_id, "job_tomorrow")], [job_id])
        self.assertIn("Reminder: job tomorrow", ana.get("/avisos/").get_data(as_text=True))

    def test_repeating_sends_one_notice_not_one_per_copy(self):
        self.create_owner()
        client_id = self.create_client()
        ana_id = self.create_employee()
        job_id = self.create_job(client_id, ana_id, self.day(2))
        self.owner.post(f"/trabalhos/{job_id}/repetir", {"every": "1", "times": "4"})
        self.assertEqual(len(self.notices(ana_id, "jobs_repeated")), 1)
        self.assertIn("4 new jobs", self.employee_browser().get("/avisos/").get_data(as_text=True))

    def test_notices_are_private(self):
        self.create_owner()
        client_id = self.create_client()
        ana_id = self.create_employee("Ana", "ana@example.com")
        self.create_employee("Bruno", "bruno@example.com", "senha-do-bruno-1")
        self.create_job(client_id, ana_id, self.day(2))
        bruno = self.employee_browser("bruno@example.com", "senha-do-bruno-1")
        self.assertNotIn("Sítio das Flores", bruno.get("/avisos/").get_data(as_text=True))
        self.assertIsNone(self.notices(ana_id)[0]["read_at"])  # o Bruno abrir os avisos dele não mexe nos da Ana

    def test_email_goes_out_after_the_page_is_delivered(self):
        self.create_owner()
        self.enable_email()
        client_id = self.create_client()
        ana_id = self.create_employee()
        r = self.owner.post("/trabalhos/novo", {"client_id": client_id, "assigned_to": ana_id, "title": "Manutenção",
                                                "job_date": self.day(4), "start_time": "08:30", "tasks": "Cortar"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(FakeSMTP.sent, [])  # o dono não espera pelo e-mail
        r.close()  # o servidor terminou de entregar a página: agora o e-mail sai
        self.assertEqual(len(FakeSMTP.sent), 1)
        msg = FakeSMTP.sent[0]
        self.assertEqual((msg["To"], msg["From"]), ("ana@example.com", "renan.jardins@gmail.com"))
        self.assertIn("Sítio das Flores", msg["Subject"])
        self.assertIn("/trabalhos/", msg.get_content())
        self.assertNotIn(self.notices(ana_id)[0]["emailed_at"], (None, "skipped"))

    def test_email_failure_keeps_the_notice_for_a_retry(self):
        self.create_owner()
        self.enable_email()
        client_id = self.create_client()
        ana_id = self.create_employee()
        FakeSMTP.fail_login = True
        r = self.owner.post("/trabalhos/novo", {"client_id": client_id, "assigned_to": ana_id, "title": "Manutenção",
                                                "job_date": self.day(4), "start_time": "08:30", "tasks": "Cortar"})
        with self.assertLogs(self.app.logger, level="ERROR"):  # o erro fica registrado, a página não quebra
            r.close()
        self.assertEqual((r.status_code, FakeSMTP.sent), (302, []))
        self.assertIsNone(self.notices(ana_id)[0]["emailed_at"])  # volta para a fila

    def test_daily_link_sends_reminder_email_once(self):
        self.create_owner()
        self.enable_email()
        client_id = self.create_client()
        ana_id = self.create_employee()
        self.create_job(client_id, ana_id, self.day(1))
        self.backdate_notices(days=3)
        with self.app.app_context():  # o aviso de agendamento já tinha saído por e-mail naquele dia
            db = get_db()
            db.execute("UPDATE notifications SET emailed_at = 'skipped'")
            db.commit()
        self.assertEqual(Browser(self.app).get("/avisos/diario/chave-errada").status_code, 404)
        self.owner.get("/conta/avisos")  # a tela cria a chave do link diário
        key = self.query("SELECT cron_key FROM settings")[0]["cron_key"]
        r = Browser(self.app).get(f"/avisos/diario/{key}")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_data(as_text=True).startswith("ok"))
        reminders = [m for m in FakeSMTP.sent if m["Subject"].startswith("Reminder")]
        self.assertEqual([m["To"] for m in reminders], ["ana@example.com"])
        Browser(self.app).get(f"/avisos/diario/{key}")  # chamar de novo não manda em dobro
        self.assertEqual(len([m for m in FakeSMTP.sent if m["Subject"].startswith("Reminder")]), 1)

    def test_first_visit_after_the_hour_sends_reminders_once_a_day(self):
        self.app.config["DAILY_EMAIL_HOUR"] = 0  # "depois das 17h" vira "a qualquer hora" no teste
        self.create_owner()
        self.enable_email()
        client_id = self.create_client()
        ana_id = self.create_employee()
        self.create_job(client_id, ana_id, self.day(1))
        self.backdate_notices(days=3)
        with self.app.app_context():
            db = get_db()
            db.execute("UPDATE notifications SET emailed_at = 'skipped'")
            db.execute("UPDATE settings SET last_daily_run = ''")  # as visitas da preparação do teste não contam
            db.commit()
        visitor = Browser(self.app)
        with visitor.get("/login"):  # qualquer visita, mesmo de alguém deslogado
            pass
        reminders = [m for m in FakeSMTP.sent if m["Subject"].startswith("Reminder")]
        self.assertEqual([m["To"] for m in reminders], ["ana@example.com"])
        with visitor.get("/login"):  # segunda visita no mesmo dia: não repete
            pass
        self.assertEqual(len([m for m in FakeSMTP.sent if m["Subject"].startswith("Reminder")]), 1)
        self.assertEqual(self.query("SELECT last_daily_run FROM settings")[0]["last_daily_run"], utils_today(self.app))

    def test_email_settings_keep_password_and_test_email(self):
        self.create_owner()
        self.enable_email()
        self.owner.post("/conta/avisos", {"notify_email": "1", "mail_address": "renan.jardins@gmail.com", "mail_password": ""})
        self.assertEqual(self.query("SELECT mail_password FROM settings")[0]["mail_password"], "wxyz wxyz wxyz wxyz")
        self.assertNotIn("wxyz", self.owner.get("/conta/avisos").get_data(as_text=True))  # a senha nunca volta pra tela
        self.owner.post("/conta/avisos/teste")
        self.assertEqual([m["To"] for m in FakeSMTP.sent], ["dono@example.com"])
        FakeSMTP.fail_login = True
        html = self.owner.post("/conta/avisos/teste", follow_redirects=True).get_data(as_text=True)
        self.assertIn("Gmail refused the login", html)

    # ---------- lista padrão de tarefas e feita / não feita ----------

    COACHMAKER = ("Watered the plants\nSwept the front of the house\nTrimmed the bamboo\n"
                  "Sprayed the bamboo with copper fungicide")

    def set_template(self, client_id, text):
        self.owner.post(f"/clientes/{client_id}/editar", {
            "name": "Coachmaker Mews", "address": "Coachmaker Mews", "postcode": "", "phone": "",
            "access_notes": "", "notes": "", "tier": "", "task_template": text})

    def test_client_standard_tasks_show_up_to_tick(self):
        self.create_owner()
        client_id = self.create_client()
        self.set_template(client_id, "- Watered the plants\n\n• Swept the front of the house\nTrimmed the bamboo")
        self.assertEqual(self.query("SELECT task_template FROM clients WHERE id = ?", (client_id,))[0]["task_template"],
                         "Watered the plants\nSwept the front of the house\nTrimmed the bamboo")  # colou com marcadores: limpa
        self.assertIn("Trimmed the bamboo", self.owner.get(f"/clientes/{client_id}").get_data(as_text=True))
        self.assertIn('"Swept the front of the house"', self.owner.get("/trabalhos/novo").get_data(as_text=True))

    def test_ticked_tasks_plus_others_become_the_checklist(self):
        self.create_owner()
        client_id = self.create_client()
        self.set_template(client_id, self.COACHMAKER)
        ana = self.create_employee()
        r = self.owner.post("/trabalhos/novo", {
            "client_id": client_id, "assigned_to": ana, "title": "Manutenção", "job_date": self.day(1), "start_time": "",
            "task_pick": ["Watered the plants", "Trimmed the bamboo"],  # desmarcou duas da lista padrão
            "tasks": "Cleared up the leaves\nwatered the plants", "save_template": "1"})
        self.assertEqual(r.status_code, 302)
        job_id = self.query("SELECT id FROM jobs ORDER BY id DESC LIMIT 1")[0]["id"]
        tasks = [t["description"] for t in self.query(
            "SELECT description FROM job_tasks WHERE job_id = ? ORDER BY position", (job_id,))]
        self.assertEqual(tasks, ["Watered the plants", "Trimmed the bamboo", "Cleared up the leaves"])  # sem repetir
        template = self.query("SELECT task_template FROM clients WHERE id = ?", (client_id,))[0]["task_template"]
        self.assertEqual(template.split("\n"), self.COACHMAKER.split("\n") + ["Cleared up the leaves"])  # nada some da lista
        # editando, as da lista padrão voltam marcadas e o resto fica em "outras tarefas"
        html = self.owner.get(f"/trabalhos/{job_id}/editar").get_data(as_text=True)
        self.assertIn('["Watered the plants", "Trimmed the bamboo", "Cleared up the leaves"]', html)

    def test_employee_marks_done_or_not_done_with_optional_reason(self):
        self.create_owner()
        client_id = self.create_client()
        ana = self.create_employee()
        job_id = self.create_job(client_id, ana)  # 3 tarefas
        ids = [t["id"] for t in self.query("SELECT id FROM job_tasks WHERE job_id = ? ORDER BY position", (job_id,))]
        ana_b = self.employee_browser()
        self.assertIn(f'name="task_{ids[0]}" value="not_done"', ana_b.get(f"/trabalhos/{job_id}").get_data(as_text=True))
        r = ana_b.post(f"/trabalhos/{job_id}/atualizar", {
            "action": "finish", f"task_{ids[0]}": "done",
            f"task_{ids[1]}": "not_done", f"note_{ids[1]}": "Choveu forte, fica pra próxima",
            f"task_{ids[2]}": "not_done", f"note_{ids[2]}": ""}, follow_redirects=True)  # sem motivo também vale
        rows = self.query("SELECT done, skipped, note FROM job_tasks WHERE job_id = ? ORDER BY position", (job_id,))
        self.assertEqual([(x["done"], x["skipped"], x["note"]) for x in rows],
                         [(1, 0, ""), (0, 1, "Choveu forte, fica pra próxima"), (0, 1, "")])
        self.assertIn("2 marked as not done", r.get_data(as_text=True))
        owner_html = self.owner.get(f"/trabalhos/{job_id}").get_data(as_text=True)
        self.assertIn("Choveu forte, fica pra próxima", owner_html)
        self.assertIn("res-skip", owner_html)
        self.assertIn("--s: 2", self.owner.get("/trabalhos?ver=todos").get_data(as_text=True))  # barrinha com as não feitas

    def test_editing_the_job_keeps_marks_and_reasons(self):
        self.create_owner()
        client_id = self.create_client()
        ana = self.create_employee()
        job_id = self.create_job(client_id, ana)
        ids = [t["id"] for t in self.query("SELECT id FROM job_tasks WHERE job_id = ? ORDER BY position", (job_id,))]
        self.employee_browser().post(f"/trabalhos/{job_id}/atualizar", {
            "action": "start", f"task_{ids[0]}": "done", f"task_{ids[1]}": "not_done", f"note_{ids[1]}": "Sem escada"})
        self.edit_job(job_id, status="in_progress", tasks="Cortar a grama\nPodar a cerca-viva\nEnsacar resíduos\nRegar")
        rows = self.query("SELECT description, done, skipped, note FROM job_tasks WHERE job_id = ? ORDER BY position", (job_id,))
        self.assertEqual([(x["description"], x["done"], x["skipped"], x["note"]) for x in rows], [
            ("Cortar a grama", 1, 0, ""), ("Podar a cerca-viva", 0, 1, "Sem escada"),
            ("Ensacar resíduos", 0, 0, ""), ("Regar", 0, 0, "")])

    def report_text(self, browser, job_id):
        page = browser.get(f"/trabalhos/{job_id}").get_data(as_text=True)
        found = re.search(r'<textarea id="report-text"[^>]*>(.*?)</textarea>', page, re.S)
        return (html.unescape(found.group(1)) if found else None), page

    def test_whatsapp_report_after_finishing(self):
        self.create_owner()
        client_id = self.create_client()
        self.set_template(client_id, self.COACHMAKER)  # o cliente passa a se chamar Coachmaker Mews
        ana = self.create_employee()
        self.owner.post("/trabalhos/novo", {
            "client_id": client_id, "assigned_to": ana, "title": "Manutenção", "job_date": self.day(0), "start_time": "",
            "task_pick": self.COACHMAKER.split("\n"), "tasks": "Took before and after photos"})
        job_id = self.query("SELECT id FROM jobs ORDER BY id DESC LIMIT 1")[0]["id"]
        ids = [t["id"] for t in self.query("SELECT id FROM job_tasks WHERE job_id = ? ORDER BY position", (job_id,))]
        ana_b = self.employee_browser()
        self.assertIsNone(self.report_text(ana_b, job_id)[0])  # só aparece depois de concluir
        ana_b.post(f"/trabalhos/{job_id}/atualizar", {
            "action": "finish", f"task_{ids[0]}": "done", f"task_{ids[1]}": "done",
            f"task_{ids[2]}": "not_done", f"note_{ids[2]}": "Needs a ladder", f"task_{ids[3]}": "done",
            f"task_{ids[4]}": "done"})  # a das fotos foi feita, mas não entra no relatório (as fotos vão separadas)
        today = date.fromisoformat(utils_today(self.app)).strftime("%d/%m/%Y")
        expected = (f"Coachmaker Mews – {today}\n"
                    "Tasks completed:\nWatered the plants\nSwept the front of the house\n"
                    "Sprayed the bamboo with copper fungicide\n\n"
                    "Tasks not completed:\nTrimmed the bamboo (Needs a ladder)")
        report, page = self.report_text(self.owner, job_id)
        self.assertEqual(report, expected)
        self.assertIn("https://wa.me/?text=Coachmaker%20Mews", page)
        self.assertEqual(self.report_text(ana_b, job_id)[0], expected)  # quem fez o trabalho também pode copiar

    # ---------- dinheiro recebido do cliente ----------

    def finish_with_cash(self, cash, in_report):
        self.create_owner()
        client_id = self.create_client()
        self.set_template(client_id, self.COACHMAKER)  # o cliente passa a se chamar Coachmaker Mews
        job_id = self.create_job(client_id, self.create_employee())
        first = self.query("SELECT id FROM job_tasks WHERE job_id = ? ORDER BY position", (job_id,))[0]["id"]
        data = {"action": "finish", f"task_{first}": "done", "cash": cash}
        if in_report:
            data["cash_in_report"] = "1"
        r = self.employee_browser().post(f"/trabalhos/{job_id}/atualizar", data, follow_redirects=True)
        return job_id, r.get_data(as_text=True)

    def test_cash_goes_in_the_report_when_ticked(self):
        job_id, _ = self.finish_with_cash("60,5", in_report=True)
        job = self.query("SELECT cash_pence, cash_in_report, status FROM jobs WHERE id = ?", (job_id,))[0]
        self.assertEqual((job["cash_pence"], job["cash_in_report"], job["status"]), (6050, 1, "done"))
        report, _ = self.report_text(self.owner, job_id)
        self.assertTrue(report.endswith("\n\nCash collected: £60.50"), report)

    def test_cash_left_out_of_the_report_but_the_owner_still_sees_it(self):
        job_id, _ = self.finish_with_cash("£45", in_report=False)
        report, page = self.report_text(self.owner, job_id)
        self.assertNotIn("Cash collected", report)
        self.assertIn("£45", page)
        self.assertIn("not in the report", page)

    def test_invalid_cash_keeps_the_marks_and_does_not_finish(self):
        job_id, page = self.finish_with_cash("sessenta", in_report=True)
        job = self.query("SELECT cash_pence, status FROM jobs WHERE id = ?", (job_id,))[0]
        self.assertEqual((job["cash_pence"], job["status"]), (None, "scheduled"))  # não concluiu
        self.assertEqual(self.query("SELECT done FROM job_tasks WHERE job_id = ? ORDER BY position", (job_id,))[0]["done"], 1)
        self.assertIn("Invalid cash amount", page)

    def test_money_parsing_and_format(self):
        for text, pence in [("60", 6000), ("60.5", 6050), ("60,50", 6050), ("£ 60", 6000), ("", None), ("0", None)]:
            self.assertEqual(utils.parse_money(text), pence, text)
        for bad in ("abc", "-5", "1,200", "60.555"):
            with self.assertRaises(ValueError):
                utils.parse_money(bad)
        self.assertEqual((utils.money(6000), utils.money(6050), utils.money(120000)), ("£60", "£60.50", "£1,200"))

    # ---------- relatório da empresa ----------

    def set_job(self, job_id, **fields):
        """Muda o trabalho direto no banco. Dinheiro sem cash_by fica com a primeira pessoa do trabalho,
        como se ela mesma tivesse anotado o valor."""
        if fields.get("cash_pence") and "cash_by" not in fields:
            fields["cash_by"] = (self.people(job_id) or [None])[0]
        with self.app.app_context():
            db = get_db()
            db.execute(f"UPDATE jobs SET {', '.join(k + ' = ?' for k in fields)} WHERE id = ?", (*fields.values(), job_id))
            db.commit()

    def test_company_report_numbers_for_the_week(self):
        self.create_owner()
        client_id = self.create_client()
        ana = self.create_employee("Ana", "ana@example.com")
        bruno = self.create_employee("Bruno", "bruno@example.com", "senha-do-bruno-1")
        today = utils_today(self.app)
        j1, j2, j3 = (self.create_job(client_id, who, today) for who in (ana, bruno, ana))
        self.set_job(j1, status="done", started_at=f"{today}T09:00:00+00:00", finished_at=f"{today}T11:00:00+00:00",
                     cash_pence=6000)
        self.set_job(j2, status="done", started_at=f"{today}T12:00:00+00:00", finished_at=f"{today}T12:30:00+00:00",
                     cash_pence=4550, cash_in_report=0)  # fora do relatório do WhatsApp, mas conta pro dono
        self.set_job(j3, status="cancelled")
        page = self.owner.get("/relatorio/").get_data(as_text=True)
        for expected in ("£105.50", "£60", "£45.50", "2h30", "2h00", "30 min", "of 3 scheduled", ">Ana<", ">Bruno<"):
            self.assertIn(expected, page)

    def test_company_report_other_periods_and_access(self):
        self.create_owner()
        client_id = self.create_client()
        old = self.create_job(client_id, self.create_employee(), "2025-03-10")
        # já repassado: não aparece em "com a equipe", então só o filtro de período decide onde ele aparece
        self.set_job(old, cash_pence=2000, cash_received_at="2025-03-11T10:00:00+00:00")
        self.assertNotIn("£20", self.owner.get("/relatorio/").get_data(as_text=True))  # não é desta semana
        self.assertIn("£20", self.owner.get("/relatorio/?periodo=semana&data=2025-03-12").get_data(as_text=True))
        self.assertIn("£20", self.owner.get("/relatorio/?periodo=ano&data=2025-06-01").get_data(as_text=True))
        self.assertEqual(self.owner.get("/relatorio/?periodo=xyz&data=bobagem").status_code, 200)  # cai no padrão
        self.assertEqual(self.employee_browser().get("/relatorio/").status_code, 403)

    def test_dashboard_shows_cash_collected_this_week(self):
        self.create_owner()
        client_id = self.create_client()
        job = self.create_job(client_id, self.create_employee(), utils_today(self.app))
        self.set_job(job, cash_pence=7525)
        page = self.owner.get("/painel").get_data(as_text=True)
        self.assertIn("£75.25", page)
        self.assertIn('href="/relatorio/"', page)

    def test_marking_cash_received_moves_it_out_of_to_hand_over(self):
        self.create_owner()
        client_id = self.create_client()
        ana = self.create_employee()
        this_week = self.create_job(client_id, ana, utils_today(self.app))
        old = self.create_job(client_id, ana, "2025-01-10")
        self.set_job(this_week, status="done", cash_pence=6000)
        self.set_job(old, status="done", cash_pence=2000)
        self.assertIn("£80", self.owner.get("/relatorio/").get_data(as_text=True))  # a repassar inclui a antiga
        self.assertIn("£80", self.owner.get("/painel").get_data(as_text=True))
        r = self.owner.post(f"/relatorio/dinheiro/{this_week}/recebido", {"next": "/relatorio/?periodo=semana"})
        self.assertTrue(r.headers["Location"].endswith("/relatorio/?periodo=semana"))
        self.assertIsNotNone(self.query("SELECT cash_received_at FROM jobs WHERE id = ?", (this_week,))[0]["cash_received_at"])
        dash = self.owner.get("/painel").get_data(as_text=True)
        self.assertIn("£60", dash)      # esta semana continua £60
        self.assertIn("£20", dash)      # a repassar: só a antiga
        self.assertNotIn("£80", dash)
        self.owner.post(f"/relatorio/dinheiro/{this_week}/recebido")  # tocar de novo desfaz
        self.assertIsNone(self.query("SELECT cash_received_at FROM jobs WHERE id = ?", (this_week,))[0]["cash_received_at"])
        # só o dono, só trabalho com dinheiro, e o "next" nunca leva pra fora do app
        self.assertEqual(self.employee_browser().post(f"/relatorio/dinheiro/{old}/recebido").status_code, 403)
        self.assertEqual(self.owner.post(f"/relatorio/dinheiro/{self.create_job(client_id, ana)}/recebido").status_code, 404)
        r = self.owner.post(f"/relatorio/dinheiro/{old}/recebido", {"next": "https://example.com/"})
        self.assertTrue(r.headers["Location"].endswith("/relatorio/"))

    def test_changing_the_cash_amount_undoes_received(self):
        self.create_owner()
        client_id = self.create_client()
        job_id = self.create_job(client_id, self.create_employee())
        ana_b = self.employee_browser()
        ana_b.post(f"/trabalhos/{job_id}/atualizar", {"action": "start", "cash": "60", "cash_in_report": "1"})
        self.owner.post(f"/relatorio/dinheiro/{job_id}/recebido")
        ana_b.post(f"/trabalhos/{job_id}/atualizar", {"action": "save", "cash": "60", "cash_in_report": "1"})  # mesmo valor
        self.assertIsNotNone(self.query("SELECT cash_received_at FROM jobs WHERE id = ?", (job_id,))[0]["cash_received_at"])
        ana_b.post(f"/trabalhos/{job_id}/atualizar", {"action": "save", "cash": "65", "cash_in_report": "1"})  # mudou
        row = self.query("SELECT cash_pence, cash_received_at FROM jobs WHERE id = ?", (job_id,))[0]
        self.assertEqual((row["cash_pence"], row["cash_received_at"]), (6500, None))

    # ---------- gráficos e PDF do relatório ----------

    def test_report_charts_on_the_page(self):
        self.create_owner()
        client_id = self.create_client()
        today = utils_today(self.app)
        job = self.create_job(client_id, self.create_employee(), today)
        self.set_job(job, status="done", started_at=f"{today}T09:00:00+00:00", finished_at=f"{today}T10:00:00+00:00",
                     cash_pence=5000)
        page = self.owner.get("/relatorio/").get_data(as_text=True)
        self.assertEqual(page.count('class="vbar"'), 7)  # uma barra por dia da semana
        self.assertIn('class="hbars"', page)             # horas por pessoa
        self.assertEqual(page.count('class="stackbar"'), 2)  # dinheiro e tarefas
        self.assertEqual(self.owner.get("/relatorio/?periodo=ano").get_data(as_text=True).count('class="vbar"'), 12)
        self.assertNotIn('class="vbar"', self.owner.get("/relatorio/?periodo=dia").get_data(as_text=True))

    def test_company_report_pdf_download(self):
        self.create_owner()
        client_id = self.create_client()
        today = utils_today(self.app)
        job = self.create_job(client_id, self.create_employee(), today)
        self.set_job(job, status="done", started_at=f"{today}T09:00:00+00:00", finished_at=f"{today}T10:30:00+00:00",
                     cash_pence=6050)
        for period in ("semana", "mes", "ano"):
            with self.owner.get(f"/relatorio/pdf?periodo={period}&data={today}") as r:
                self.assertEqual((r.status_code, r.mimetype), (200, "application/pdf"))
                self.assertTrue(r.data.startswith(b"%PDF"))
                self.assertIn(f"relatorio-{period}-", r.headers["Content-Disposition"])
                pdf = r.data
        if shutil.which("pdftotext"):  # confere o texto quando a ferramenta existe (no PythonAnywhere pode não ter)
            text = subprocess.run(["pdftotext", "-", "-"], input=pdf, capture_output=True, check=True).stdout.decode()
            for expected in ("Company report", "£60.50", "Sítio das Flores", "1h30"):
                self.assertIn(expected, text)
        with self.owner.get("/relatorio/pdf?periodo=semana&data=2020-01-01") as r:  # período vazio também gera
            self.assertEqual(r.status_code, 200)
        self.assertEqual(self.employee_browser().get("/relatorio/pdf").status_code, 403)
        self.assertIn('action="/relatorio/pdf"', self.owner.get("/conta/relatorio-pdf").get_data(as_text=True))

    # ---------- finanças ----------

    def add_transfer(self, **fields):
        data = {"received_on": utils_today(self.app), "client_id": "", "amount": "120", "note": ""}
        data.update(fields)
        return self.owner.post("/financas/", data)

    def test_finance_records_transfers_and_adds_cash(self):
        self.create_owner()
        client_id = self.create_client()
        today = utils_today(self.app)
        job = self.create_job(client_id, self.create_employee(), today)
        self.set_job(job, cash_pence=4550)  # dinheiro recebido pela equipe entra sozinho
        r = self.add_transfer(client_id=str(client_id), amount="120,50", note="Fatura de setembro")
        self.assertEqual(r.status_code, 302)
        row = self.query("SELECT * FROM transfers")[0]
        self.assertEqual((row["received_on"], row["amount_pence"], row["client_id"], row["note"]),
                         (today, 12050, client_id, "Fatura de setembro"))
        page = self.owner.get("/financas/").get_data(as_text=True)
        for expected in ("£166", "£120.50", "£45.50", "Fatura de setembro", "Sítio das Flores"):
            self.assertIn(expected, page)
        self.assertEqual(page.count('class="vbar"'), 7)  # gráfico da semana: uma barra por dia

    def test_finance_validation_period_delete_and_access(self):
        self.create_owner()
        self.create_employee()
        for bad in ({"amount": ""}, {"amount": "abc"}, {"received_on": "2026-02-31"}, {"client_id": "999"}):
            self.assertEqual(self.add_transfer(**bad).status_code, 200)  # volta pro formulário, com o erro
        self.assertEqual(self.query("SELECT COUNT(*) AS n FROM transfers")[0]["n"], 0)
        r = self.add_transfer(received_on="2025-03-10", amount="80")
        self.assertIn("data=2025-03-10", r.headers["Location"])  # abre o período em que ela caiu
        self.assertIn("£80", self.owner.get(r.headers["Location"]).get_data(as_text=True))
        self.assertNotIn("£80", self.owner.get("/financas/").get_data(as_text=True))  # não é desta semana
        self.assertIn("£80", self.owner.get("/financas/?periodo=ano&data=2025-06-01").get_data(as_text=True))
        transfer_id = self.query("SELECT id FROM transfers")[0]["id"]
        ana = self.employee_browser()
        self.assertEqual(ana.get("/financas/").status_code, 403)
        self.assertEqual(ana.post("/financas/", {"amount": "10"}).status_code, 403)
        self.assertEqual(ana.post(f"/financas/transferencias/{transfer_id}/excluir").status_code, 403)
        self.owner.post(f"/financas/transferencias/{transfer_id}/excluir")
        self.assertEqual(self.query("SELECT COUNT(*) AS n FROM transfers")[0]["n"], 0)
        self.assertEqual(self.owner.post(f"/financas/transferencias/{transfer_id}/excluir").status_code, 404)

    def test_finance_chart_labels_round_half_up(self):
        from jardim import finance
        self.assertEqual([finance._short(p) for p in (13050, 12049, 100000, 125000, 99999)],
                         ["£131", "£120", "£1k", "£1.3k", "£1k"])

    # ---------- idioma de cada pessoa ----------

    def test_each_person_can_pick_their_language(self):
        self.create_owner()
        self.create_employee()
        ana = self.employee_browser()
        self.assertIn("My jobs", ana.get("/meus-trabalhos").get_data(as_text=True))  # padrão do app: inglês
        r = ana.post("/conta/idioma", {"language": "pt_BR"}, follow_redirects=True)
        self.assertIn("Idioma salvo.", r.get_data(as_text=True))  # a mensagem já vem no idioma novo
        self.assertIn("Meus trabalhos", ana.get("/meus-trabalhos").get_data(as_text=True))
        self.assertIn("Dashboard", self.owner.get("/painel").get_data(as_text=True))  # o dono continua em inglês
        ana.post("/conta/idioma", {"language": ""})  # volta a seguir o padrão do app...
        self.owner.post("/conta/idioma", {"language": "", "app_language": "pt_BR"})  # o dono muda o padrão da equipe
        self.assertIn("Meus trabalhos", ana.get("/meus-trabalhos").get_data(as_text=True))  # ...que agora é português
        ana.post("/conta/idioma", {"language": "klingon"})  # valor estranho: fica no padrão
        self.assertEqual(self.query("SELECT language FROM users WHERE role = 'employee'")[0]["language"], "")

    def test_notice_emails_go_out_in_each_persons_language(self):
        self.create_owner()
        self.enable_email()  # app (e dono) em inglês
        client_id = self.create_client()
        ana_id = self.create_employee()
        self.employee_browser().post("/conta/idioma", {"language": "pt_BR"})
        r = self.owner.post("/trabalhos/novo", {"client_id": client_id, "assigned_to": ana_id, "title": "Manutenção",
                                                "job_date": self.day(4), "start_time": "08:30", "tasks": "Cortar"})
        r.close()
        self.assertEqual(len(FakeSMTP.sent), 1)
        self.assertTrue(FakeSMTP.sent[0]["Subject"].startswith("Novo trabalho: "), FakeSMTP.sent[0]["Subject"])

    # ---------- cargo gerente ----------

    def create_manager(self, perms=(), email="gil@example.com"):
        data = {"name": "Gil", "email": email, "phone": "", "role": "manager", "password": "senha-do-gil-1"}
        data.update({f"perm_{p}": "1" for p in perms})
        self.assertEqual(self.owner.post("/equipe/novo", data).status_code, 302)
        return self.query("SELECT id FROM users WHERE email = ?", (email,))[0]["id"]

    def edit_manager(self, member_id, role="manager", perms=()):
        data = {"name": "Gil", "email": "gil@example.com", "phone": "", "role": role, "password": "", "active": "1"}
        data.update({f"perm_{p}": "1" for p in perms})
        self.assertEqual(self.owner.post(f"/equipe/{member_id}/editar", data).status_code, 302)

    def test_manager_sees_only_what_the_owner_ticked(self):
        self.create_owner()
        client_id = self.create_client()
        ana_id = self.create_employee()
        job_id = self.create_job(client_id, ana_id)
        self.create_manager(perms=("schedule",))
        gil = self.employee_browser("gil@example.com", "senha-do-gil-1")
        for url, status in (("/painel", 200), ("/trabalhos", 200), (f"/trabalhos/{job_id}", 200), ("/trabalhos/novo", 200),
                            ("/meus-trabalhos", 200), ("/clientes/", 403), ("/relatorio/", 403), ("/financas/", 403),
                            ("/equipe/", 403), ("/conta/empresa", 403), ("/conta/avisos", 403), ("/conta", 200)):
            self.assertEqual(gil.get(url).status_code, status, url)
        self.assertTrue(gil.get("/").headers["Location"].endswith("/painel"))
        painel = gil.get("/painel").get_data(as_text=True)
        self.assertNotIn("Team cash", painel)          # dinheiro da equipe: só com acesso ao relatório
        self.assertNotIn('href="/clientes/"', painel)  # o menu só mostra o que foi liberado
        self.assertNotIn('href="/equipe/"', painel)
        r = gil.post("/trabalhos/novo", {"client_id": client_id, "assigned_to": ana_id, "title": "Poda",
                                         "job_date": self.day(3), "start_time": "", "tasks": "Podar"})
        self.assertEqual(r.status_code, 302)  # o gerente agenda pra equipe...
        self.assertEqual(len(self.notices(ana_id, "job_assigned")), 2)  # ...e a Ana é avisada

    def test_manager_sensitive_access_and_role_change(self):
        self.create_owner()
        client_id = self.create_client()
        job = self.create_job(client_id, self.create_employee(), utils_today(self.app))
        self.set_job(job, status="done", cash_pence=5000)
        gil_id = self.create_manager(perms=("report", "clients"))
        self.assertIn("Manager: Clients, Report", self.owner.get("/equipe/").get_data(as_text=True))
        gil = self.employee_browser("gil@example.com", "senha-do-gil-1")
        page = gil.get("/relatorio/").get_data(as_text=True)
        self.assertIn("£50", page)
        self.assertNotIn("/recebido", page)  # sem "confirmar repasses", o botão Recebi nem aparece
        self.assertEqual(gil.post(f"/relatorio/dinheiro/{job}/recebido").status_code, 403)
        with gil.get("/relatorio/pdf") as r:
            self.assertEqual(r.status_code, 200)
        self.assertEqual(gil.get("/clientes/").status_code, 200)
        self.assertEqual(gil.get("/financas/").status_code, 403)
        self.assertEqual(gil.get("/painel").status_code, 403)  # sem a agenda
        self.edit_manager(gil_id, perms=("report", "clients", "cash"))  # o dono libera os repasses
        self.assertEqual(gil.post(f"/relatorio/dinheiro/{job}/recebido").status_code, 302)
        self.edit_manager(gil_id, role="employee", perms=("report", "finance"))  # virou funcionário: acessos somem
        self.assertEqual(self.query("SELECT permissions FROM users WHERE id = ?", (gil_id,))[0]["permissions"], "")
        self.assertEqual(gil.get("/relatorio/").status_code, 403)
        self.assertTrue(gil.get("/").headers["Location"].endswith("/meus-trabalhos"))

    def test_backup_script_copies_everything_even_what_is_still_in_the_wal(self):
        """backup.py tem que levar junto o que ainda está no jardim.db-wal (baixar só o .db perderia isso)."""
        import sqlite3
        project = Path(self.tmp.name) / "projeto"
        (project / "instance").mkdir(parents=True)
        shutil.copy(Path(__file__).resolve().parent.parent / "backup.py", project / "backup.py")
        live = sqlite3.connect(project / "instance" / "jardim.db")
        live.execute("PRAGMA journal_mode = WAL")
        live.execute("CREATE TABLE t (x)")
        live.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(50)])
        live.commit()  # a conexão continua aberta: as linhas ainda estão no arquivo -wal
        self.assertTrue((project / "instance" / "jardim.db-wal").exists())
        out = subprocess.run([sys.executable, "backup.py"], cwd=project, capture_output=True, text=True, check=True)
        self.assertIn("Cópia salva: backups/jardim-", out.stdout)
        copy = next((project / "backups").glob("jardim-*.db"))
        self.assertEqual(sqlite3.connect(copy).execute("SELECT COUNT(*) FROM t").fetchone()[0], 50)
        live.close()

    def test_delete_confirmations_survive_apostrophes(self):
        """Em inglês o aviso tem apóstrofo ("can't"): antes isso quebrava o JavaScript e excluía sem perguntar."""
        self.create_owner()
        client_id = self.create_client()
        job_id = self.create_job(client_id, self.create_employee())
        page = self.owner.get(f"/trabalhos/{job_id}").get_data(as_text=True)
        self.assertIn('data-confirm="Delete this job? This can&#39;t be undone."', page)  # a janelinha "Você tem certeza?"
        self.assertNotIn("return confirm(", page)

    def test_duration_and_date_filters(self):
        with self.app.test_request_context():
            self.assertEqual(utils.duration("2026-09-21T09:00:00+00:00", "2026-09-21T11:15:00+00:00"), "2h15")
            self.assertEqual(utils.duration("2026-09-21T09:00:00+00:00", "2026-09-21T09:40:00+00:00"), "40 min")
            self.assertEqual(utils.duration(None, "2026-09-21T09:40:00+00:00"), "—")
            self.assertEqual(utils.date_long("2026-09-21"), "Mon, 21 Sep")  # inglês é o idioma padrão
            self.assertEqual(utils.dt_local("2026-09-20T18:00:00+00:00"), "20/09 at 19:00")
            g.lang = "pt_BR"
            self.assertEqual(utils.date_long("2026-09-21"), "seg, 21 set")
            self.assertEqual(utils.dt_local("2026-09-20T18:00:00+00:00"), "20/09 às 19:00")
            self.assertEqual(utils.time_local("2026-07-01T12:00:00+00:00"), "13:00")  # horário de verão em Londres

    # ---------- área da empresa ----------

    def company_setup(self, language="en"):
        """Empresa (Hillside) com o jardim 12 Elm Road ligado, o Sítio das Flores particular, a Ana na equipe
        e o acesso da Laura. Devolve (empresa, elm, particular, ana, página do acesso criado)."""
        self.create_owner()
        private = self.create_client()
        elm = self.create_client("12 Elm Road")
        ana_id = self.create_employee()
        r = self.owner.post("/empresas/nova", {"name": "Hillside Property Services"})
        company_id = self.query("SELECT id FROM companies")[0]["id"]
        self.assertTrue(r.headers["Location"].endswith(f"/empresas/{company_id}"))
        self.owner.post(f"/empresas/{company_id}/jardins", {"client_id": elm})
        page = self.owner.post(f"/empresas/{company_id}/acessos/novo", {
            "name": "Laura Mills", "email": "laura@hillside.co.uk", "language": language, "password": "senha-da-laura-1"})
        self.assertEqual(page.status_code, 200)
        return company_id, elm, private, ana_id, page.get_data(as_text=True)

    def portal_browser(self, email="laura@hillside.co.uk", password="senha-da-laura-1"):
        b = Browser(self.app)
        r = b.login(email, password)
        self.assertEqual(r.status_code, 302, "login da empresa deveria funcionar")
        self.assertTrue(r.headers["Location"].endswith("/portal/"), r.headers["Location"])
        return b

    def set_times(self, job_id, start, finish):
        """Concluído, com o Iniciar e o Concluir nesses horários (hora de Londres), direto no banco."""
        day = self.query("SELECT job_date FROM jobs WHERE id = ?", (job_id,))[0]["job_date"]
        with self.app.app_context():
            tz = utils.tz()
            iso = [datetime.fromisoformat(f"{day}T{hm}:00").replace(tzinfo=tz).astimezone(timezone.utc).isoformat(timespec="seconds")
                   for hm in (start, finish)]
            db = get_db()
            db.execute("UPDATE jobs SET status = 'done', started_at = ?, finished_at = ? WHERE id = ?", (*iso, job_id))
            db.commit()

    def test_company_area_shows_only_finished_services_at_its_gardens(self):
        company_id, elm, private, ana_id, access = self.company_setup()
        # a senha aparece uma vez, com a mensagem pronta pro WhatsApp; no banco ela fica embaralhada
        self.assertIn("senha-da-laura-1", access)
        self.assertIn("https://wa.me/?text=Hi%20Laura%21", access)
        self.assertNotIn("senha-da-laura-1", self.query("SELECT password_hash FROM company_users")[0]["password_hash"])
        today = self.day(0)
        done = self.create_job(elm, ana_id, today, tasks="Mow the lawn\nTrim the hedge\nTake before and after photos\nWeed the path")
        ids = [t["id"] for t in self.query("SELECT id FROM job_tasks WHERE job_id = ? ORDER BY position, id", (done,))]
        ana = self.employee_browser()
        ana.post(f"/trabalhos/{done}/fotos/antes", {"photo": (self._photo(), "a.jpg")})
        ana.post(f"/trabalhos/{done}/fotos/depois", {"photo": [(self._photo(), "b.jpg"), (self._photo(), "c.jpg")]})
        ana.post(f"/trabalhos/{done}/atualizar", {
            "action": "finish", f"task_{ids[0]}": "done", f"task_{ids[1]}": "done", f"task_{ids[2]}": "done",
            f"task_{ids[3]}": "not_done", f"note_{ids[3]}": "Path blocked by a skip",
            "materials": "1 bag of green waste, half bag of strulch", "employee_notes": "Recado interno da Ana", "cash": "45"})
        self.set_times(done, "10:20", "11:20")
        open_job = self.create_job(elm, ana_id, today)  # ainda não foi concluído
        other = self.create_job(private, ana_id, today)  # jardim particular
        self.set_times(other, "12:00", "13:00")

        laura = self.portal_browser()
        self.assertTrue(laura.get("/").headers["Location"].endswith("/portal/"))
        day = laura.get("/portal/").get_data(as_text=True)
        service = laura.get(f"/portal/service/{done}").get_data(as_text=True)
        for page in (day, service):
            self.assertIn("12 Elm Road", page)
            self.assertIn("10:20–11:20 · 1h00", page)
            self.assertIn("1 bag of green waste, half bag of strulch", page)
            for private_bit in ("Sítio das Flores", "£45", "Recado interno", "Ana", "07123 456789", "Portão azul",
                                "Só o dono", "Take before and after photos"):
                self.assertNotIn(private_bit, page)
        self.assertIn("2 tasks done", day)
        self.assertIn("1 not done", day)
        self.assertIn("Path blocked by a skip", service)
        self.assertIn("Weed the path", service)
        # só serviço concluído de jardim da empresa
        self.assertEqual(laura.get(f"/portal/service/{open_job}").status_code, 404)
        self.assertEqual(laura.get(f"/portal/service/{other}").status_code, 404)
        # fotos: só as daquele serviço
        mine = self.job_photos(done)[0]["filename"]
        r = laura.get(f"/portal/service/{done}/photo/{mine}?mini=1")
        self.assertEqual((r.status_code, r.mimetype), (200, "image/jpeg"))
        r.close()
        self.assertIn(f"/portal/service/{done}/photo/{mine}", day)
        self.assertEqual(laura.get(f"/portal/service/{other}/photo/{mine}").status_code, 404)
        self.assertEqual(laura.get(f"/portal/service/{done}/photo/nao-existe.jpg").status_code, 404)
        # jardins: só os da empresa
        gardens = laura.get("/portal/gardens").get_data(as_text=True)
        self.assertIn("12 Elm Road", gardens)
        self.assertNotIn("Sítio das Flores", gardens)
        self.assertEqual(laura.get(f"/portal/gardens/{elm}").status_code, 200)
        self.assertEqual(laura.get(f"/portal/gardens/{private}").status_code, 404)
        # nenhuma tela da equipe abre pra ela
        for url in ("/painel", "/clientes/", f"/trabalhos/{done}", "/conta", "/avisos/", "/empresas/",
                    f"/trabalhos/{done}/fotos/{mine}", "/minhas-horas", "/meus-trabalhos"):
            r = laura.get(url)
            self.assertEqual(r.status_code, 302, url)
            self.assertIn("/login", r.headers["Location"], url)
        self.assertTrue(laura.get("/login").headers["Location"].endswith("/"))
        # sem serviço hoje: mostra o último dia que teve, com o caminho pro anterior
        self.assertIn("No services on this day.", laura.get(f"/portal/?day={self.day(-5)}").get_data(as_text=True))

    def test_planned_hours_are_split_between_the_people_on_a_share_job(self):
        self.create_owner()
        client = self.create_client()
        ana = self.create_employee("Ana", "ana@example.com")
        bruno = self.create_employee("Bruno", "bruno@example.com", "senha-do-bruno-1")
        # texto livre: fica como foi escrito; um intervalo divide as duas pontas; sem tempo reconhecível, sem "cada"
        for typed, each in (("1 to 2hrs", "30 min–1h00 each"), ("15min", "7 min each"), ("2 hours", "1h00 each"),
                            ("ask the client", None)):
            self.owner.post("/trabalhos/novo", {"client_id": client, "assigned_to": [str(ana), str(bruno)], "title": "X",
                                                "job_date": self.day(1), "planned_hours": typed})
            jid = self.query("SELECT id FROM jobs ORDER BY id DESC LIMIT 1")[0]["id"]
            page = self.owner.get(f"/trabalhos/{jid}").get_data(as_text=True)
            self.assertIn(typed, page)
            if each:
                self.assertIn(each, page)
            else:
                self.assertNotIn(" each", page.split("Planned hours")[1][:120])
            self.owner.post(f"/trabalhos/{jid}/excluir")
        self.assertEqual(self.query("SELECT COUNT(*) AS n FROM jobs")[0]["n"], 0)
        # 2 horas pra duas pessoas: 1h00 cada, com o Share
        self.owner.post("/trabalhos/novo", {"client_id": client, "assigned_to": [str(ana), str(bruno)], "title": "Manutenção",
                                            "job_date": self.day(1), "planned_hours": "2"})
        job = self.query("SELECT id, planned_minutes FROM jobs ORDER BY id DESC LIMIT 1")[0]
        self.assertEqual(job["planned_minutes"], 120)
        detail = self.owner.get(f"/trabalhos/{job['id']}").get_data(as_text=True)
        self.assertIn("Planned hours", detail)
        self.assertIn("1h00 each", detail)
        self.assertIn("pill-share", detail)
        agenda = self.owner.get("/trabalhos").get_data(as_text=True)
        self.assertIn("2 · 1h00 each", agenda)  # o texto como foi digitado, mais a parte de cada um
        # uma pessoa só: nada de Share nem de "cada"; editar mantém o formato digitável
        form = self.owner.get(f"/trabalhos/{job['id']}/editar").get_data(as_text=True)
        self.assertIn('name="planned_hours" value="2"', form)
        self.owner.post(f"/trabalhos/{job['id']}/editar", {"client_id": client, "assigned_to": str(ana), "title": "Manutenção",
                                                          "job_date": self.day(1), "planned_hours": "1:30", "status": "scheduled"})
        detail = self.owner.get(f"/trabalhos/{job['id']}").get_data(as_text=True)
        self.assertIn("1:30", detail)
        self.assertNotIn("45 min each", detail)
        self.assertNotIn("pill-share", detail)
        self.assertIn('value="1:30"', self.owner.get(f"/trabalhos/{job['id']}/editar").get_data(as_text=True))
        # repetir copia as horas previstas
        self.owner.post(f"/trabalhos/{job['id']}/repetir", {"every": "1", "times": "2"})
        self.assertEqual([r["planned_minutes"] for r in self.query("SELECT planned_minutes FROM jobs ORDER BY id")], [90, 90, 90])

    def test_phone_fields_take_a_country_code_and_the_team_has_an_address(self):
        self.create_owner()
        # equipe: código do país + número (o 0 da frente sai), endereço gravado e mostrado na lista
        self.owner.post("/equipe/novo", {"name": "Ana", "email": "ana@example.com", "password": "senha-da-ana-1",
                                         "role": "employee", "phone_cc": "44", "phone": "07700 900111",
                                         "address": "3 Rose Lane, N1 1AA"})
        ana = self.query("SELECT id, phone, address FROM users WHERE email = 'ana@example.com'")[0]
        self.assertEqual((ana["phone"], ana["address"]), ("+44 7700 900111", "3 Rose Lane, N1 1AA"))
        team = self.owner.get("/equipe/").get_data(as_text=True)
        self.assertIn("+44 7700 900111", team)
        self.assertIn("3 Rose Lane, N1 1AA", team)
        form = self.owner.get(f"/equipe/{ana['id']}/editar").get_data(as_text=True)
        self.assertIn('<option value="44" selected>🇬🇧 +44</option>', form)
        self.assertIn('name="phone" value="7700 900111"', form)
        self.assertIn('name="address" value="3 Rose Lane, N1 1AA"', form)
        # Brasil: outro código; e sem o código (formulário antigo) o número fica como veio
        self.owner.post(f"/equipe/{ana['id']}/editar", {"name": "Ana", "email": "ana@example.com", "role": "employee",
                                                        "active": "1", "phone_cc": "55", "phone": "11 99999 0000"})
        self.assertEqual(self.query("SELECT phone FROM users WHERE id = ?", (ana["id"],))[0]["phone"], "+55 11 99999 0000")
        self.assertIn('<option value="55" selected>🇧🇷 +55</option>', self.owner.get(f"/equipe/{ana['id']}/editar").get_data(as_text=True))
        # cliente, perfil e empresa usam o mesmo campo; o WhatsApp/SMS entende o número com o código
        client = self.create_client()
        self.owner.post(f"/clientes/{client}/editar", {"name": "Sítio das Flores", "phone_cc": "353", "phone": "087 123 4567"})
        self.assertEqual(self.query("SELECT phone FROM clients WHERE id = ?", (client,))[0]["phone"], "+353 87 123 4567")
        self.assertEqual(utils.intl_phone("+353 87 123 4567"), "353871234567")
        self.owner.post("/conta/perfil", {"name": "Dono", "phone_cc": "44", "phone": "07700 900999"})
        self.assertEqual(self.query("SELECT phone FROM users WHERE role = 'owner'")[0]["phone"], "+44 7700 900999")
        self.owner.post("/conta/empresa", {"company_name": "X", "company_phone_cc": "44", "company_phone": "020 7000 0000"})
        self.assertEqual(self.query("SELECT company_phone FROM settings")[0]["company_phone"], "+44 20 7000 0000")
        for url in ("/clientes/novo", "/conta/perfil", "/conta/empresa", "/cotacoes/nova"):
            page = self.owner.get(url).get_data(as_text=True)
            self.assertIn('_cc"', page, url)
            self.assertIn("🇬🇧 +44", page, url)

    def test_companies_have_a_tier_and_a_colour_from_the_palette(self):
        company_id, elm, private, ana_id, _ = self.company_setup()
        # criar com categoria e cor (o formulário de nova empresa tem os dois)
        lst = self.owner.get("/empresas/").get_data(as_text=True)
        self.assertNotIn('name="tier"', lst)  # a lista é só a lista; o formulário tem página própria
        self.assertIn('href="/empresas/nova"', lst)
        page = self.owner.get("/empresas/nova").get_data(as_text=True)
        self.assertIn('name="tier"', page)
        self.assertIn('name="color" value="#2f6b86"', page)
        self.owner.post("/empresas/nova", {"name": "SLQ Properties", "tier": "platina", "color": "#2f6b86"})
        slq = self.query("SELECT id, tier, color FROM companies WHERE name = 'SLQ Properties'")[0]
        self.assertEqual((slq["tier"], slq["color"]), ("platina", "#2f6b86"))
        lst = self.owner.get("/empresas/").get_data(as_text=True)
        self.assertIn('pill-tier-platina">Platinum', lst)
        self.assertIn('class="co-dot" style="--co: #2f6b86"', lst)
        # editar: nome, categoria e cor juntos; cor fora da paleta e categoria inventada viram "nenhuma"
        self.owner.post(f"/empresas/{company_id}/nome", {"name": "Hillside Property Services", "tier": "ouro", "color": "#a8324a"})
        row = self.query("SELECT tier, color FROM companies WHERE id = ?", (company_id,))[0]
        self.assertEqual((row["tier"], row["color"]), ("ouro", "#a8324a"))
        self.owner.post(f"/empresas/{slq['id']}/nome", {"name": "SLQ Properties", "tier": "vip", "color": "red"})
        row = self.query("SELECT tier, color FROM companies WHERE id = ?", (slq["id"],))[0]
        self.assertEqual((row["tier"], row["color"]), ("", ""))
        # a etiqueta da empresa ao lado do jardim usa a cor; a página do jardim mostra a categoria
        clients = self.owner.get("/clientes/").get_data(as_text=True)
        self.assertIn('pill-company" style="--co: #a8324a">Hillside Property Services', clients)
        detail = self.owner.get(f"/clientes/{elm}").get_data(as_text=True)
        self.assertIn('pill-tier-ouro">Gold', detail)
        # na área da empresa, as abas usam a cor dela e o nome vem com a bolinha; a prévia do dono também
        laura = self.portal_browser()
        for url in ("/portal/", "/portal/gardens", "/portal/report", "/portal/account"):
            page = laura.get(url).get_data(as_text=True)
            self.assertIn('portal-nav" aria-label="Company area" style="--co: #a8324a"', page, url)
        self.assertIn('class="co-dot" style="--co: #a8324a"></span>Hillside Property Services <span class="pill pill-tier-ouro">Gold', laura.get("/portal/").get_data(as_text=True))
        self.assertIn('style="--co: #a8324a"', self.owner.get(f"/empresas/{company_id}/previa/").get_data(as_text=True))
        # criar um login também deixa escolher a cor da empresa
        form = self.owner.get(f"/empresas/{company_id}/acessos/novo").get_data(as_text=True)
        self.assertIn('name="color" value="#a8324a" checked', form)
        self.assertNotIn('name="tier"', form)
        self.owner.post(f"/empresas/{company_id}/acessos/novo", {"name": "Tom", "email": "tom@hillside.co.uk", "language": "en",
                                                                 "password": "senha-do-tom-1", "color": "#6b4fa0"})
        self.assertEqual(self.query("SELECT color FROM companies WHERE id = ?", (company_id,))[0]["color"], "#6b4fa0")
        # a categoria platina existe pro cliente comum também
        self.owner.post(f"/clientes/{private}/editar", {"name": "Sítio das Flores", "tier": "platina"})
        self.assertIn('pill-tier-platina">Platinum', self.owner.get("/clientes/").get_data(as_text=True))

    def test_quote_can_be_for_a_company(self):
        company_id, elm, private, ana_id, _ = self.company_setup()
        form = self.owner.get("/cotacoes/nova").get_data(as_text=True)
        self.assertIn('<optgroup label="Companies">', form)
        self.assertIn(f'<option value="company:{company_id}">Hillside Property Services</option>', form)
        self.assertIn('"company:%d": {"address": "", "email": "laura@hillside.co.uk"' % company_id, form)
        # vindo da página da empresa, já vem escolhida e com nome e e-mail do acesso
        form = self.owner.get(f"/cotacoes/nova?company={company_id}").get_data(as_text=True)
        self.assertIn(f'<option value="company:{company_id}" selected>', form)
        self.assertIn('value="Hillside Property Services"', form)
        self.assertIn('value="laura@hillside.co.uk"', form)
        self.assertIn(f"/cotacoes/nova?company={company_id}", self.owner.get(f"/empresas/{company_id}").get_data(as_text=True))
        # salvar: nome e e-mail em branco vêm da empresa; a cotação fica ligada a ela
        q = self.new_quote(client_id=f"company:{company_id}", to_name="", to_email="", to_address="", to_postcode="")
        qid = q["id"]
        self.assertEqual((q["client_id"], q["company_id"], q["to_name"], q["to_email"]),
                         (None, company_id, "Hillside Property Services", "laura@hillside.co.uk"))
        detail = self.owner.get(f"/cotacoes/{qid}").get_data(as_text=True)
        self.assertIn(f'Company: <a href="/empresas/{company_id}">Hillside Property Services</a>', detail)
        self.assertIn(f'<option value="company:{company_id}" selected>', self.owner.get(f"/cotacoes/{qid}/editar").get_data(as_text=True))
        r = self.owner.post("/cotacoes/nova", {"client_id": "company:999", "to_name": "", "title": "X", "valid_until": self.day(30)})
        self.assertEqual(r.status_code, 200)  # empresa que não existe: volta pro formulário com o aviso
        self.assertIn("Pick a valid client or company.", r.get_data(as_text=True))
        # aceita: o cliente criado pra agendar já nasce ligado à empresa
        token = self.query("SELECT token FROM quotes WHERE id = ?", (qid,))[0]["token"]
        self.visitor().post(f"/c/{token}/resposta", {"answer": "accept", "name": "Laura Mills"})
        r = self.owner.post(f"/cotacoes/{qid}/agendar")
        self.assertEqual(r.status_code, 302)
        new_client = self.query("SELECT name, company_id FROM clients ORDER BY id DESC LIMIT 1")[0]
        self.assertEqual((new_client["name"], new_client["company_id"]), ("Hillside Property Services", company_id))

    def test_schedule_search_by_client_name(self):
        self.create_owner()
        rosa, elm = self.create_client("Sítio das Flores"), self.create_client("12 Elm Road")
        ana = self.create_employee()
        self.create_job(rosa, ana, self.day(1))
        self.create_job(elm, ana, self.day(2))
        self.create_job(elm, None, self.day(3))
        page = self.owner.get("/trabalhos?cliente=elm").get_data(as_text=True)
        self.assertIn("12 Elm Road", page)
        self.assertNotIn("Sítio das Flores", page)
        self.assertIn("Results for “elm”: 2.", page)
        self.assertIn('data-more="4" data-more-step="4"', page)  # abre com 4; "Mostrar mais 4" abre o resto
        # a busca respeita o filtro de funcionário e a aba; "%" e "_" não viram coringa
        page = self.owner.get("/trabalhos?cliente=elm&func=none").get_data(as_text=True)
        self.assertEqual(page.count("12 Elm Road"), 1)
        self.assertIn("No jobs for “%” in this view.", self.owner.get("/trabalhos?cliente=%25").get_data(as_text=True))
        self.assertIn("Sítio das Flores", self.owner.get("/trabalhos?cliente=s%C3%ADtio").get_data(as_text=True))

    def test_company_page_shows_only_the_latest_comments(self):
        company_id, elm, private, ana_id, _ = self.company_setup()
        job = self.create_job(elm, ana_id, self.day(0))
        self.set_times(job, "09:00", "10:00")
        laura = self.portal_browser()
        for i in range(1, 6):
            laura.post(f"/portal/service/{job}/comment", {"body": f"Company note number {i}"}).close()
        page = self.owner.get(f"/empresas/{company_id}").get_data(as_text=True)
        for i in range(1, 6):
            self.assertIn(f"Company note number {i}", page)
        self.assertLess(page.index("Company note number 5"), page.index("Company note number 1"))  # o mais novo primeiro
        # a lista abre com 3 e o botão "Show 5 more" vai abrindo o resto (script do base.html)
        self.assertIn('data-more="3" data-more-step="5" data-more-label="Show {n} more"', page)
        self.assertIn("Showing {shown} of {total}", page)

    def test_team_chat_on_a_job_stays_between_the_team(self):
        company_id, elm, private, ana_id, _ = self.company_setup()
        bruno = self.create_employee("Bruno", "bruno@example.com", "senha-do-bruno-1")
        gil = self.create_manager(perms=("schedule",))
        owner_id = self.query("SELECT id FROM users WHERE role = 'owner'")[0]["id"]
        job = self.create_job(elm, ana_id, self.day(0))
        ana, bruno_b = self.employee_browser(), self.employee_browser("bruno@example.com", "senha-do-bruno-1")
        # a Ana, escalada, escreve com uma foto; o Bruno, que não está no trabalho, nem abre a página
        r = ana.post(f"/trabalhos/{job}/equipe", {"body": "Gate code changed to 4321", "files": [(self._photo(), "gate.jpg")]})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(bruno_b.post(f"/trabalhos/{job}/equipe", {"body": "Hi"}).status_code, 404)
        page = ana.get(f"/trabalhos/{job}").get_data(as_text=True)
        self.assertIn("Gate code changed to 4321", page)
        self.assertIn("Team chat", page)
        file_id = self.query("SELECT id FROM comment_files")[0]["id"]
        self.assertIn(f"/trabalhos/{job}/equipe/arquivo/{file_id}", page)
        with ana.get(f"/trabalhos/{job}/equipe/arquivo/{file_id}?mini=1") as r:
            self.assertEqual((r.status_code, r.mimetype), (200, "image/jpeg"))
        self.assertEqual(bruno_b.get(f"/trabalhos/{job}/equipe/arquivo/{file_id}").status_code, 404)
        self.assertEqual(self.owner.get(f"/trabalhos/{job}/comentarios/arquivo/{file_id}").status_code, 404)  # não é da empresa
        # o dono e o gerente da agenda ficam sabendo; a própria Ana e o Bruno, não
        self.assertEqual(len(self.notices(owner_id, "team_comment")), 1)
        self.assertEqual(len(self.notices(gil, "team_comment")), 1)
        self.assertEqual(self.notices(ana_id, "team_comment"), [])
        self.assertEqual(self.notices(bruno, "team_comment"), [])
        bell = self.owner.get("/avisos/").get_data(as_text=True)
        self.assertIn("Ana wrote about: 12 Elm Road", bell)
        self.assertIn("Gate code changed to 4321", bell)
        self.assertIn(f"/trabalhos/{job}#equipe", bell)
        # o dono responde duas vezes: a Ana recebe um aviso só, com o texto da última
        self.owner.post(f"/trabalhos/{job}/equipe", {"body": "Thanks, noted."})
        self.owner.post(f"/trabalhos/{job}/equipe", {"body": "Also bring the long ladder."})
        mine = self.notices(ana_id, "team_comment")
        self.assertEqual(len(mine), 1)
        self.assertIn("Also bring the long ladder.", mine[0]["params"])
        self.assertEqual(self.notices(bruno, "team_comment"), [])
        self.assertIn("Write something or attach a file before sending.",
                      ana.post(f"/trabalhos/{job}/equipe", {"body": "  "}, follow_redirects=True).get_data(as_text=True))
        # a empresa nunca vê: nem na área dela, nem na conversa com ela na página do trabalho
        self.set_times(job, "09:00", "10:00")
        laura = self.portal_browser()
        service = laura.get(f"/portal/service/{job}").get_data(as_text=True)
        for private_bit in ("Gate code changed", "long ladder", "Thanks, noted"):
            self.assertNotIn(private_bit, service)
        self.assertEqual(laura.get(f"/portal/service/{job}/file/{file_id}").status_code, 404)
        laura.post(f"/portal/service/{job}/comment", {"body": "Lovely work"})
        owner_page = self.owner.get(f"/trabalhos/{job}").get_data(as_text=True)
        self.assertIn("Lovely work", owner_page)
        self.assertLess(owner_page.index("Gate code changed"), owner_page.index("Lovely work"))  # conversa da equipe vem antes
        self.assertEqual(self.query("SELECT COUNT(*) AS n FROM job_comments WHERE internal = 1")[0]["n"], 3)
        # apagar: a Ana apaga a dela (e o aviso some); não apaga a do dono; o dono apaga qualquer uma
        ids = [m["id"] for m in self.query("SELECT id FROM job_comments WHERE internal = 1 ORDER BY id")]
        self.assertEqual(ana.post(f"/trabalhos/{job}/equipe/{ids[1]}/apagar").status_code, 403)
        ana.post(f"/trabalhos/{job}/equipe/{ids[0]}/apagar")
        self.assertEqual(self.query("SELECT COUNT(*) AS n FROM comment_files")[0]["n"], 0)
        self.assertEqual(self.notices(owner_id, "team_comment"), [])
        self.owner.post(f"/trabalhos/{job}/equipe/{ids[1]}/apagar")
        self.assertEqual(self.query("SELECT COUNT(*) AS n FROM job_comments WHERE internal = 1")[0]["n"], 1)
        self.assertIn("Message deleted.", self.owner.get(f"/trabalhos/{job}").get_data(as_text=True))

    def test_company_area_report_sums_the_period_by_garden(self):
        company_id, elm, private, ana_id, _ = self.company_setup()
        oak = self.create_client("5 Oak Avenue")
        self.owner.post(f"/empresas/{company_id}/jardins", {"client_id": oak})
        today = self.day(0)
        week_start, _ = utils.week_bounds(today)
        j1 = self.create_job(elm, ana_id, week_start, tasks="Mow the lawn\nTrim the hedge\nTake photos\nWeed the path")
        ids = [t["id"] for t in self.query("SELECT id FROM job_tasks WHERE job_id = ? ORDER BY position, id", (j1,))]
        ana = self.employee_browser()
        ana.post(f"/trabalhos/{j1}/fotos/depois", {"photo": [(self._photo(), "b.jpg"), (self._photo(), "c.jpg")]})
        ana.post(f"/trabalhos/{j1}/atualizar", {
            "action": "finish", f"task_{ids[0]}": "done", f"task_{ids[1]}": "done", f"task_{ids[2]}": "done",
            f"task_{ids[3]}": "not_done", f"note_{ids[3]}": "Path blocked by a skip", "cash": "45"})
        self.set_times(j1, "09:00", "10:30")
        j2 = self.create_job(oak, ana_id, week_start, tasks="Mow the lawn")
        self.set_times(j2, "11:00", "11:45")
        self.create_job(elm, ana_id, today)  # ainda não concluído: fora do relatório
        other = self.create_job(private, ana_id, week_start)  # jardim particular: fora
        self.set_times(other, "12:00", "13:00")
        old = self.create_job(oak, ana_id, "2025-03-10")  # outro período
        self.set_times(old, "12:00", "13:00")

        laura = self.portal_browser()
        page = laura.get("/portal/report").get_data(as_text=True)
        for expected in ("2</b><span>services done", "2h15", "2</b><span>gardens visited", "of 2", "2</b><span>tasks done",
                         "1 not done", "12 Elm Road", "5 Oak Avenue", "1h30", "45 min", "Path blocked by a skip", "Weed the path",
                         "Services per day", f"/portal/?day={week_start}", "Time on site per garden"):
            self.assertIn(expected, page)
        for private_bit in ("Sítio das Flores", "£45", "Ana", "Take photos"):
            self.assertNotIn(private_bit, page)
        year = laura.get("/portal/report?periodo=ano&data=2025-06-01").get_data(as_text=True)
        self.assertIn("1</b><span>service done", year)
        self.assertIn("Services per month", year)
        self.assertIn("/portal/report?periodo=mes&amp;data=2025-03-01", year)
        self.assertIn("No services at your gardens in this period.", laura.get("/portal/report?periodo=mes&data=2020-01-01").get_data(as_text=True))
        self.assertEqual(laura.get("/portal/report?periodo=xyz&data=bobagem").status_code, 200)  # cai no padrão
        with laura.get(f"/portal/report/pdf?periodo=semana&data={today}") as r:
            self.assertEqual((r.status_code, r.mimetype), (200, "application/pdf"))
            self.assertTrue(r.data.startswith(b"%PDF"))
            self.assertIn("report-semana-", r.headers["Content-Disposition"])
            pdf = r.data
        if shutil.which("pdftotext"):
            text = subprocess.run(["pdftotext", "-", "-"], input=pdf, capture_output=True, check=True).stdout.decode()
            for expected in ("Hillside Property Services", "12 Elm Road", "2h15", "Path blocked by a skip"):
                self.assertIn(expected, text)
            self.assertNotIn("£45", text)
        with laura.get("/portal/report/pdf?periodo=ano&data=2020-01-01") as r:  # período vazio também gera
            self.assertEqual(r.status_code, 200)
        # a prévia do dono mostra o mesmo, pelos endereços da prévia; a equipe não entra na área da empresa
        preview = self.owner.get(f"/empresas/{company_id}/previa/relatorio").get_data(as_text=True)
        self.assertIn("2</b><span>services done", preview)
        self.assertIn(f"/empresas/{company_id}/previa/relatorio/pdf", preview)
        self.assertIn(f"/empresas/{company_id}/previa/?day={week_start}", preview)
        with self.owner.get(f"/empresas/{company_id}/previa/relatorio/pdf") as r:
            self.assertEqual(r.mimetype, "application/pdf")
        self.assertEqual(ana.get("/portal/report").status_code, 302)
        self.assertIn("/portal/report", laura.get("/portal/").get_data(as_text=True))  # a aba nova

    def test_company_comments_reach_the_owner_and_the_reply_goes_back(self):
        company_id, elm, private, ana_id, _ = self.company_setup()
        self.enable_email()
        gil = self.create_manager(perms=("schedule",))
        owner_id = self.query("SELECT id FROM users WHERE role = 'owner'")[0]["id"]
        job = self.create_job(elm, ana_id, self.day(0))
        self.set_times(job, "09:00", "10:00")
        pending = self.create_job(elm, ana_id, self.day(1))
        laura = self.portal_browser()
        empty = laura.post(f"/portal/service/{job}/comment", {"body": "   \n "}, follow_redirects=True)
        self.assertIn("Write something or attach a file before sending.", empty.get_data(as_text=True))
        self.assertEqual(laura.post(f"/portal/service/{pending}/comment", {"body": "Hello"}).status_code, 404)
        r = laura.post(f"/portal/service/{job}/comment", {"body": "Could you also clear the side path next time?"})
        self.assertEqual(r.status_code, 302)
        r.close()  # os avisos por e-mail saem depois da página
        # o dono e o gerente da agenda ficam sabendo; a funcionária não
        self.assertEqual(len(self.notices(owner_id, "company_comment")), 1)
        self.assertEqual(len(self.notices(gil, "company_comment")), 1)
        self.assertEqual(self.notices(ana_id, "company_comment"), [])
        self.assertEqual(sorted(m["To"] for m in FakeSMTP.sent), ["dono@example.com", "gil@example.com"])
        bell = self.owner.get("/avisos/").get_data(as_text=True)
        self.assertIn("Hillside Property Services commented: 12 Elm Road", bell)
        self.assertIn("Could you also clear the side path next time?", bell)
        self.assertIn(f"/trabalhos/{job}#comentarios", bell)
        self.assertIn("1 new comment", self.owner.get("/empresas/").get_data(as_text=True))
        # na página do trabalho: a conversa; abrir tira o "novo"
        page = self.owner.get(f"/trabalhos/{job}").get_data(as_text=True)
        self.assertIn("Comments · Hillside Property Services", page)
        self.assertIn("Could you also clear the side path next time?", page)
        self.assertIsNotNone(self.query("SELECT seen_at FROM job_comments")[0]["seen_at"])
        self.assertNotIn("new comment", self.owner.get("/empresas/").get_data(as_text=True))
        # a funcionária não vê a conversa nem responde
        ana = self.employee_browser()
        self.assertNotIn("Hillside", ana.get(f"/trabalhos/{job}").get_data(as_text=True))
        self.assertEqual(ana.post(f"/trabalhos/{job}/comentarios", {"body": "Oi"}).status_code, 403)
        # o dono responde: aparece na área dela e vai por e-mail, no idioma dela
        FakeSMTP.sent.clear()
        r = self.owner.post(f"/trabalhos/{job}/comentarios", {"body": "Sure, we'll do it on Thursday."})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(FakeSMTP.sent, [])  # o dono não espera pelo e-mail
        r.close()
        self.assertEqual(len(FakeSMTP.sent), 1)
        mail = FakeSMTP.sent[0]
        self.assertEqual(mail["To"], "laura@hillside.co.uk")
        self.assertEqual(mail["Subject"], "Gestão de Jardinagem replied: 12 Elm Road")
        self.assertIn("Sure, we'll do it on Thursday.", mail.get_content())
        self.assertIn(f"/portal/service/{job}#comments", mail.get_content())
        day = laura.get("/portal/").get_data(as_text=True)
        self.assertIn("New reply", day)
        self.assertIn("New replies from", day)
        self.assertIn("Sure, we", laura.get(f"/portal/service/{job}").get_data(as_text=True))
        self.assertNotIn("New reply", laura.get("/portal/").get_data(as_text=True))  # ela abriu: deixou de ser nova
        # resposta num serviço que ela ainda não vê: fica guardada, sem e-mail
        FakeSMTP.sent.clear()
        r = self.owner.post(f"/trabalhos/{pending}/comentarios", {"body": "Heads up for tomorrow"}, follow_redirects=True)
        self.assertIn("once the service is finished", r.get_data(as_text=True))
        self.assertEqual(FakeSMTP.sent, [])
        # só o dono apaga comentário
        reply_id = self.query("SELECT id FROM job_comments WHERE job_id = ? AND from_company = 0", (job,))[0]["id"]
        gil_b = self.employee_browser("gil@example.com", "senha-do-gil-1")
        self.assertEqual(gil_b.post(f"/trabalhos/{job}/comentarios/{reply_id}/apagar").status_code, 403)
        self.owner.post(f"/trabalhos/{job}/comentarios/{reply_id}/apagar")
        self.assertEqual([m["from_company"] for m in self.query("SELECT * FROM job_comments WHERE job_id = ?", (job,))], [1])

    def test_company_logins_are_separate_from_the_team(self):
        company_id, elm, private, ana_id, _ = self.company_setup()
        # o mesmo e-mail não serve pra equipe e pra empresa (a tela de login é uma só)
        page = self.owner.post(f"/empresas/{company_id}/acessos/novo", {
            "name": "X", "email": "ana@example.com", "language": "en", "password": "senha-qualquer-1"}).get_data(as_text=True)
        self.assertIn("already used to log in", page)
        self.owner.post("/equipe/novo", {"name": "Laura", "email": "LAURA@hillside.co.uk", "password": "senha-da-laura-2",
                                         "role": "employee"})
        self.assertEqual(self.query("SELECT COUNT(*) AS n FROM users WHERE email = 'laura@hillside.co.uk'")[0]["n"], 0)
        # a conta dela: senha e idioma
        laura = self.portal_browser()
        self.assertIn("Laura Mills", laura.get("/portal/account").get_data(as_text=True))
        wrong = laura.post("/portal/account", {"action": "password", "current_password": "errada",
                                               "new_password": "nova-senha-123", "confirm_password": "nova-senha-123"})
        self.assertIn("The current password is incorrect.", wrong.get_data(as_text=True))
        laura.post("/portal/account", {"action": "language", "language": "pt_BR"})
        self.assertIn("Serviços", laura.get("/portal/").get_data(as_text=True))
        # esqueceu a senha: o dono gera outra (a mensagem vai no idioma dela) e a antiga para de valer
        login_id = self.query("SELECT id FROM company_users")[0]["id"]
        page = self.owner.post(f"/empresas/{company_id}/acessos/{login_id}/senha").get_data(as_text=True)
        new_password = re.search(r'<code class="secret">([a-z0-9]+)</code>', page).group(1)
        self.assertIn("Olá, Laura!", page)
        self.assertEqual(Browser(self.app).login("laura@hillside.co.uk", "senha-da-laura-1").status_code, 200)
        laura = self.portal_browser(password=new_password)
        # um comentário dela fica mesmo se o acesso for excluído
        job = self.create_job(elm, ana_id, self.day(0))
        self.set_times(job, "09:00", "10:00")
        laura.post(f"/portal/service/{job}/comment", {"body": "Lovely work"})
        # desativado: a sessão cai e o login não entra
        self.owner.post(f"/empresas/{company_id}/acessos/{login_id}/ativo")
        self.assertEqual(laura.get("/portal/").status_code, 302)
        self.assertEqual(Browser(self.app).login("laura@hillside.co.uk", new_password).status_code, 200)
        self.owner.post(f"/empresas/{company_id}/acessos/{login_id}/excluir")
        self.assertEqual(self.query("SELECT author_name, company_user_id FROM job_comments"),
                         [{"author_name": "Laura Mills", "company_user_id": None}])
        self.assertIn("Laura Mills", self.owner.get(f"/trabalhos/{job}").get_data(as_text=True))
        # excluir a empresa: os jardins continuam, sem empresa
        self.owner.post(f"/empresas/{company_id}/acessos/novo", {"name": "Tom", "email": "tom@hillside.co.uk",
                                                                  "language": "en", "password": "senha-do-tom-1"})
        self.owner.post(f"/empresas/{company_id}/excluir")
        self.assertIsNone(self.query("SELECT company_id FROM clients WHERE id = ?", (elm,))[0]["company_id"])
        self.assertEqual(self.query("SELECT COUNT(*) AS n FROM company_users")[0]["n"], 0)
        self.assertEqual(self.owner.get(f"/trabalhos/{job}").status_code, 200)

    def test_owner_preview_and_who_manages_companies(self):
        company_id, elm, private, ana_id, _ = self.company_setup()
        job = self.create_job(elm, ana_id, self.day(0))
        self.set_times(job, "09:00", "10:30")
        self.owner.post(f"/trabalhos/{job}/comentarios", {"body": "Gate was locked, we used the side door."})
        preview = self.owner.get(f"/empresas/{company_id}/previa/").get_data(as_text=True)
        self.assertIn("Preview: this is what Hillside Property Services sees", preview)
        self.assertIn("09:00–10:30 · 1h30", preview)
        self.assertIn(f"/empresas/{company_id}/previa/servico/{job}", preview)
        page = self.owner.get(f"/empresas/{company_id}/previa/servico/{job}").get_data(as_text=True)
        self.assertIn("Gate was locked", page)
        self.assertNotIn('name="body"', page)  # na prévia não se comenta
        self.assertIsNone(self.query("SELECT seen_at FROM job_comments")[0]["seen_at"])  # e não conta como lida pela empresa
        for url in (f"/empresas/{company_id}/previa/jardins", f"/empresas/{company_id}/previa/jardins/{elm}", "/empresas/",
                    f"/empresas/{company_id}", f"/empresas/{company_id}/acessos/novo", f"/clientes/{elm}", "/clientes/novo"):
            self.assertEqual(self.owner.get(url).status_code, 200, url)
        self.assertEqual(self.owner.get(f"/empresas/{company_id}/previa/jardins/{private}").status_code, 404)
        self.assertEqual(self.owner.get(f"/empresas/{company_id}/previa/servico/{job + 99}").status_code, 404)
        # só o dono mexe em empresas (nem o gerente com Clientes e a agenda)
        self.create_manager(perms=("clients", "schedule"))
        gil = self.employee_browser("gil@example.com", "senha-do-gil-1")
        for url in ("/empresas/", f"/empresas/{company_id}", f"/empresas/{company_id}/previa/"):
            self.assertEqual(gil.get(url).status_code, 403, url)
        self.assertEqual(gil.post("/empresas/nova", {"name": "Outra"}).status_code, 403)
        # o gerente edita o jardim, mas não muda a empresa dele (nem vê a escolha)
        self.assertNotIn('name="company_id"', gil.get(f"/clientes/{elm}/editar").get_data(as_text=True))
        gil.post(f"/clientes/{elm}/editar", {"name": "12 Elm Road", "postcode": "SE1 1AA"})
        self.assertEqual(self.query("SELECT company_id FROM clients WHERE id = ?", (elm,))[0]["company_id"], company_id)
        # o dono escolhe no cadastro, e a lista de clientes mostra de quem é o jardim
        self.owner.post(f"/clientes/{private}/editar", {"name": "Sítio das Flores", "company_id": str(company_id)})
        self.assertEqual(self.query("SELECT company_id FROM clients WHERE id = ?", (private,))[0]["company_id"], company_id)
        self.assertIn('pill-company">Hillside Property Services', self.owner.get("/clientes/").get_data(as_text=True))
        self.owner.post(f"/clientes/{private}/editar", {"name": "Sítio das Flores", "company_id": "999"})
        self.assertIsNone(self.query("SELECT company_id FROM clients WHERE id = ?", (private,))[0]["company_id"])
        self.owner.post("/clientes/novo", {"name": "3 Oak Close", "company_id": str(company_id)})
        self.assertEqual(self.query("SELECT company_id FROM clients WHERE name = '3 Oak Close'")[0]["company_id"], company_id)
        # tirar o jardim da empresa: ela deixa de ver os serviços dele
        self.owner.post(f"/empresas/{company_id}/jardins/{elm}/tirar")
        self.assertEqual(self.portal_browser().get(f"/portal/service/{job}").status_code, 404)

    def test_company_conversation_stays_with_its_company(self):
        """O jardim passou de uma empresa pra outra: a nova não vê o que foi conversado com a antiga."""
        hillside, elm, private, ana_id, _ = self.company_setup()
        self.enable_email()
        job = self.create_job(elm, ana_id, self.day(0))
        self.set_times(job, "09:00", "10:00")
        laura = self.portal_browser()
        laura.post(f"/portal/service/{job}/comment", {"body": "Hillside only: the tenant is moving out."})
        self.owner.post(f"/trabalhos/{job}/comentarios", {"body": "Noted, thanks Laura."})
        self.owner.post("/empresas/nova", {"name": "Brick Lettings"})
        brick = self.query("SELECT id FROM companies WHERE name = 'Brick Lettings'")[0]["id"]
        self.owner.post(f"/empresas/{brick}/acessos/novo", {"name": "Bob Brick", "email": "bob@brick.co.uk", "language": "en",
                                                            "password": "senha-do-bob-1"})
        self.owner.post(f"/empresas/{brick}/jardins", {"client_id": elm})  # o 12 Elm Road agora é da Brick
        bob = self.portal_browser("bob@brick.co.uk", "senha-do-bob-1")
        page = bob.get(f"/portal/service/{job}").get_data(as_text=True)
        self.assertEqual(bob.get(f"/portal/service/{job}").status_code, 200)
        for private_bit in ("tenant is moving out", "Noted, thanks Laura", "Laura", "Hillside"):
            self.assertNotIn(private_bit, page)
        day = bob.get("/portal/").get_data(as_text=True)
        self.assertNotIn("svc-comments", day)  # nem a contagem de comentários aparece pra ele
        self.assertNotIn("New repl", day)
        self.assertEqual(laura.get(f"/portal/service/{job}").status_code, 404)
        # a equipe vê a conversa inteira, cada comentário com a empresa certa; a resposta de agora vai só pra Brick
        staff = self.owner.get(f"/trabalhos/{job}").get_data(as_text=True)
        self.assertIn("Comments · Brick Lettings", staff)
        self.assertIn("Laura Mills</b> · Hillside Property Services", staff)
        FakeSMTP.sent.clear()
        r = self.owner.post(f"/trabalhos/{job}/comentarios", {"body": "Hi Bob, welcome!"})
        r.close()
        self.assertEqual([m["To"] for m in FakeSMTP.sent], ["bob@brick.co.uk"])
        self.assertIn("Hi Bob, welcome!", bob.get(f"/portal/service/{job}").get_data(as_text=True))
        self.assertNotIn("Hi Bob", self.owner.get(f"/empresas/{hillside}").get_data(as_text=True))
        self.assertIn("tenant is moving out", self.owner.get(f"/empresas/{hillside}").get_data(as_text=True))

    def test_company_sessions_end_with_a_new_password_or_when_disabled(self):
        company_id, elm, private, ana_id, _ = self.company_setup()
        login_id = self.query("SELECT id FROM company_users")[0]["id"]
        phone, laptop = self.portal_browser(), self.portal_browser()
        # o dono gera uma senha nova: quem estava logado com a antiga sai
        page = self.owner.post(f"/empresas/{company_id}/acessos/{login_id}/senha").get_data(as_text=True)
        password = re.search(r'<code class="secret">([a-z0-9]+)</code>', page).group(1)
        self.assertEqual(phone.get("/portal/").status_code, 302)
        phone, laptop = self.portal_browser(password=password), self.portal_browser(password=password)
        # ela troca a própria senha: continua logada aqui, o outro aparelho sai
        r = phone.post("/portal/account", {"action": "password", "current_password": password,
                                           "new_password": "nova-senha-123", "confirm_password": "nova-senha-123"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(phone.get("/portal/").status_code, 200)
        self.assertEqual(laptop.get("/portal/").status_code, 302)
        # desativar e reativar sem ela entrar no meio: a sessão antiga não volta
        self.owner.post(f"/empresas/{company_id}/acessos/{login_id}/ativo")
        self.owner.post(f"/empresas/{company_id}/acessos/{login_id}/ativo")
        self.assertEqual(phone.get("/portal/").status_code, 302)
        self.assertEqual(self.portal_browser(password="nova-senha-123").get("/portal/").status_code, 200)

    def test_odd_addresses_give_not_found_instead_of_errors(self):
        company_id, elm, private, ana_id, _ = self.company_setup()
        laura = self.portal_browser()
        huge = "9" * 20
        self.assertEqual(laura.get(f"/portal/service/{huge}").status_code, 404)
        self.assertEqual(laura.get(f"/portal/service/{'9' * 5000}").status_code, 404)
        self.assertEqual(laura.get(f"/portal/gardens/{elm}?page=614891469123651721").status_code, 200)
        self.assertEqual(laura.post(f"/portal/service/{huge}/comment", {"body": "x"}).status_code, 404)
        self.assertEqual(self.owner.get(f"/trabalhos/{huge}").status_code, 404)
        self.assertEqual(self.owner.post(f"/empresas/{company_id}/jardins", {"client_id": huge}).status_code, 404)
        # ?next= com tab ou quebra de linha não vira endereço de fora nem erro
        b = Browser(self.app)
        r = b.post("/login?next=/%09/evil.example", {"email": "dono@example.com", "password": "senha-do-dono-1"})
        self.assertEqual(r.headers["Location"], "/")
        b = Browser(self.app)
        r = b.post("/login?next=/portal/%0D%0A", {"email": "laura@hillside.co.uk", "password": "senha-da-laura-1"})
        self.assertEqual((r.status_code, r.headers["Location"]), (302, "/portal/"))

    def test_company_comments_have_limits_and_one_notice_per_service(self):
        company_id, elm, private, ana_id, _ = self.company_setup()
        owner_id = self.query("SELECT id FROM users WHERE role = 'owner'")[0]["id"]
        job = self.create_job(elm, ana_id, self.day(0))
        self.set_times(job, "09:00", "10:00")
        laura = self.portal_browser()
        laura.post(f"/portal/service/{job}/comment", {"body": "First thing"})
        laura.post(f"/portal/service/{job}/comment", {"body": "Second thing"})
        notices = self.notices(owner_id, "company_comment")
        self.assertEqual(len(notices), 1)  # um aviso só enquanto ele não lê, com o último comentário
        self.assertIn("Second thing", notices[0]["params"])
        # apagar o comentário apaga o aviso com o texto dele
        second = self.query("SELECT id FROM job_comments WHERE body = 'Second thing'")[0]["id"]
        self.owner.post(f"/trabalhos/{job}/comentarios/{second}/apagar")
        self.assertEqual(self.notices(owner_id, "company_comment"), [])
        # no máximo 20 por hora (o apagado não conta: sobrou 1, mais 19 = 20)
        for i in range(19):
            laura.post(f"/portal/service/{job}/comment", {"body": f"Comment {i}"})
        page = laura.post(f"/portal/service/{job}/comment", {"body": "One too many"}, follow_redirects=True)
        self.assertIn("Too many comments in a short time.", page.get_data(as_text=True))
        self.assertEqual(self.query("SELECT COUNT(*) AS n FROM job_comments WHERE body = 'One too many'")[0]["n"], 0)
        # (resumo do dia: testes logo abaixo)
        # só abrir a página de verdade marca a resposta como lida (HEAD não conta)
        self.owner.post(f"/trabalhos/{job}/comentarios", {"body": "Reply"})
        laura.c.head(f"/portal/service/{job}")
        self.assertIsNone(self.query("SELECT seen_at FROM job_comments WHERE body = 'Reply'")[0]["seen_at"])
        laura.get(f"/portal/service/{job}")
        self.assertIsNotNone(self.query("SELECT seen_at FROM job_comments WHERE body = 'Reply'")[0]["seen_at"])

    # ---------- resumo do dia por e-mail pra empresa ----------

    def mail_clock_at(self, hour, minute=0, days=0):
        base = datetime.now(ZoneInfo("Europe/London")).replace(hour=hour, minute=minute, second=0, microsecond=0)
        self.mail_now[0] = base + timedelta(days=days)

    def visit(self):
        """Uma visita qualquer ao app: depois que a página sai, o resumo sai se estiver na hora."""
        self.owner.get("/painel").close()

    def summaries(self):
        return [m for m in FakeSMTP.sent if "service report" in m["Subject"] or "relatório dos serviços" in m["Subject"]]

    def test_daily_summary_goes_out_once_after_the_chosen_hour(self):
        company_id, elm, private, ana_id, _ = self.company_setup()
        self.enable_email()
        today = self.day(0)
        job = self.create_job(elm, ana_id, today, tasks="Mow the lawn\nWeed the path\nTake the photos")
        ids = [t["id"] for t in self.query("SELECT id FROM job_tasks WHERE job_id = ? ORDER BY position, id", (job,))]
        ana = self.employee_browser()
        ana.post(f"/trabalhos/{job}/fotos/antes", {"photo": (self._photo(), "a.jpg")})
        ana.post(f"/trabalhos/{job}/atualizar", {
            "action": "finish", f"task_{ids[0]}": "done", f"task_{ids[1]}": "not_done", f"note_{ids[1]}": "Path flooded",
            f"task_{ids[2]}": "done", "materials": "2 bags of green waste", "employee_notes": "Recado interno", "cash": "40"})
        self.set_times(job, "10:00", "11:30")
        other = self.create_job(private, ana_id, today)  # jardim particular: nunca vai pra empresa
        self.set_times(other, "12:00", "13:00")
        FakeSMTP.sent.clear()
        self.mail_clock_at(17, 30)
        self.visit()
        self.assertEqual(self.summaries(), [])  # antes da hora: nada
        self.mail_clock_at(18, 5)
        self.visit()
        mails = self.summaries()
        self.assertEqual([m["To"] for m in mails], ["laura@hillside.co.uk"])
        with self.app.test_request_context():
            self.assertEqual(mails[0]["Subject"], f"Gestão de Jardinagem · service report · {utils.date_long(today)}")
        body = mails[0].get_content()
        for bit in ("Hi Laura,", "Here is what we did today at your gardens: 1 service, 1h30 on site.", "12 Elm Road",
                    "10:00–11:30 (1h30)", "✓ Mow the lawn", "✗ Weed the path – not done: Path flooded",
                    "Materials: 2 bags of green waste", "Photos: 1 before, 0 after", f"/portal/service/{job}",
                    f"/portal/?day={today}", "untick it under Account"):
            self.assertIn(bit, body)
        for private_bit in ("Sítio das Flores", "£40", "Recado interno", "Ana", "Take the photos"):
            self.assertNotIn(private_bit, body)
        # no mesmo dia não sai de novo, nem com serviço novo (ele vai no de amanhã)
        late = self.create_job(elm, ana_id, today)
        self.set_times(late, "19:00", "19:40")
        self.mail_clock_at(19, 45)
        self.visit()
        self.assertEqual(len(self.summaries()), 1)
        self.mail_clock_at(18, 5, days=1)
        self.visit()
        second = self.summaries()[1].get_content()
        self.assertIn("19:00–19:40", second)
        self.assertIn("since the last report", second)
        self.assertNotIn("Mow the lawn", second)
        # sem serviço novo não sai nada; e depois das 22h também não
        self.mail_clock_at(18, 5, days=2)
        self.visit()
        self.assertEqual(len(self.summaries()), 2)
        night = self.create_job(elm, ana_id, self.day(3))
        self.set_times(night, "15:00", "16:00")
        self.mail_clock_at(22, 30, days=3)
        self.visit()
        self.assertEqual(len(self.summaries()), 2)
        # quem desliga na conta dela não recebe; o outro acesso, em português, recebe no idioma dele
        self.owner.post(f"/empresas/{company_id}/acessos/novo", {"name": "Tomás Reis", "email": "tomas@hillside.co.uk",
                                                                  "language": "pt_BR", "password": "senha-do-tomas-1"})
        laura = self.portal_browser()
        self.assertIn('name="daily_mail" value="1" checked', laura.get("/portal/account").get_data(as_text=True))
        laura.post("/portal/account", {"action": "daily"})  # caixa desmarcada
        self.assertEqual(self.query("SELECT daily_mail FROM company_users WHERE email = 'laura@hillside.co.uk'")[0]["daily_mail"], 0)
        self.mail_clock_at(18, 5, days=4)
        self.visit()
        third = self.summaries()[2]
        self.assertEqual(third["To"], "tomas@hillside.co.uk")
        self.assertIn("relatório dos serviços", third["Subject"])
        self.assertIn("Olá, Tomás!", third.get_content())
        self.assertIn("15:00–16:00 (1h00)", third.get_content())  # o que ficou das 22h30 foi no dia seguinte
        self.assertEqual(len(self.summaries()), 3)

    def test_daily_summary_settings_preview_send_now_and_daily_link(self):
        company_id, elm, private, ana_id, _ = self.company_setup()
        job = self.create_job(elm, ana_id, self.day(0))
        self.set_times(job, "09:00", "10:00")
        # sem e-mail configurado: a página avisa, mostra a prévia, e nada sai
        page = self.owner.get(f"/empresas/{company_id}").get_data(as_text=True)
        self.assertIn("set up yet", page)
        self.assertIn("See the next one (1 service)", page)
        self.assertIn("Hi Laura,", page)
        self.assertIn("Goes to: Laura Mills", page)
        r = self.owner.post(f"/empresas/{company_id}/resumo/mandar", follow_redirects=True)
        self.assertIn("The summary goes out through the Gmail", r.get_data(as_text=True))
        self.enable_email()
        FakeSMTP.sent.clear()
        # desligado: não sai nem depois da hora
        self.owner.post(f"/empresas/{company_id}/resumo", {"daily_hour": ""})
        self.assertIsNone(self.query("SELECT daily_hour FROM companies")[0]["daily_hour"])
        self.mail_clock_at(18, 5)
        self.visit()
        self.assertEqual(self.summaries(), [])
        # às 19h: às 18h30 ainda não, às 19h05 sai
        self.owner.post(f"/empresas/{company_id}/resumo", {"daily_hour": "19"})
        self.mail_clock_at(18, 30)
        self.visit()
        self.assertEqual(self.summaries(), [])
        self.mail_clock_at(19, 5)
        self.visit()
        self.assertEqual(len(self.summaries()), 1)
        self.assertIn("has gone out", self.owner.get(f"/empresas/{company_id}").get_data(as_text=True))
        # "Mandar agora": sem nada novo, avisa; com serviço novo, manda na hora (mesmo já tendo saído hoje)
        r = self.owner.post(f"/empresas/{company_id}/resumo/mandar", follow_redirects=True)
        self.assertIn("Nothing to send", r.get_data(as_text=True))
        extra = self.create_job(elm, ana_id, self.day(0))
        self.set_times(extra, "11:00", "11:45")
        r = self.owner.post(f"/empresas/{company_id}/resumo/mandar", follow_redirects=True)
        self.assertIn("Summary sent to 1 person.", r.get_data(as_text=True))
        self.assertEqual(len(self.summaries()), 2)
        self.assertIn("11:00–11:45", self.summaries()[1].get_content())
        # o link diário (cron-job.org) manda na hora certa, uma vez por dia
        key = self.query("SELECT cron_key FROM settings")[0]["cron_key"] or None
        if not key:
            self.owner.get("/conta/avisos")
            key = self.query("SELECT cron_key FROM settings")[0]["cron_key"]
        more = self.create_job(elm, ana_id, self.day(0))
        self.set_times(more, "12:00", "12:30")
        self.mail_clock_at(19, 10, days=1)
        text = self.owner.get(f"/avisos/diario/{key}").get_data(as_text=True)
        self.assertIn("1 resumo(s) pra empresa", text)
        text = self.owner.get(f"/avisos/diario/{key}").get_data(as_text=True)
        self.assertIn("0 resumo(s) pra empresa", text)
        # só o dono mexe no resumo
        self.create_manager(perms=("clients", "schedule"))
        gil = self.employee_browser("gil@example.com", "senha-do-gil-1")
        self.assertEqual(gil.post(f"/empresas/{company_id}/resumo", {"daily_hour": ""}).status_code, 403)
        self.assertEqual(gil.post(f"/empresas/{company_id}/resumo/mandar").status_code, 403)


    # ---------- anexos nos comentários ----------

    def test_comments_carry_photos_and_documents_both_ways(self):
        import io
        company_id, elm, private, ana_id, _ = self.company_setup()
        job = self.create_job(elm, ana_id, self.day(0))
        self.set_times(job, "09:00", "10:00")
        laura = self.portal_browser()
        pdf = (io.BytesIO(b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"), "Tenancy notice.pdf")
        fake_pdf = (io.BytesIO(b"MZ this is not a pdf"), "virus.pdf")
        exe = (io.BytesIO(b"MZ\x90\x00"), "setup.exe")
        page = laura.post(f"/portal/service/{job}/comment", {
            "body": "Here is the notice and a photo of the gate.",
            "files": [(self._photo(), "gate.jpg"), pdf, fake_pdf, exe]}, follow_redirects=True).get_data(as_text=True)
        self.assertIn("Comment sent.", page)
        self.assertIn("2 files were left out: only photos, PDF, Word or Excel up to 10 MB.", page)
        files = self.query("SELECT * FROM comment_files ORDER BY id")
        self.assertEqual([(f["kind"], f["original"]) for f in files], [("photo", "gate.jpg"), ("doc", "Tenancy notice.pdf")])
        photo, doc = files
        folder = Path(self.app.config["UPLOAD_ROOT"]) / "comments" / str(job)
        self.assertTrue((folder / photo["filename"]).exists() and (folder / doc["filename"]).exists())
        # a Laura vê os dois na conversa e abre; a foto sai reduzida, o PDF abre no navegador
        self.assertIn(f"/portal/service/{job}/file/{photo['id']}?mini=1", page)
        self.assertIn("Tenancy notice.pdf", page)
        r = laura.get(f"/portal/service/{job}/file/{photo['id']}")
        self.assertEqual((r.status_code, r.mimetype), (200, "image/jpeg"))
        r.close()
        r = laura.get(f"/portal/service/{job}/file/{doc['id']}")
        self.assertEqual((r.status_code, r.mimetype), (200, "application/pdf"))
        self.assertTrue(r.headers["Content-Disposition"].startswith("inline"))
        r.close()
        # o dono também abre (pela página do trabalho), e responde com uma planilha
        staff = self.owner.get(f"/trabalhos/{job}").get_data(as_text=True)
        self.assertIn(f"/trabalhos/{job}/comentarios/arquivo/{doc['id']}", staff)
        r = self.owner.get(f"/trabalhos/{job}/comentarios/arquivo/{doc['id']}")
        self.assertEqual(r.status_code, 200)
        r.close()
        xlsx = (io.BytesIO(b"PK\x03\x04 planilha de mentira"), "Quote.xlsx")
        self.owner.post(f"/trabalhos/{job}/comentarios", {"body": "", "files": [xlsx]})
        sheet = self.query("SELECT * FROM comment_files WHERE original = 'Quote.xlsx'")[0]
        r = laura.get(f"/portal/service/{job}/file/{sheet['id']}")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.headers["Content-Disposition"].startswith("attachment"))  # Word e Excel baixam
        r.close()
        self.assertEqual(self.query("SELECT body FROM job_comments WHERE from_company = 0")[0]["body"], "")  # só o anexo
        # quem não é da conversa não abre: outra empresa, a funcionária, alguém de fora
        self.owner.post("/empresas/nova", {"name": "Brick Lettings"})
        brick = self.query("SELECT id FROM companies WHERE name = 'Brick Lettings'")[0]["id"]
        self.owner.post(f"/empresas/{brick}/acessos/novo", {"name": "Bob", "email": "bob@brick.co.uk", "language": "en",
                                                            "password": "senha-do-bob-1"})
        bob = self.portal_browser("bob@brick.co.uk", "senha-do-bob-1")
        self.assertEqual(bob.get(f"/portal/service/{job}/file/{doc['id']}").status_code, 404)
        self.assertEqual(self.employee_browser().get(f"/trabalhos/{job}/comentarios/arquivo/{doc['id']}").status_code, 403)
        self.assertEqual(Browser(self.app).get(f"/portal/service/{job}/file/{doc['id']}").status_code, 302)
        # só anexo que não serve: nada é gravado; mais de 6: nada é gravado
        before = len(self.query("SELECT id FROM job_comments"))
        page = laura.post(f"/portal/service/{job}/comment", {"body": "", "files": [(io.BytesIO(b"x"), "a.exe")]},
                          follow_redirects=True).get_data(as_text=True)
        self.assertIn("1 file was left out", page)
        many = [(self._photo(), f"p{i}.jpg") for i in range(7)]
        page = laura.post(f"/portal/service/{job}/comment", {"body": "lots", "files": many}, follow_redirects=True).get_data(as_text=True)
        self.assertIn("Up to 6 files per comment.", page)
        self.assertEqual(len(self.query("SELECT id FROM job_comments")), before)
        # apagar o comentário apaga os arquivos; excluir o trabalho apaga a pasta
        first = self.query("SELECT id FROM job_comments ORDER BY id")[0]["id"]
        self.owner.post(f"/trabalhos/{job}/comentarios/{first}/apagar")
        self.assertFalse((folder / photo["filename"]).exists() or (folder / doc["filename"]).exists())
        self.assertEqual(self.query("SELECT COUNT(*) AS n FROM comment_files WHERE comment_id = ?", (first,))[0]["n"], 0)
        self.owner.post(f"/trabalhos/{job}/excluir")
        self.assertFalse(folder.exists())

    def test_file_only_comment_notifies_with_the_file_names(self):
        company_id, elm, private, ana_id, _ = self.company_setup()
        owner_id = self.query("SELECT id FROM users WHERE role = 'owner'")[0]["id"]
        job = self.create_job(elm, ana_id, self.day(0))
        self.set_times(job, "09:00", "10:00")
        self.portal_browser().post(f"/portal/service/{job}/comment", {"files": [(self._photo(), "broken fence.jpg")]})
        notice = self.notices(owner_id, "company_comment")[0]
        self.assertIn("📎 broken fence.jpg", notice["params"])
        self.assertIn("broken fence.jpg", self.owner.get("/avisos/").get_data(as_text=True))

def utils_today(app):
    with app.test_request_context():
        return utils.today_iso()


if __name__ == "__main__":
    unittest.main()
