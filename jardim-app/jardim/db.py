"""Conexão com o SQLite e migrações do banco."""
import sqlite3
from pathlib import Path

from flask import current_app, g

# Para mudar a estrutura do banco depois (nova coluna, nova tabela), NÃO mexa no schema.sql.
# Adicione aqui um novo item ao final da lista; ele roda uma única vez, na ordem.
# Exemplo:  "ALTER TABLE clients ADD COLUMN whatsapp TEXT NOT NULL DEFAULT ''",
MIGRATIONS = [
    "ALTER TABLE clients ADD COLUMN tier TEXT NOT NULL DEFAULT '';",
    "ALTER TABLE settings ADD COLUMN notify_email INTEGER NOT NULL DEFAULT 0;",
    "ALTER TABLE settings ADD COLUMN mail_address TEXT NOT NULL DEFAULT '';",
    "ALTER TABLE settings ADD COLUMN mail_password TEXT NOT NULL DEFAULT '';",
    "ALTER TABLE settings ADD COLUMN cron_key TEXT NOT NULL DEFAULT '';",
    "ALTER TABLE settings ADD COLUMN last_daily_run TEXT NOT NULL DEFAULT '';",
    "ALTER TABLE clients ADD COLUMN task_template TEXT NOT NULL DEFAULT '';",  # tarefas padrão do local
    "ALTER TABLE job_tasks ADD COLUMN skipped INTEGER NOT NULL DEFAULT 0;",   # 1 = marcada como não feita
    "ALTER TABLE job_tasks ADD COLUMN note TEXT NOT NULL DEFAULT '';",        # motivo de não ter sido feita
    "ALTER TABLE jobs ADD COLUMN cash_pence INTEGER;",                         # dinheiro recebido do cliente, em pence
    "ALTER TABLE jobs ADD COLUMN cash_in_report INTEGER NOT NULL DEFAULT 1;",  # 1 = aparece no relatório do WhatsApp
    "ALTER TABLE jobs ADD COLUMN cash_received_at TEXT;",                      # quando o dono recebeu o dinheiro
    "ALTER TABLE users ADD COLUMN language TEXT NOT NULL DEFAULT '';",         # idioma escolhido pela pessoa ('' = padrão do app)
    # Cargo Gerente: o SQLite não deixa mudar a regra (CHECK) de uma coluna, então a tabela de pessoas é
    # recriada do jeito que a documentação do SQLite manda: referências desligadas durante a troca (senão
    # apagar a tabela velha levaria junto os avisos), tudo copiado, e a nova assume o nome da antiga.
    """
    PRAGMA foreign_keys = OFF;
    BEGIN;
    CREATE TABLE users_new (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        name          TEXT NOT NULL,
        email         TEXT NOT NULL UNIQUE COLLATE NOCASE,
        password_hash TEXT NOT NULL,
        role          TEXT NOT NULL CHECK (role IN ('owner', 'manager', 'employee')),
        phone         TEXT NOT NULL DEFAULT '',
        active        INTEGER NOT NULL DEFAULT 1,
        created_at    TEXT NOT NULL DEFAULT (datetime('now')),
        language      TEXT NOT NULL DEFAULT '',
        permissions   TEXT NOT NULL DEFAULT ''   -- acessos do gerente, ex.: 'schedule,report'
    );
    INSERT INTO users_new (id, name, email, password_hash, role, phone, active, created_at, language)
        SELECT id, name, email, password_hash, role, phone, active, created_at, language FROM users;
    DROP TABLE users;
    ALTER TABLE users_new RENAME TO users;
    COMMIT;
    PRAGMA foreign_keys = ON;
    """,
    # Trabalho com várias pessoas ("Share"): quem estava em jobs.assigned_to passa para job_assignees
    # (tabela criada pelo schema.sql) e a coluna antiga fica vazia, sem uso. O dinheiro recebido do
    # cliente passa a guardar quem anotou (cash_by): com várias pessoas, é quem está com ele.
    """
    BEGIN;
    INSERT OR IGNORE INTO job_assignees (job_id, user_id) SELECT id, assigned_to FROM jobs WHERE assigned_to IS NOT NULL;
    ALTER TABLE jobs ADD COLUMN cash_by INTEGER REFERENCES users(id);
    UPDATE jobs SET cash_by = assigned_to WHERE cash_pence IS NOT NULL;
    UPDATE jobs SET assigned_to = NULL WHERE assigned_to IS NOT NULL;
    COMMIT;
    """,
    # Cotações: os avisos ("o cliente abriu", "aceitou", "recusou") apontam para a cotação.
    "ALTER TABLE notifications ADD COLUMN quote_id INTEGER REFERENCES quotes(id) ON DELETE CASCADE;",
    # Aparência (Conta → Aparência): nome no topo, logo e paleta de cores
    """
    BEGIN;
    ALTER TABLE settings ADD COLUMN app_name TEXT NOT NULL DEFAULT '';
    ALTER TABLE settings ADD COLUMN app_short_name TEXT NOT NULL DEFAULT '';
    ALTER TABLE settings ADD COLUMN show_name INTEGER NOT NULL DEFAULT 1;
    ALTER TABLE settings ADD COLUMN theme TEXT NOT NULL DEFAULT 'floresta';
    ALTER TABLE settings ADD COLUMN color_main TEXT NOT NULL DEFAULT '';
    ALTER TABLE settings ADD COLUMN color_accent TEXT NOT NULL DEFAULT '';
    ALTER TABLE settings ADD COLUMN logo_version TEXT NOT NULL DEFAULT '';
    ALTER TABLE settings ADD COLUMN logo_meta TEXT NOT NULL DEFAULT '';
    COMMIT;
    """,
    # Lembrete pro cliente na véspera (Conta → Lembrete pro cliente) e a opção em cada cliente
    """
    BEGIN;
    ALTER TABLE settings ADD COLUMN reminder_mode TEXT NOT NULL DEFAULT 'off';
    ALTER TABLE settings ADD COLUMN reminder_hour INTEGER NOT NULL DEFAULT 18;
    ALTER TABLE settings ADD COLUMN reminder_language TEXT NOT NULL DEFAULT 'en';
    ALTER TABLE settings ADD COLUMN reminder_text TEXT NOT NULL DEFAULT '';
    ALTER TABLE settings ADD COLUMN twilio_sid TEXT NOT NULL DEFAULT '';
    ALTER TABLE settings ADD COLUMN twilio_token TEXT NOT NULL DEFAULT '';
    ALTER TABLE settings ADD COLUMN twilio_from TEXT NOT NULL DEFAULT '';
    ALTER TABLE settings ADD COLUMN reminder_checked_at TEXT NOT NULL DEFAULT '';
    ALTER TABLE clients ADD COLUMN reminders INTEGER NOT NULL DEFAULT 1;
    COMMIT;
    """,
    # Área da empresa: cada jardim (cliente) pode ser de uma empresa que contrata vários jardins.
    # As tabelas novas (companies, company_users, job_comments) vêm do schema.sql.
    """
    BEGIN;
    ALTER TABLE clients ADD COLUMN company_id INTEGER REFERENCES companies(id) ON DELETE SET NULL;
    CREATE INDEX IF NOT EXISTS idx_clients_company ON clients(company_id);
    COMMIT;
    """,
    # Resumo do dia por e-mail pra empresa (portal_mail.py): a hora de cada empresa (vazio = desligado),
    # até onde o último resumo foi, e a opção de cada pessoa da empresa de não receber.
    """
    BEGIN;
    ALTER TABLE companies ADD COLUMN daily_hour INTEGER DEFAULT 18;
    ALTER TABLE companies ADD COLUMN daily_sent_on TEXT NOT NULL DEFAULT '';
    ALTER TABLE companies ADD COLUMN daily_sent_at TEXT NOT NULL DEFAULT '';
    ALTER TABLE company_users ADD COLUMN daily_mail INTEGER NOT NULL DEFAULT 1;
    ALTER TABLE settings ADD COLUMN company_mail_checked_at TEXT NOT NULL DEFAULT '';
    COMMIT;
    """,
    # Conversa da equipe em cada trabalho (dono, gerentes e quem está escalado), na mesma tabela dos
    # comentários da empresa: internal = 1 é da equipe e a empresa nunca vê.
    "ALTER TABLE job_comments ADD COLUMN internal INTEGER NOT NULL DEFAULT 0;",
    # Horas previstas do trabalho (o dono anota ao agendar); num Share, o tempo é dividido entre as pessoas
    "ALTER TABLE jobs ADD COLUMN planned_minutes INTEGER;",
    # ...do jeito que o dono digitou ("1 to 2hrs", "15min"); planned_minutes guarda o maior valor entendido
    "ALTER TABLE jobs ADD COLUMN planned_text TEXT NOT NULL DEFAULT '';",
    # Endereço de quem é da equipe (Equipe → Adicionar pessoa)
    "ALTER TABLE users ADD COLUMN address TEXT NOT NULL DEFAULT '';",
]


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(current_app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(_error=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db():
    conn = get_db()
    schema = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")
    conn.executescript(schema)
    version = max(conn.execute("PRAGMA user_version").fetchone()[0], 1)
    for target, sql in enumerate(MIGRATIONS, start=2):
        if version < target:
            conn.executescript(sql)
            version = target
            conn.execute(f"PRAGMA user_version = {int(version)}")  # anota a cada etapa: se uma falhar, as de antes não rodam de novo
    conn.execute(f"PRAGMA user_version = {int(version)}")
    conn.commit()


def init_app(app):
    app.teardown_appcontext(close_db)
    with app.app_context():
        init_db()
