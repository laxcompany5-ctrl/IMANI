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
│   ├── schema.sql          # users + audit_log + individuals
│   ├── db.py               # SQLite helpers + CLI (init-db, reset-db, seed, migrate)
│   ├── security.py         # CSRF protection
│   ├── validation.py       # username/email/password + register field rules
│   ├── models.py           # UserRepository, IndividualRepository, AuditRepository
│   ├── auth.py             # authenticate, sessions, roles, lockout
│   ├── oauth.py            # Google OAuth 2.0 / OIDC (stdlib only)
│   ├── routes/
│   │   ├── home.py         # /  (public landing page)
│   │   ├── auth_routes.py  # login, signup, logout, /auth/google[/callback]
│   │   ├── individuals.py  # /individuals  (search / insert / update / delete)
│   │   ├── admin.py        # /admin/approvals, /admin/users
│   │   └── dashboard.py    # /dashboard
│   ├── templates/          # Jinja2 pages
│   └── static/             # CSS + JS (purple theme)
└── tests/
    ├── conftest.py
    ├── test_auth.py        # sign-up, approval gate, lockout, RBAC
    ├── test_google.py      # Google OAuth flow (mocked endpoints)
    ├── test_home.py        # public landing page
    └── test_individuals.py # register CRUD + search + access control
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
- **Bit 6+** — Matching engine, reporting, deployment hardening.
