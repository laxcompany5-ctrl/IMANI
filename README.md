# IMANI — Security Intelligence Console

A security-analyst web app built in small, working increments ("bits").

> **Bit 1 — Authentication:** sign-in and a self-service access-request
> (sign-up) form. New accounts stay **pending** until an administrator
> approves them, so no unvetted user can reach the console.
>
> **Bit 2 — Google sign-in:** "Continue with Google" on both the sign-in and
> access-request pages, using OAuth 2.0 / OpenID Connect. New Google users
> become **pending** accounts just like email sign-ups; existing approved
> accounts are linked and signed in directly.
>
> **Bit 4 — Public landing page:** a public home page at `/`; the analyst
> dashboard lives at `/dashboard`.
>
> **Bit 5 — Individual register:** search, insert, update and delete people of
> interest, with identity, contact and physical-trait fields. Analysts (and
> admins) can make changes; viewers have read-only access. Every change is
> written to the audit trail.
>
> **Bit 6 — Matching engine:** run a *probe* (observed traits) and rank the
> register by weighted similarity — gait 25%, height 25%, face 20%, build 15%,
> posture 15% — with a confidence band (HIGH → VERY LOW). Face is compared by
> cosine similarity of optional templates from an external tool; runs are saved.
> Analysts run matches; any signed-in user can review the history.
>
> **Bit 7 — Reporting & exports:** a reports console with key metrics and recent
> activity, printable case reports for a match run or an individual record
> (print / save-as-PDF), CSV exports of the register, match results and the
> activity log, and an admin activity-log viewer with filtering.
>
> **Bit 8 — CCTV capture & re-identification:** an ingestion API a camera
> deployment posts to. Every detected person is stored as a *sighting* with its
> traits and timestamp, then re-identified against the register: a returning
> subject is attached to their existing record (never duplicated), an unknown
> one is auto-enrolled, and each person's *identity profile* learns from every
> visit so recognition improves over time.

---

## 1. Stack

- **Python 3 + Flask 3** (server-rendered Jinja2 templates)
- **SQLite** via the standard-library `sqlite3` (no ORM)
- **Werkzeug** scrypt password hashing
- **Google OAuth 2.0** via the standard library `urllib` (no OAuth package)
- **gunicorn + nginx** for production deployment (see `deploy/DEPLOY.md`)
- Vanilla CSS + a little progressive-enhancement JS — no build step

The stack was chosen to match what is already installed on this machine, so the
app runs with **no extra dependencies**.

---

## 2. Quick start

```bash
cd /home/godmode/Projects/imani-web

# 1. Create the database tables
python3 -m flask --app app init-db

# 2. Create the default administrator
python3 -m flask --app app seed

# 3. Run the development server
python3 -m flask --app app run --debug
# or:  IMANI_PORT=5055 python3 app.py
```

Open <http://127.0.0.1:5000> (or whichever port you started on).

**Default administrator**

```
username: admin
password: Imani@2026
```

> Override before first use with `IMANI_ADMIN_USER`, `IMANI_ADMIN_PASS`,
> `IMANI_ADMIN_EMAIL`, or change it later via the environment.

> **Port note:** if another service already uses port 5000, run with
> `IMANI_PORT=5055 python3 app.py`.

### Tests

```bash
python3 -m pytest
```

---

## 3. The authentication flow

1. A visitor opens **`/auth/login`** and clicks **Request access**.
2. They submit `/auth/signup` — full name, username, email, a chosen access
   level (analyst / viewer) and a password.
3. The account is created with status **`pending`** and the lowest privilege
   (`viewer`). It **cannot sign in yet**.
4. An administrator opens **`/admin/approvals`**, picks the final role and
   **approves** (or rejects) the request.
5. Once approved the account is **`active`** and can sign in.

Every step — sign-up, failed sign-in, approval, rejection, sign-out — is written
to the **audit log**.

---

## 3a. Google sign-in setup

Google sign-in is active once credentials are present; without them the button
still appears but explains that setup is required.

1. In the [Google Cloud Console](https://console.cloud.google.com/) create a
   project, then **APIs & Services → OAuth consent screen**.
2. Create credentials → **OAuth client ID** → application type **Web
   application**.
3. Add an **Authorised redirect URI** that matches exactly, e.g.
   `http://127.0.0.1:5055/auth/google/callback`
   (and/or `http://127.0.0.1:5000/auth/google/callback`).
4. Export the credentials (or put them in a `.env` file):

```bash
export IMANI_GOOGLE_CLIENT_ID="xxxxxxxx.apps.googleusercontent.com"
export IMANI_GOOGLE_CLIENT_SECRET="GOCSPX-xxxxxxxx"
# optional: pin the redirect URI and restrict email domains
export IMANI_GOOGLE_REDIRECT_URI="http://127.0.0.1:5055/auth/google/callback"
export IMANI_GOOGLE_ALLOWED_DOMAINS="imani.local,example.org"
```

### How the Google flow maps to IMANI

| Situation | Result |
|-----------|--------|
| Google identity already linked and account **active** | Signed in immediately |
| Google email matches an existing approved account | Identity **linked**, signed in |
| Google email matches a **pending/disabled** account | Not signed in; appropriate message |
| Brand-new verified Google email | A **pending** access request is created for admin review |
| Email not verified by Google, or domain not allowed | Rejected |

Only **Google-verified** emails are trusted, and the flow uses **PKCE** with a
single-use `state` value to defeat CSRF and code-interception attacks.

---

## 3b. The individual register

The register lives at **`/individuals/`** and provides the four essentials:

| Action | Where | Minimum role |
|--------|-------|--------------|
| **Search** | `/individuals/?q=…&status=…&build=…` | viewer |
| **View** | `/individuals/<id>` | viewer |
| **Insert** | `/individuals/new` | analyst |
| **Update** | `/individuals/<id>/edit` | analyst |
| **Delete** | `/individuals/<id>/delete` | analyst |

Each record carries identity fields (name, alias, date of birth, gender,
national ID, nationality), contact details (phone, email, address) and the
physical traits the matching engine will use later (`height_cm`, `build`,
`posture`, `gait`, distinguishing marks). Records get a human-friendly
**reference** such as `IM-00042`, and can be **archived** instead of deleted.

Search matches across name, alias, reference, national ID, phone and email, and
can be narrowed by status and build. Each record's detail page shows its own
activity trail.

---

## 3c. The matching engine

Open **`/match/`** and enter whatever you observed for a subject — height,
build, posture, gait and (optionally) a face template. IMANI scores every
**active** individual against the probe and returns the ranked candidates at
`/match/<run>`.

### How a score is computed

Each *comparable* trait returns a similarity in `[0, 1]`:

| Trait | Weight | Similarity |
|-------|--------|------------|
| Gait | 25% | exact = 1.0; same pace group = 0.5; else 0 |
| Height | 25% | `1 − |Δcm| / 8` (0 beyond 8 cm) |
| Face | 20% | cosine similarity of the two templates (`max(0, cos)`) |
| Build | 15% | ordinal: exact = 1.0, each step apart −0.5 |
| Posture | 15% | ordinal, as build |

A trait is only compared when **both** the probe and the record provide it; the
weights of the compared traits are **re-normalised** so a partial probe still
yields a comparable 0–100 score. That score maps to a band:

| Score | Band |
|-------|------|
| ≥ 85 | **HIGH** |
| 70–84 | **MODERATE** |
| 55–69 | **LOW** |
| < 55 | **VERY LOW** |

### Face templates

Face data comes from an external facial-recognition tool: paste its numeric
vector into the individual's record (and/or the probe). Templates are compared
with cosine similarity, so unrelated faces (orthogonal vectors) score 0. When a
record or the probe has no template, face drops out and the other weights are
re-normalised — no template is required to use the engine.

### Saved runs

Every run is stored as a `match_runs` row with its `match_results`, so it can be
reviewed at **`/match/history`**. Results snapshot the candidate's name and
reference, so they survive even if the individual is later deleted. Runs are
audited (`MATCH` / `DELETE`) and can be removed by an analyst.

---

## 3d. Reporting & exports

Reporting turns the register, the matching engine and the audit trail into
something you can hand to a supervisor or keep on file.

| Endpoint | Who | What |
|----------|-----|------|
| `/reports` | analyst+ | Console: register totals by status, match counts, band distribution, recent runs and a live activity feed |
| `/individuals/export.csv` | any signed-in user | The register as CSV, honouring the current search filters (face templates are **never** exported) |
| `/match/<id>/report` | any signed-in user | Printable case report for a run: probe, ranked candidates with per-trait breakdown, prepared-by footer |
| `/match/<id>/export.csv` | any signed-in user | That run's ranked candidates as CSV |
| `/individuals/<id>/report` | any signed-in user | Printable profile for one individual: identity, traits, contact, case notes, activity trail |
| `/activity` | admin | Filterable, paginated view of the whole audit log (by action, entity, actor, details) |
| `/activity/export.csv` | admin | The filtered audit log as CSV |

Printable reports are standalone pages styled as light "paper" (via
`static/css/report.css`) so they print cleanly and never waste ink on the dark
theme. Each has a **Print / Save as PDF** button that simply invokes the
browser's print dialog.

CSV export columns are fixed and documented in code
(`individuals.EXPORT_COLUMNS`, `match.EXPORT_TRAITS`,
`reports.ACTIVITY_EXPORT_COLUMNS`); values are written with Python's `csv`
module so commas, quotes and newlines are escaped correctly.

---

## 3e. CCTV capture & re-identification

IMANI does not itself run computer vision. In a real deployment an edge box or
CV worker beside the recorder does the detection and hands IMANI a person's
extracted traits as JSON. IMANI owns the parts that matter for an investigation:
**storage, matching, and the identity decision.**

### Ingesting a capture

A device `POST`s to `/ingest/capture`, authenticating with a shared key in the
`X-API-Key` header (the endpoint is exempt from session CSRF). The API is
**off** until `IMANI_INGEST_API_KEY` is set.

```bash
curl -X POST https://your-domain/ingest/capture \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: <the shared key>' \
  -d '{"camera": "gate-3", "captured_at": "2026-10-14 07:31:02",
       "height_cm": 181, "build": "athletic", "posture": "upright",
       "gait": "brisk", "face_template": [0.12, 0.87, -0.4],
       "image_ref": "frames/2026-10-14/gate-3/004312.jpg"}'
```

It replies with the decision:

```json
{ "reference": "SG-00042", "status": "identified", "score": 96.4,
  "band": "HIGH", "created_person": false,
  "individual": {"id": 7, "reference": "IM-00007", "name": "John Kamau"},
  "candidates": [ ... ] }
```

### What happens to each capture

1. **It is always stored.** Traits, camera and timestamp are written to the
   `sightings` table first — nothing a camera saw is discarded, even when the
   person is a complete stranger.
2. **It is re-identified.** The capture is scored against every active person's
   *identity profile* using the same weighted engine as the manual matcher
   (gait 25% · height 25% · face 20% · build 15% · posture 15%).
3. **It is attached or enrolled.** At or above `IMANI_CAPTURE_ACCEPT_SCORE`
   (default 85) the sighting is attached to that person. Between the *review*
   and *accept* scores it is attached but flagged for an analyst. Below both, a
   new person is auto-enrolled (`source = 'cctv'`, named `Unknown (SG-…)`) so
   their next visit is recognised instead of duplicated.
4. **The profile learns.** Each attached sighting is folded into the person's
   `identity_profiles` row: height converges on a running mean, build / posture
   / gait keep vote counts (matching uses the mode), and a small gallery of
   near-orthogonal face templates is kept for the cosine matcher.

### The sightings console

| Endpoint | Who | What |
|----------|-----|------|
| `/sightings` | any signed-in user | The capture feed — filter by camera or decision, newest first |
| `/sightings/<id>` | any signed-in user | One capture: decision, per-trait breakdown, the person it was attached to |
| `/sightings/review` | analyst+ | Tentative matches awaiting confirmation |
| `/sightings/simulate` | analyst+ | Post a capture by hand (stand-in for a camera during setup) |
| `/ingest/capture` | API key | The machine-to-machine ingestion endpoint |

From a capture page an analyst can **confirm** a match, **re-assign** it to a
different person, **enrol** it as a brand-new person, or **delete** it. Every
decision is written to the audit trail, so each person's record carries a
history of when and where the cameras recognised them.

---

## 4. Security measures

| Area | Implementation |
|------|----------------|
| Passwords | Werkzeug scrypt hashing; policy enforced server-side (8+ chars, letters + numbers) |
| Session | Server-side session, `HttpOnly` + `SameSite=Lax` cookies |
| CSRF | Random token in every form, validated on all state-changing requests |
| Brute force | 5 failed attempts → 5-minute lockout |
| Enumeration | A wrong password and an unknown username give the same response |
| Authorisation | Role hierarchy `viewer < analyst < admin`; admin-only approval & user management |
| Audit | Append-only `audit_log` of every authentication event |
| Redirects | `next` parameter validated to block open-redirects |
| Google OAuth | Authorization Code + **PKCE (S256)**, single-use `state` CSRF check, verified-email requirement, optional domain allow-list |

---

## 5. Project structure

```
imani-web/
├── app.py                  # dev entry point (env-configurable host/port)
├── wsgi.py                 # production WSGI entry point (gunicorn)
├── gunicorn.conf.py        # gunicorn settings
├── Procfile                # for PaaS (Render/Railway/Fly.io)
├── .env.example            # documented environment variables
├── requirements.txt
├── pytest.ini
├── deploy/
│   ├── DEPLOY.md           # step-by-step VPS deployment guide
│   ├── imani.service       # systemd unit
│   └── nginx-imani.conf    # nginx reverse-proxy config
├── imani/
│   ├── __init__.py         # application factory
│   ├── config.py           # configuration
│   ├── schema.sql          # users + audit_log + individuals + match tables
│   ├── db.py               # SQLite helpers + CLI (init-db, reset-db, seed, migrate)
│   ├── security.py         # CSRF protection
│   ├── validation.py       # username/email/password + register field rules
│   ├── models.py           # User/Individual/Match/Audit repositories
│   ├── matching.py         # the scoring engine (weights, bands, cosine)
│   ├── auth.py             # authenticate, sessions, roles, lockout
│   ├── oauth.py            # Google OAuth 2.0 / OIDC (stdlib only)
│   ├── reports.py          # CSV + printable-report helpers
│   ├── capture.py          # CCTV ingestion + re-identification (Bit 8)
│   ├── routes/
│   │   ├── home.py         # /  (public landing page)
│   │   ├── auth_routes.py  # login, signup, logout, /auth/google[/callback]
│   │   ├── individuals.py  # /individuals  (search / insert / update / delete)
│   │   ├── match.py        # /match  (run a probe, history, results)
│   │   ├── sightings.py    # /sightings  (capture feed, review, actions)
│   │   ├── ingest.py       # /ingest/capture  (device API, X-API-Key)
│   │   ├── reports.py      # /reports, /activity, /activity/export.csv
│   │   ├── admin.py        # /admin/approvals, /admin/users
│   │   └── dashboard.py    # /dashboard
│   ├── templates/          # Jinja2 pages
│   └── static/             # CSS + JS (purple theme, report.css for print)
└── tests/
    ├── conftest.py
    ├── test_auth.py        # sign-up, approval gate, lockout, RBAC
    ├── test_google.py      # Google OAuth flow (mocked endpoints)
    ├── test_home.py        # public landing page
    ├── test_individuals.py # register CRUD + search + access control
    ├── test_matching.py    # scoring, bands, /match flow
    ├── test_reports.py     # reports overview, CSV exports, case reports, activity log
    └── test_capture.py     # CCTV ingestion, re-identification, profiles, sightings console
```

---

## 6. Deployment (putting it on a real domain)

IMANI ships with everything needed to run on a server behind **nginx** with
**HTTPS**: `wsgi.py`, `gunicorn.conf.py`, a systemd unit, an nginx config and a
full guide in **[`deploy/DEPLOY.md`](deploy/DEPLOY.md)**.

```bash
# On the server, after setting up .env and the venv:
gunicorn --config gunicorn.conf.py wsgi:app
```

Setting `IMANI_ENV=production` enables:

- `SESSION_COOKIE_SECURE` (cookies sent over HTTPS only)
- `PREFERRED_URL_SCHEME=https` and `ProxyFix` (correct URLs behind nginx)
- a startup guard that **refuses to boot with the default secret key**

Quick summary of the domain flow: register the domain (e.g. at HostAfrica) →
point its `A` record at your server → deploy as above → add
`https://<domain>/auth/google/callback` to the Google OAuth client. Full detail
is in `deploy/DEPLOY.md`.

---

## 7. Roadmap

- **Bit 1 (done)** — Authentication: sign in, access requests, approval, RBAC, audit.
- **Bit 2 (done)** — Google sign-in (OAuth 2.0 / OIDC) on login and sign-up.
- **Bit 3 (done)** — Production deployment: gunicorn, nginx, systemd, HTTPS, `.env`.
- **Bit 4 (done)** — Public landing page at `/`; dashboard moved to `/dashboard`; purple theme.
- **Bit 5 (done)** — The individual register (search / insert / update / delete).
- **Bit 6 (done)** — Matching engine: weighted trait scoring, confidence bands, saved runs.
- **Bit 7 (done)** — Reporting & exports: reports console, printable case/profile reports, CSV exports, activity log.
- **Bit 8 (done)** — CCTV capture & re-identification: ingestion API, sightings, auto-enrolment, learning identity profiles.
- **Bit 9+** — Deployment hardening and next increments.
