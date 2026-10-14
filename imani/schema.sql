-- IMANI — SQLite schema
--
-- Bit 1 covers the authentication core: the users table (with an
-- approval lifecycle) and an append-only audit_log.

CREATE TABLE IF NOT EXISTS users (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    username       TEXT    NOT NULL UNIQUE,
    email          TEXT    NOT NULL UNIQUE,
    full_name      TEXT    NOT NULL,
    password_hash  TEXT    NOT NULL,

    -- Access control -------------------------------------------------
    role           TEXT    NOT NULL DEFAULT 'viewer'
                   CHECK (role IN ('admin', 'analyst', 'viewer')),
    requested_role TEXT    CHECK (requested_role IS NULL
                                  OR requested_role IN ('analyst', 'viewer')),

    -- Approval lifecycle: a sign-up is 'pending' until an admin acts.
    status         TEXT    NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending', 'active', 'disabled', 'rejected')),

    -- Brute-force protection ----------------------------------------
    failed_attempts INTEGER NOT NULL DEFAULT 0,
    locked_until    TEXT,

    -- Provenance -----------------------------------------------------
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    decided_at      TEXT,
    decided_by      INTEGER REFERENCES users(id)
);

CREATE INDEX IF NOT EXISTS idx_users_status ON users(status);

CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    action     TEXT    NOT NULL,
    entity     TEXT    NOT NULL,
    entity_id  INTEGER,
    details    TEXT,
    actor      TEXT,
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at DESC);
