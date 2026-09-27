-- Estrutura inicial do banco (SQLite). Roda toda vez que o app inicia;
-- "IF NOT EXISTS" garante que os dados existentes não são apagados.
-- Para mudar uma tabela que já existe, NÃO edite este arquivo: adicione um item em MIGRATIONS (db.py).
-- Tabela nova pode entrar aqui no fim, sempre com IF NOT EXISTS.

PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    email         TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('owner', 'employee')),
    phone         TEXT NOT NULL DEFAULT '',
    active        INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS clients (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    address      TEXT NOT NULL DEFAULT '',
    postcode     TEXT NOT NULL DEFAULT '',
    phone        TEXT NOT NULL DEFAULT '',
    email        TEXT NOT NULL DEFAULT '',
    access_notes TEXT NOT NULL DEFAULT '',  -- o funcionário escalado vê (portão, estacionamento, cachorro...)
    notes        TEXT NOT NULL DEFAULT '',  -- anotações internas: só o dono vê
    active       INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS jobs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id      INTEGER NOT NULL REFERENCES clients(id),
    assigned_to    INTEGER REFERENCES users(id),
    title          TEXT NOT NULL,
    job_date       TEXT NOT NULL,             -- AAAA-MM-DD
    start_time     TEXT NOT NULL DEFAULT '',  -- HH:MM (opcional)
    status         TEXT NOT NULL DEFAULT 'scheduled'
                   CHECK (status IN ('scheduled', 'in_progress', 'done', 'cancelled')),
    description    TEXT NOT NULL DEFAULT '',
    employee_notes TEXT NOT NULL DEFAULT '',
    materials      TEXT NOT NULL DEFAULT '',
    started_at     TEXT,                      -- UTC, formato ISO 8601
    finished_at    TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_jobs_date ON jobs(job_date);
CREATE INDEX IF NOT EXISTS idx_jobs_assigned ON jobs(assigned_to, job_date);

CREATE TABLE IF NOT EXISTS job_tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    description TEXT NOT NULL,
    done        INTEGER NOT NULL DEFAULT 0,
    position    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tasks_job ON job_tasks(job_id);

CREATE TABLE IF NOT EXISTS client_photos (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id   INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    filename    TEXT NOT NULL,             -- arquivo em instance/uploads/clients/<client_id>/
    uploaded_by INTEGER REFERENCES users(id),
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_photos_client ON client_photos(client_id);

CREATE TABLE IF NOT EXISTS settings (
    id               INTEGER PRIMARY KEY CHECK (id = 1),  -- uma linha só, sempre id=1
    language         TEXT NOT NULL DEFAULT 'en',
    company_name     TEXT NOT NULL DEFAULT '',
    company_phone    TEXT NOT NULL DEFAULT '',
    company_email    TEXT NOT NULL DEFAULT '',
    company_address  TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS notifications (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,              -- ver notifications.py (job_assigned, job_tomorrow...)
    job_id      INTEGER REFERENCES jobs(id) ON DELETE CASCADE,
    params      TEXT NOT NULL DEFAULT '{}', -- como o trabalho estava na hora do aviso (JSON)
    read_at     TEXT,
    emailed_at  TEXT,                       -- quando saiu por e-mail ('skipped' = não vai sair)
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications(user_id, read_at);
CREATE INDEX IF NOT EXISTS idx_notifications_job ON notifications(job_id);

CREATE TABLE IF NOT EXISTS transfers (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    received_on   TEXT NOT NULL,                  -- dia em que o dinheiro entrou (AAAA-MM-DD)
    client_id     INTEGER REFERENCES clients(id) ON DELETE SET NULL,
    amount_pence  INTEGER NOT NULL CHECK (amount_pence > 0),
    note          TEXT NOT NULL DEFAULT '',       -- referência, ex.: fatura de setembro
    created_by    INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_transfers_day ON transfers(received_on);

-- Fotos de Antes e Depois de cada trabalho (a galeria geral do cliente continua em client_photos).
-- Apagar o trabalho apaga as linhas daqui; os arquivos são apagados por jobs.delete_job.
CREATE TABLE IF NOT EXISTS job_photos (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    phase       TEXT NOT NULL CHECK (phase IN ('before', 'after')),
    filename    TEXT NOT NULL,             -- arquivo em instance/uploads/jobs/<job_id>/
    uploaded_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_job_photos_job ON job_photos(job_id, phase);

-- Quem vai fazer cada trabalho: uma pessoa, ou várias (trabalho "Share").
-- Veio no lugar de jobs.assigned_to, que ficou sem uso (a migração 16 passou tudo pra cá).
CREATE TABLE IF NOT EXISTS job_assignees (
    job_id   INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    user_id  INTEGER NOT NULL REFERENCES users(id),
    PRIMARY KEY (job_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_job_assignees_user ON job_assignees(user_id, job_id);

-- Cotações (orçamentos). O cliente abre pelo link secreto (token), sem login, e aceita ou recusa.
-- "Para" (to_*) é uma cópia de quem recebe: pode ser um cliente cadastrado ou alguém novo.
CREATE TABLE IF NOT EXISTS quotes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    number       INTEGER NOT NULL UNIQUE,              -- aparece como Q-0001
    token        TEXT NOT NULL UNIQUE,                 -- parte secreta do link do cliente
    client_id    INTEGER REFERENCES clients(id) ON DELETE SET NULL,
    to_name      TEXT NOT NULL,
    to_email     TEXT NOT NULL DEFAULT '',
    to_phone     TEXT NOT NULL DEFAULT '',
    to_address   TEXT NOT NULL DEFAULT '',
    to_postcode  TEXT NOT NULL DEFAULT '',
    title        TEXT NOT NULL,
    intro        TEXT NOT NULL DEFAULT '',             -- mensagem de abertura
    notes        TEXT NOT NULL DEFAULT '',             -- condições: pagamento, o que está incluído...
    frequency    TEXT NOT NULL DEFAULT 'once'
                 CHECK (frequency IN ('once', 'weekly', 'fortnightly', 'monthly')),
    valid_until  TEXT NOT NULL,                        -- AAAA-MM-DD
    language     TEXT NOT NULL DEFAULT 'en',           -- idioma da página que o cliente vê
    status       TEXT NOT NULL DEFAULT 'open'
                 CHECK (status IN ('open', 'accepted', 'declined', 'cancelled')),
    sent_at      TEXT,                                 -- UTC; quando saiu por WhatsApp ou e-mail
    viewed_at    TEXT,                                 -- primeira vez que o cliente abriu
    answered_at  TEXT,
    answer_name  TEXT NOT NULL DEFAULT '',             -- nome que o cliente digitou ao aceitar
    answer_note  TEXT NOT NULL DEFAULT '',
    job_id       INTEGER REFERENCES jobs(id) ON DELETE SET NULL,  -- trabalho agendado a partir dela
    created_by   INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_quotes_client ON quotes(client_id);

CREATE TABLE IF NOT EXISTS quote_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    quote_id    INTEGER NOT NULL REFERENCES quotes(id) ON DELETE CASCADE,
    description TEXT NOT NULL,
    quantity    REAL NOT NULL DEFAULT 1,
    unit_pence  INTEGER NOT NULL DEFAULT 0,
    position    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_quote_items_quote ON quote_items(quote_id);

-- Fotos da cotação, cada uma com um texto embaixo (arquivos em instance/uploads/quotes/<quote_id>/).
CREATE TABLE IF NOT EXISTS quote_photos (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    quote_id    INTEGER NOT NULL REFERENCES quotes(id) ON DELETE CASCADE,
    filename    TEXT NOT NULL,
    caption     TEXT NOT NULL DEFAULT '',
    uploaded_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_quote_photos_quote ON quote_photos(quote_id);

-- Lembrete pro cliente na véspera: um por cliente por dia de serviço (mesmo com dois trabalhos lá).
-- channel: sms (automático, pelo Twilio), whatsapp ou sms_phone (com um toque, do celular de quem tocou)
-- ou manual (marcado como feito). status: sending enquanto o SMS está saindo, sent ou failed.
CREATE TABLE IF NOT EXISTS client_reminders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id   INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    job_date    TEXT NOT NULL,                   -- o dia do serviço (a mensagem sai na véspera)
    channel     TEXT NOT NULL DEFAULT 'sms',
    status      TEXT NOT NULL DEFAULT 'sending' CHECK (status IN ('sending', 'sent', 'failed')),
    attempts    INTEGER NOT NULL DEFAULT 1,
    detail      TEXT NOT NULL DEFAULT '',        -- id da mensagem no Twilio ou o motivo da falha
    sent_by     INTEGER REFERENCES users(id) ON DELETE SET NULL,
    updated_at  TEXT NOT NULL,                   -- UTC
    UNIQUE (client_id, job_date)
);

-- Área da empresa. Empresas que contratam vários jardins (ex.: uma administradora de imóveis); não confundir
-- com os dados da SUA empresa (settings.company_*). Cada jardim é um cliente com clients.company_id (migração 20).
-- Quem é da empresa entra com o login dela (company_users) e só VÊ os serviços concluídos desses jardins.
CREATE TABLE IF NOT EXISTS companies (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Login de quem é da empresa. O e-mail não pode ser o de alguém da equipe: a tela de login é a mesma.
CREATE TABLE IF NOT EXISTS company_users (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id     INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    name           TEXT NOT NULL,
    email          TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash  TEXT NOT NULL,
    language       TEXT NOT NULL DEFAULT 'en',
    active         INTEGER NOT NULL DEFAULT 1,
    session_key    TEXT NOT NULL DEFAULT '',        -- muda ao trocar a senha ou desativar: derruba quem estava logado
    last_login_at  TEXT,                            -- UTC
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_company_users_company ON company_users(company_id);

-- Comentários num serviço: os da empresa (pela área dela) e as respostas da equipe.
-- company_id: a conversa é com qual empresa (se o jardim mudar de empresa, a nova não vê a conversa da antiga).
-- seen_at: quando o outro lado leu (o comentário da empresa, pela equipe; a resposta, pela empresa).
CREATE TABLE IF NOT EXISTS job_comments (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id           INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    company_id       INTEGER REFERENCES companies(id) ON DELETE SET NULL,
    from_company     INTEGER NOT NULL DEFAULT 0,                               -- 1 = escrito na área da empresa
    user_id          INTEGER REFERENCES users(id) ON DELETE SET NULL,          -- quem da equipe escreveu
    company_user_id  INTEGER REFERENCES company_users(id) ON DELETE SET NULL,  -- quem da empresa escreveu
    author_name      TEXT NOT NULL,             -- o nome na hora (continua certo se a conta for apagada)
    body             TEXT NOT NULL,
    seen_at          TEXT,                      -- UTC
    created_at       TEXT NOT NULL              -- UTC
);
CREATE INDEX IF NOT EXISTS idx_job_comments_job ON job_comments(job_id, id);

-- Anexos dos comentários: fotos (reduzidas e sem a localização, como as do trabalho) e documentos (PDF,
-- Word, Excel). Arquivos em instance/uploads/comments/<job_id>/. Apagar o comentário apaga as linhas daqui.
CREATE TABLE IF NOT EXISTS comment_files (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    comment_id  INTEGER NOT NULL REFERENCES job_comments(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL CHECK (kind IN ('photo', 'doc')),
    filename    TEXT NOT NULL,                -- nome no disco (aleatório)
    original    TEXT NOT NULL DEFAULT '',     -- nome do arquivo como a pessoa mandou
    size        INTEGER NOT NULL DEFAULT 0,   -- bytes
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_comment_files_comment ON comment_files(comment_id);

-- Serviço extra reportado pela equipe (extras.py): um trabalho feito fora da agenda. O dono aprova (vira um
-- trabalho concluído, com as horas) ou recusa. job_id: o trabalho criado na aprovação.
CREATE TABLE IF NOT EXISTS job_reports (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    client_id     INTEGER REFERENCES clients(id) ON DELETE SET NULL,
    client_name   TEXT NOT NULL,
    place         TEXT NOT NULL DEFAULT '',
    job_date      TEXT NOT NULL,
    start_time    TEXT NOT NULL DEFAULT '',
    minutes       INTEGER NOT NULL,
    hours_text    TEXT NOT NULL DEFAULT '',
    tasks         TEXT NOT NULL,
    notes         TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
    decided_by    INTEGER REFERENCES users(id) ON DELETE SET NULL,
    decided_at    TEXT,
    decision_note TEXT NOT NULL DEFAULT '',
    job_id        INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_job_reports_status ON job_reports(status, id);
CREATE INDEX IF NOT EXISTS idx_job_reports_user ON job_reports(user_id, id);
