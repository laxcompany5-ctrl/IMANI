# IMANI — Security Intelligence Console

A security-analyst web app built in small, working increments ("bits").

> **Bit 1 (this commit) — Authentication:** sign-in and a self-service
> access-request (sign-up) form. New accounts stay **pending** until an
> administrator approves them, so no unvetted user can reach the console.

---

## 1. Stack

- **Python 3 + Flask 3** (server-rendered Jinja2 templates)
- **SQLite** via the standard-library `sqlite3` (no ORM)
- **Werkzeug** scrypt password hashing
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

---

## 5. Project structure

```
imani-web/
├── app.py                  # entry point (env-configurable host/port)
├── requirements.txt
├── pytest.ini
├── imani/
│   ├── __init__.py         # application factory
│   ├── config.py           # configuration
│   ├── schema.sql          # users + audit_log
│   ├── db.py               # SQLite helpers + CLI (init-db, reset-db, seed)
│   ├── security.py         # CSRF protection
│   ├── validation.py       # username/email/password rules
│   ├── models.py           # UserRepository, AuditRepository
│   ├── auth.py             # authenticate, sessions, roles, lockout
│   ├── routes/
│   │   ├── auth_routes.py  # /auth/login, /auth/signup, /auth/logout
│   │   ├── admin.py        # /admin/approvals, /admin/users
│   │   └── dashboard.py    # /
│   ├── templates/          # Jinja2 pages
│   └── static/             # CSS + JS
└── tests/
    ├── conftest.py
    └── test_auth.py        # 11 tests: sign-up, approval gate, lockout, RBAC
```

---

## 6. Roadmap

- **Bit 1 (done)** — Authentication: sign in, access requests, approval, RBAC, audit.
- **Bit 2** — Analyst dashboard shell & navigation.
- **Bit 3** — The individual register (search / insert / update / delete).
- **Bit 4+** — Matching engine, reporting, deployment hardening.
