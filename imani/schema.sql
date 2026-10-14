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

    -- Google sign-in -------------------------------------------------
    -- Google's stable subject ("sub") claim. NULL for password accounts.
    google_id      TEXT    UNIQUE,
    avatar_url     TEXT,
    auth_provider  TEXT    NOT NULL DEFAULT 'password'
                   CHECK (auth_provider IN ('password', 'google')),

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
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_google ON users(google_id);

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

-- Bit 5 — the individual register ------------------------------------------
-- People of interest: identity, contact details and the physical traits the
-- matching engine (Bit 6+) will score against.
CREATE TABLE IF NOT EXISTS individuals (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    -- Human-friendly identifier, filled in as "IM-00001" after insert.
    reference      TEXT    UNIQUE,
    full_name      TEXT    NOT NULL,
    alias          TEXT,
    date_of_birth  TEXT,
    gender         TEXT    NOT NULL DEFAULT 'unspecified'
                   CHECK (gender IN ('female', 'male', 'other', 'unspecified')),
    national_id    TEXT,
    phone          TEXT,
    email          TEXT,
    nationality    TEXT,
    address        TEXT,

    -- Physical traits used by the matching engine.
    height_cm      INTEGER,
    build          TEXT    NOT NULL DEFAULT 'average'
                   CHECK (build IN ('slim', 'average', 'athletic', 'heavy')),
    posture        TEXT    NOT NULL DEFAULT 'neutral'
                   CHECK (posture IN ('upright', 'neutral', 'stooped', 'hunched')),
    gait           TEXT    NOT NULL DEFAULT 'normal'
                   CHECK (gait IN ('normal', 'brisk', 'slow', 'limp', 'shuffle')),
    distinguishing_marks TEXT,
    notes          TEXT,

    status         TEXT    NOT NULL DEFAULT 'active'
                   CHECK (status IN ('active', 'archived')),

    -- Provenance -----------------------------------------------------
    created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    created_by     TEXT,
    updated_at     TEXT,
    updated_by     TEXT
);

CREATE INDEX IF NOT EXISTS idx_individuals_name   ON individuals(full_name);
CREATE INDEX IF NOT EXISTS idx_individuals_status ON individuals(status);
CREATE INDEX IF NOT EXISTS idx_individuals_national ON individuals(national_id);
