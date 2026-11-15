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
                    CHECK (source_type IN ('rtsp','http','file','device','demo','phone')),
    source_url      TEXT    NOT NULL,
    enabled         INTEGER NOT NULL DEFAULT 1,
    -- Bit 12: optional group/zone and per-camera algorithm overrides.
    group_id        INTEGER REFERENCES camera_groups(id) ON DELETE SET NULL,
    algorithm_json  TEXT,

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

-- Bit 12 — Camera groups/zones (Bit 12)
CREATE TABLE IF NOT EXISTS camera_groups (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL UNIQUE,
    description TEXT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Bit 12 — extend cameras with groups and per-camera algorithm settings
-- These columns are added via migrations; CREATE TABLE won't alter existing.
-- (Schema evolution handled by init/migrations in practice; we also update
-- the table definition expectation via app logic.)

-- Bit 12b — Auto-captured snapshots from CCTV/phone/camera video feeds
CREATE TABLE IF NOT EXISTS camera_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    camera_id   INTEGER REFERENCES cameras(id) ON DELETE SET NULL,
    camera_name TEXT,
    filename    TEXT    NOT NULL,
    path        TEXT    NOT NULL,
    source_type TEXT,
    captured_at TEXT    NOT NULL DEFAULT (datetime('now')),
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    -- Bit 13: measured image features + a human annotation used by AI search.
    features_json TEXT,
    description TEXT
);

CREATE INDEX IF NOT EXISTS idx_camera_snapshots_camera ON camera_snapshots(camera_id);
CREATE INDEX IF NOT EXISTS idx_camera_snapshots_captured ON camera_snapshots(captured_at DESC);

-- Bit 12c — Auto-saved video recordings from CCTV/phone/camera feeds for reference
CREATE TABLE IF NOT EXISTS camera_videos (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    camera_id   INTEGER REFERENCES cameras(id) ON DELETE SET NULL,
    camera_name TEXT,
    filename    TEXT    NOT NULL,
    path        TEXT    NOT NULL,
    source_type TEXT,
    duration_sec REAL,
    frame_count INTEGER,
    size_bytes  INTEGER,
    started_at  TEXT,
    ended_at    TEXT,
    captured_at TEXT    NOT NULL DEFAULT (datetime('now')),
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    -- Bit 13: 'manual' (saved from buffer), 'auto' (continuous segment),
    -- 'audio' (phone microphone). Features + annotation drive AI search.
    kind        TEXT    NOT NULL DEFAULT 'manual',
    mime_type   TEXT,
    features_json TEXT,
    description TEXT
);

CREATE INDEX IF NOT EXISTS idx_camera_videos_camera ON camera_videos(camera_id);
CREATE INDEX IF NOT EXISTS idx_camera_videos_captured ON camera_videos(captured_at DESC);

-- Bit 12d — AI Search: descriptions and features for all media
CREATE TABLE IF NOT EXISTS media_descriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    media_type TEXT NOT NULL CHECK(media_type IN ('snapshot','video','archive')),
    media_id INTEGER NOT NULL,
    description TEXT,
    features_json TEXT,
    tags TEXT,
    uploaded_by TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_media_desc_type_id ON media_descriptions(media_type, media_id);
CREATE TABLE IF NOT EXISTS media_search_index (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    media_type TEXT NOT NULL,
    media_id INTEGER NOT NULL,
    camera_name TEXT,
    camera_id INTEGER,
    timestamp TEXT,
    file_path TEXT,
    thumbnail_path TEXT,
    description TEXT,
    extracted_text TEXT,
    features_hash TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_media_search_ts ON media_search_index(timestamp DESC);

-- Bit 14 — import a video from a URL, extract snapshots, upscale them to 4K.
-- The source footage and the snapshots live in separate directories on disk;
-- only metadata is stored here.
CREATE TABLE IF NOT EXISTS video_imports (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_url      TEXT    NOT NULL,
    source_kind     TEXT,                       -- direct / platform
    title           TEXT,
    status          TEXT    NOT NULL DEFAULT 'queued',
                                                -- queued, running, complete, failed
    stage           TEXT,                       -- human-readable current step
    progress        INTEGER NOT NULL DEFAULT 0, -- 0..100
    error           TEXT,
    video_path      TEXT,                       -- relative to VIDEO_IMPORT_ROOT
    mime_type       TEXT,
    duration_sec    REAL,
    width           INTEGER,
    height          INTEGER,
    fps             REAL,
    mode            TEXT    NOT NULL DEFAULT 'auto',
    interval_sec    REAL    NOT NULL DEFAULT 2,
    max_snapshots   INTEGER NOT NULL DEFAULT 24,
    candidate_count INTEGER NOT NULL DEFAULT 0,
    snapshot_count  INTEGER NOT NULL DEFAULT 0,
    output_width    INTEGER NOT NULL DEFAULT 3840,
    output_height   INTEGER NOT NULL DEFAULT 2160,
    created_by      TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    completed_at    TEXT
);

CREATE INDEX IF NOT EXISTS idx_video_imports_status ON video_imports(status);
CREATE INDEX IF NOT EXISTS idx_video_imports_created ON video_imports(created_at DESC);

CREATE TABLE IF NOT EXISTS video_import_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    import_id       INTEGER NOT NULL REFERENCES video_imports(id) ON DELETE CASCADE,
    timestamp_sec   REAL,
    original_path   TEXT    NOT NULL,           -- relative to VIDEO_IMPORT_ROOT
    upscaled_path   TEXT    NOT NULL,
    thumb_path      TEXT,
    original_width  INTEGER,
    original_height INTEGER,
    width           INTEGER,
    height          INTEGER,
    size_bytes      INTEGER,
    features_json   TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_video_import_snaps_import
    ON video_import_snapshots(import_id);
CREATE INDEX IF NOT EXISTS idx_video_import_snaps_ts
    ON video_import_snapshots(import_id, timestamp_sec);
