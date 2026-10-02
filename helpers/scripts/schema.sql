-- =====================================================================
-- clients.db schema  —  SOURCE OF TRUTH
--
-- Applied by db.init_db() via sqlite3.executescript(). Every statement is
-- IF NOT EXISTS, so running it against an existing database is a no-op.
-- Changing an existing column requires a migration (see migrate_schema.py);
-- adding a new table or index only requires editing this file.
--
-- Timestamps: stored as UTC text 'YYYY-MM-DD HH:MM:SS'.  Application code
-- should write datetime('now') / CURRENT_TIMESTAMP or a UTC string in that
-- format so the 48-hour follow-up math stays consistent.
-- =====================================================================


-- ---------------------------------------------------------------------
-- employees: written by linkedin_people_scraper.py and the CSV upload.
-- email is NULL until hunter.py (or a CSV/db_tool upsert) fills it in.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS employees (
    company_name          TEXT NOT NULL,
    first_name            TEXT,
    last_name             TEXT,
    job_title             TEXT,
    linkedin_profile_url  TEXT NOT NULL,
    email                 TEXT,
    PRIMARY KEY (company_name, linkedin_profile_url)
);


-- ---------------------------------------------------------------------
-- client_metadata: one row per company.
--   email_name : short name used in email/DM templates ({email_name})
--   domain     : company web domain passed to Hunter.io
-- company_name must match employees.company_name exactly (it's the join key).
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS client_metadata (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    company_name  TEXT NOT NULL UNIQUE,
    email_name    TEXT NOT NULL,
    domain        TEXT
);


-- ---------------------------------------------------------------------
-- hunter_lookups: cache of Hunter.io Email Finder calls so the same person
-- is never queried twice (email is NULL when Hunter found nothing).
-- domain / first_name / last_name are stored lowercased.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS hunter_lookups (
    domain      TEXT NOT NULL,
    first_name  TEXT NOT NULL,
    last_name   TEXT NOT NULL,
    email       TEXT,
    score       INTEGER,
    queried_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (domain, first_name, last_name)
);


-- ---------------------------------------------------------------------
-- sent_emails: one row per email send attempt (send_emails.py).
--   status   : 'sent' | 'refused' | 'error'
--   campaign : 'initial' | 'followup'  (see send_emails.CAMPAIGN_SEQUENCE)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sent_emails (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    email      TEXT NOT NULL,
    status     TEXT NOT NULL,
    detail     TEXT,
    campaign   TEXT NOT NULL DEFAULT 'initial'
);

CREATE INDEX IF NOT EXISTS idx_sent_emails_email_status
    ON sent_emails (email, status, campaign);


-- ---------------------------------------------------------------------
-- sent_dms: one row per LinkedIn DM copied & sent from the UI
-- (linkedin_dms.py). Same shape as sent_emails, keyed on the profile URL.
--   campaign : 'dm1' | 'dm2' | 'dm3'  (see linkedin_dms.DM_SEQUENCE)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sent_dms (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp             TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    linkedin_profile_url  TEXT NOT NULL,
    status                TEXT NOT NULL,
    detail                TEXT,
    campaign              TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sent_dms_url_status
    ON sent_dms (linkedin_profile_url, status, campaign);