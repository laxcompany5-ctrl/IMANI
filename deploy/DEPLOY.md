# Deploying IMANI to a server (e.g. a HostAfrica VPS)

This guide targets a fresh **Ubuntu/Debian VPS** with a domain pointed at it.
The app runs under **gunicorn**, behind **nginx**, with **HTTPS**.

Replace `imani.co.ke` and `/opt/imani` with your domain and chosen path.

---

## 0. Point the domain at the server

In the HostAfrica control panel add DNS records:

| Type | Name | Value |
|------|------|-------|
| A | `@` | `<your VPS public IP>` |
| A | `www` | `<your VPS public IP>` |

Wait for DNS to propagate (`dig +short imani.co.ke` should print the IP).

---

## 1. Install system packages

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip nginx git
```

## 2. Create a service user and copy the app

```bash
sudo useradd --system --create-home --shell /usr/sbin/nologin imani
sudo mkdir -p /opt/imani
sudo chown imani:imani /opt/imani

# From your machine:
#   rsync -av --exclude instance --exclude .venv ./ user@server:/tmp/imani/
sudo -u imani git clone <your-repo-url> /opt/imani   # or copy the files in
cd /opt/imani
```

## 3. Python environment and dependencies

```bash
sudo -u imani python3 -m venv /opt/imani/.venv
sudo -u imani /opt/imani/.venv/bin/pip install -r requirements.txt
```

## 4. Configure the environment

```bash
sudo -u imani cp .env.example .env
sudo -u imani python3 -c "import secrets; print(secrets.token_urlsafe(48))"  # secret key
sudo -u imani nano .env
```

Set at minimum:

```
IMANI_ENV=production
IMANI_SECRET_KEY=<the value you just generated>
IMANI_GOOGLE_CLIENT_ID=...
IMANI_GOOGLE_CLIENT_SECRET=...
IMANI_GOOGLE_REDIRECT_URI=https://imani.co.ke/auth/google/callback
IMANI_ADMIN_PASS=<a strong password>
```

> `IMANI_ENV=production` enables HTTPS-only cookies, `ProxyFix` and refuses to
> boot if `IMANI_SECRET_KEY` is still the default — a deliberate safety guard.

## 5. Create the database and the administrator

```bash
cd /opt/imani
sudo -u imani /opt/imani/.venv/bin/flask --app app init-db
sudo -u imani /opt/imani/.venv/bin/flask --app app seed
```

## 6. Run IMANI as a service

```bash
sudo cp deploy/imani.service /etc/systemd/system/imani.service
sudo systemctl daemon-reload
sudo systemctl enable --now imani
sudo systemctl status imani        # should be active (running)
```

## 7. Put nginx in front

```bash
sudo cp deploy/nginx-imani.conf /etc/nginx/sites-available/imani
sudo ln -s /etc/nginx/sites-available/imani /etc/nginx/sites-enabled/imani
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
```

## 8. Enable HTTPS (free, automatic)

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d imani.co.ke -d www.imani.co.ke
```

Certbot rewrites the nginx block for HTTPS and sets up auto-renewal.

## 9. Finish the Google setup

In **Google Cloud Console → Credentials → your OAuth client**, add the
**Authorised redirect URI**:

```
https://imani.co.ke/auth/google/callback
```

Then browse to **https://imani.co.ke** and sign in.

---

## Updating the app later

```bash
cd /opt/imani
sudo -u imani git pull
sudo -u imani /opt/imani/.venv/bin/pip install -r requirements.txt
sudo -u imani /opt/imani/.venv/bin/flask --app app migrate   # apply schema changes
sudo systemctl restart imani
```

## Backups

The whole dataset is one file (`instance/imani.sqlite3`) plus its WAL sidecars.
Back it up with SQLite's own tooling for a consistent snapshot:

```bash
sudo -u imani sqlite3 /opt/imani/instance/imani.sqlite3 ".backup '/opt/imani/instance/backup-$(date +%F).sqlite3'"
```

Add that to cron for daily backups.

---

## Alternative: a PaaS (no server admin)

If you would rather not manage a VPS, the included `Procfile` works on
Render/Railway/Fly.io. Set the same environment variables in their dashboard,
run `flask --app app init-db && flask --app app seed` once, and point your
HostAfrica domain's DNS at the platform. Note that SQLite needs **persistent
storage** on these platforms — mount a volume at `/opt/imani/instance` (or set
`IMANI_DATABASE` to a path on the volume).
