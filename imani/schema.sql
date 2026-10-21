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
    -- Optional face embedding from an external facial-recognition tool,
    -- stored as comma-separated numbers and compared via cosine similarity.
    face_template  TEXT,
    notes          TEXT,

    status         TEXT    NOT NULL DEFAULT 'active'
                   CHECK (status IN ('active', 'archived')),

    -- How the record entered the register: added by a person ('manual') or
    -- auto-enrolled from a CCTV capture ('cctv').
    source         TEXT    NOT NULL DEFAULT 'manual'
                   CHECK (source IN ('manual', 'cctv')),

    -- Provenance -----------------------------------------------------
    created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    created_by     TEXT,
    updated_at     TEXT,
    updated_by     TEXT
);

CREATE INDEX IF NOT EXISTS idx_individuals_name   ON individuals(full_name);
CREATE INDEX IF NOT EXISTS idx_individuals_status ON individuals(status);
CREATE INDEX IF NOT EXISTS idx_individuals_national ON individuals(national_id);

-- Bit 6 — the matching engine ----------------------------------------------
-- A *match run* is a probe: the traits an analyst observed for a subject.
-- Each run keeps its ranked candidates so it can be reviewed later.
CREATE TABLE IF NOT EXISTS match_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    reference       TEXT    UNIQUE,          -- "MR-00001", filled after insert
    title           TEXT,
    probe_height_cm INTEGER,
    probe_build     TEXT,
    probe_posture   TEXT,
    probe_gait      TEXT,
    probe_face      TEXT,
    min_score       REAL    NOT NULL DEFAULT 0,
    notes           TEXT,
    -- The trait keys that were actually comparable for this run.
    traits_used     TEXT,
    result_count    INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    created_by      TEXT
);

CREATE INDEX IF NOT EXISTS idx_match_runs_created ON match_runs(created_at DESC);

CREATE TABLE IF NOT EXISTS match_results (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id               INTEGER NOT NULL
                         REFERENCES match_runs(id) ON DELETE CASCADE,
    -- The individual may be deleted later; we keep a name snapshot.
    individual_id        INTEGER REFERENCES individuals(id) ON DELETE SET NULL,
    individual_name      TEXT,
    individual_reference TEXT,
    score                REAL    NOT NULL,
    band                 TEXT    NOT NULL,
    -- Per-trait similarities, stored as JSON.
    breakdown            TEXT
);

CREATE INDEX IF NOT EXISTS idx_match_results_run ON match_results(run_id);
CREATE INDEX IF NOT EXISTS idx_match_results_rank ON match_results(run_id, score DESC);

-- Bit 8 — CCTV capture ingestion & re-identification ------------------------
-- Every person a camera detects becomes a *sighting*: the traits extracted by
-- the edge device plus the time and camera, whether or not we know who it is.
CREATE TABLE IF NOT EXISTS sightings (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    reference      TEXT    UNIQUE,           -- "SG-00001", filled after insert
    -- The person this capture was attached to. NULL if it was never resolved.
    individual_id  INTEGER REFERENCES individuals(id) ON DELETE SET NULL,
    camera         TEXT,
    captured_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    received_at    TEXT    NOT NULL DEFAULT (datetime('now')),

    -- How the engine resolved this capture:
    --   identified -> confidently attached to a known person
    --   review     -> attached tentatively; an analyst should confirm
    --   new        -> no match; a new person was auto-enrolled for it
    status         TEXT    NOT NULL DEFAULT 'new'
                   CHECK (status IN ('identified', 'review', 'new')),
    score          REAL,                      -- similarity to individual_id
    band           TEXT,
    breakdown      TEXT,                      -- per-trait similarities (JSON)

    -- A snapshot of the extracted traits for this capture.
    height_cm      INTEGER,
    build          TEXT,
    posture        TEXT,
    gait           TEXT,
    face_template  TEXT,
    image_ref      TEXT,                      -- path/URI of the stored frame
    notes          TEXT,

    resolved       INTEGER NOT NULL DEFAULT 0, -- an analyst has reviewed it
    created_at     TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_sightings_created ON sightings(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_sightings_individual ON sightings(individual_id);
CREATE INDEX IF NOT EXISTS idx_sightings_status ON sightings(status);

-- The rolling "identity profile" the engine learns for each person from their
-- sightings. Height converges to a mean, categorical traits to a mode, and
-- face keeps a small gallery of templates. Matching uses it in preference to
-- the hand-typed register values, so recognition improves with every visit.
CREATE TABLE IF NOT EXISTS identity_profiles (
    individual_id  INTEGER PRIMARY KEY
                   REFERENCES individuals(id) ON DELETE CASCADE,
    height_mean    REAL,
    height_count   INTEGER NOT NULL DEFAULT 0,
    tallies        TEXT,                      -- JSON: build/posture/gait counts
    face_templates TEXT,                      -- JSON list of templates
    sample_count   INTEGER NOT NULL DEFAULT 0,
    first_seen     TEXT,
    last_seen      TEXT,
    updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

-- CCTV cameras connected to IMANI (Bit 10). A camera is either a real stream
-- (rtsp/http/file/device, read with OpenCV) or a "demo" source that replays a
-- synthetic walk, so the capture pipeline can be exercised without hardware.
CREATE TABLE IF NOT EXISTS cameras (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL UNIQUE,
    location        TEXT,
    source_type     TEXT    NOT NULL DEFAULT 'rtsp'
                    CHECK (source_type IN ('rtsp','http','file','device','demo')),
    source_url      TEXT    NOT NULL,
    enabled         INTEGER NOT NULL DEFAULT 1,

    -- What the monitor is doing right now. "unavailable" means the camera is
    -- configured but this server cannot read it (OpenCV missing, etc.).
    status          TEXT    NOT NULL DEFAULT 'stopped'
                    CHECK (status IN ('stopped','connecting','live','error','unavailable')),

    last_frame_at   TEXT,
    last_capture_at TEXT,
    last_error      TEXT,
    created_by      TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_cameras_status ON cameras(status);

-- The footage behind each sighting (Bit 11). One row per stored asset: a
-- still frame (the poster) and the clip recorded around the moment a person
-- was caught, so a capture can be reviewed long after it happened.
CREATE TABLE IF NOT EXISTS capture_media (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    sighting_id INTEGER NOT NULL,
    kind        TEXT    NOT NULL CHECK (kind IN ('frame', 'clip')),
    path        TEXT    NOT NULL,   -- relative to MEDIA_ROOT
    frame_count INTEGER,
    duration_ms INTEGER,
    width       INTEGER,
    height      INTEGER,
    fps         REAL,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_capture_media_sighting ON capture_media(sighting_id);
CREATE INDEX IF NOT EXISTS idx_capture_media_kind ON capture_media(kind);
