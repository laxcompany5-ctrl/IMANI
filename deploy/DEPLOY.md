# Deploy IMANI to Oracle Cloud Always Free

This is the permanent-hosting route for the full app: **Flask + SQLite + local
media + camera monitor threads** running on an Oracle Cloud **Always Free ARM
VM**, behind nginx with a free Let's Encrypt certificate.

Target repository: `https://github.com/Laxcompany5/IMANI`  
Example public URL below: `https://imani-demo.duckdns.org`

> Why a VPS and not a free PaaS? Render/Koyeb/Hugging Face free tiers have
> ephemeral disks and sleep; PythonAnywhere free limits outbound internet and
> CPU. IMANI needs a persistent SQLite database, stored video/4K snapshots and
> long-lived camera threads. An Always Free VM supplies all three at no cost.

---

## 0. Create the Oracle Always Free VM

1. Sign up at <https://www.oracle.com/cloud/free/>.
2. Create an **ARM Ampere A1** instance:
   - Shape: `VM.Standard.A1.Flex`
   - 2 OCPU / 12 GB RAM is a safe request; 1 OCPU / 6 GB also works. Use the
     largest free capacity your region offers (up to 4 OCPU / 24 GB).
   - Image: **Ubuntu 24.04 ARM64**
   - Boot volume: 50 GB (within the Always Free allowance).
3. Add your SSH public key when prompted. Open ingress ports **22**, **80** and
   **443** in the VCN security list (restrict 22 to your own IP if possible).
4. Note the instance **public IP**.
5. Add a 50 GB **Block Volume** and attach it to the instance. This stays within
   the 200 GB Always Free block-storage allowance.

> Do **not** upgrade the trial account to a paid account. Keep usage inside the
> Always Free limits so the bill remains $0.

---

## 1. Point a free hostname at the VM

Use a free DuckDNS name (or any domain you already own):

1. Go to <https://www.duckdns.org/>, sign in and create a subdomain, e.g.
   `imani-demo`.
2. Set its IP to the VM public IP.
3. Keep the `DUCKDNS_TOKEN` private — it is only used to update the DNS record.

Replace `imani-demo.duckdns.org` everywhere below with your real hostname.

---

## 2. Install packages and mount the persistent volume

SSH into the VM as the Ubuntu user, then:

```bash
sudo apt update
sudo apt upgrade -y
sudo apt install -y python3-venv python3-dev build-essential nginx git curl \
                   ffmpeg sqlite3 libgl1 libglib2.0-0
```

Find the attached block device (often `/dev/oracleoci/oraclevdb` or
`/dev/sdb`; **do not guess** — check `lsblk`):

```bash
lsblk
sudo mkfs.ext4 -F /dev/<YOUR_BLOCK_DEVICE>
sudo mkdir -p /mnt/imani-data
sudo mount /dev/<YOUR_BLOCK_DEVICE> /mnt/imani-data
```

Make the mount permanent:

```bash
UUID=$(sudo blkid -s UUID -o value /dev/<YOUR_BLOCK_DEVICE>)
echo "UUID=$UUID /mnt/imani-data ext4 defaults,nofail 0 2" | sudo tee -a /etc/fstab
sudo mount -a
df -h /mnt/imani-data
```

---

## 3. Clone IMANI and connect its `instance/` directory to the volume

```bash
sudo useradd --system --create-home --shell /usr/sbin/nologin imani
sudo mkdir -p /opt/imani /mnt/imani-data/instance /mnt/imani-data/backups
sudo chown -R imani:imani /opt/imani /mnt/imani-data

sudo -u imani git clone https://github.com/Laxcompany5/IMANI.git /opt/imani
cd /opt/imani

# The repo intentionally ignores instance/. Replace the empty folder with a
# symlink so the database, uploads, video imports and 4K snapshots live on the
# persistent block volume.
sudo rm -rf /opt/imani/instance
sudo -u imani ln -s /mnt/imani-data/instance /opt/imani/instance
```

Create the Python environment:

```bash
sudo -u imani python3 -m venv /opt/imani/.venv
sudo -u imani /opt/imani/.venv/bin/pip install --upgrade pip
sudo -u imani /opt/imani/.venv/bin/pip install -r /opt/imani/requirements.txt
```

---

## 4. Configure the production environment

```bash
sudo -u imani python3 -c "import secrets; print(secrets.token_urlsafe(48))"
sudo -u imani python3 -c "import secrets; print(secrets.token_urlsafe(32))"
sudo -u imani cp /opt/imani/.env.example /opt/imani/.env
sudo -u imani chmod 600 /opt/imani/.env
sudo -u imani nano /opt/imani/.env
```

Set these values (use the generated strings for the two secrets):

```ini
IMANI_ENV=production
IMANI_SECRET_KEY=<long random string>
IMANI_PUBLIC_URL=https://imani-demo.duckdns.org
IMANI_ADMIN_USER=admin
IMANI_ADMIN_EMAIL=admin@imani.local
IMANI_ADMIN_PASS=<strong admin password>
IMANI_INGEST_API_KEY=<second random string>
IMANI_MEDIA_ROOT=/opt/imani/instance/media
IMANI_VIDEO_IMPORT_ROOT=/opt/imani/instance/video_imports
GUNICORN_BIND=127.0.0.1:8000
GUNICORN_WORKERS=1
GUNICORN_THREADS=8
GUNICORN_TIMEOUT=300
```

Important:

- `IMANI_ENV=production` enables secure cookies, `ProxyFix` and refuses to boot
  with the default secret key.
- `IMANI_PUBLIC_URL` must be the final `https://` URL — QR pairing codes use it.
- `GUNICORN_WORKERS=1` is deliberate. Camera monitors and session state are
  in-process; multiple workers would duplicate monitors. Use threads instead.

---

## 5. Initialise the database and seed data

```bash
cd /opt/imani
sudo -u imani /opt/imani/.venv/bin/flask --app app init-db
sudo -u imani /opt/imani/.venv/bin/flask --app app seed
sudo -u imani /opt/imani/.venv/bin/flask --app app migrate
sudo -u imani /opt/imani/.venv/bin/flask --app app publish-imports
```

`seed` creates the admin only if no users exist. `publish-imports` is safe to
re-run; it publishes any finished video imports to the Videos/Snapshots library.

---

## 6. Run the app as a systemd service

```bash
sudo cp /opt/imani/deploy/imani.service /etc/systemd/system/imani.service
sudo systemctl daemon-reload
sudo systemctl enable --now imani
sudo systemctl status imani
```

Logs:

```bash
journalctl -u imani -n 100 --no-pager
```

---

## 7. Put nginx in front

```bash
sudo cp /opt/imani/deploy/nginx-imani.conf /etc/nginx/sites-available/imani
sudo sed -i 's/imani-demo.duckdns.org/<YOUR_HOSTNAME>/g' \
  /etc/nginx/sites-available/imani
sudo ln -sf /etc/nginx/sites-available/imani /etc/nginx/sites-enabled/imani
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
```

---

## 8. Add free HTTPS

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d imani-demo.duckdns.org \
  --non-interactive --agree-tos -m you@example.com --redirect
sudo systemctl status certbot.timer
```

Certbot renews automatically. If the DuckDNS IP changes after a reboot, update
the DNS record and re-run `certbot renew` if needed.

---

## 9. Verify the permanent deployment

```bash
curl -I https://imani-demo.duckdns.org/
curl -sS -o /dev/null -w '%{http_code}\n' https://imani-demo.duckdns.org/auth/login
systemctl is-active imani nginx
```

In the browser, check:

| URL | Expected |
|-----|----------|
| `/` | Purple public landing page |
| `/auth/login` | Sign in with the admin from `.env` |
| `/dashboard` | Signed-in console |
| `/cameras/connect` | HTTPS QR that a phone can scan directly |
| `/cameras/live` | Connected devices list |
| `/videos` and `/snapshots` | Published library media |

Phone camera access works because the public URL is a secure context. No
self-signed certificate warning is needed.

---

## 10. Backups and updates

Back up the SQLite database daily:

```bash
sudo mkdir -p /mnt/imani-data/backups
sudo -u imani sqlite3 /opt/imani/instance/imani.sqlite3 \
  ".backup '/mnt/imani-data/backups/imani-$(date +%F).sqlite3'"
```

Add a cron entry with `crontab -e`:

```cron
0 2 * * * /usr/bin/sqlite3 /opt/imani/instance/imani.sqlite3 ".backup '/mnt/imani-data/backups/imani-$(date +\%F).sqlite3'"
```

Update the app:

```bash
cd /opt/imani
sudo -u imani git pull
sudo -u imani /opt/imani/.venv/bin/pip install -r requirements.txt
sudo -u imani /opt/imani/.venv/bin/flask --app app migrate
sudo -u imani /opt/imani/.venv/bin/flask --app app publish-imports
sudo systemctl restart imani
```

---

## Notes and limits

- Keep `.env`, the database and media out of GitHub. The repo contains only
  source, docs and deployment templates.
- Google sign-in remains inactive until OAuth client ID/secret and the
  `https://<host>/auth/google/callback` redirect URI are configured.
- 4K snapshots are genuine 3840×2160 upscales with padding; they do not recover
  detail that was absent in the source video.
- The free Oracle ARM allocation can be capacity-constrained by region. If
  2 OCPU is unavailable, retry with 1 OCPU/6 GB or another Always Free region.
