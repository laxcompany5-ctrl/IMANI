"""Gunicorn configuration for IMANI.

Usage:
    gunicorn --config gunicorn.conf.py wsgi:app

Tune via environment variables (see .env.example).
"""
from __future__ import annotations

import os

# Bind to localhost only — nginx terminates TLS and proxies to us.
bind = os.environ.get("GUNICORN_BIND", "127.0.0.1:8000")

# IMANI keeps camera monitors and session state in-process. ONE worker is
# required; scale with threads, not workers, or monitors get duplicated.
workers = int(os.environ.get("GUNICORN_WORKERS", "1"))
threads = int(os.environ.get("GUNICORN_THREADS", "8"))

# Video imports can take minutes, so allow long requests.
timeout = int(os.environ.get("GUNICORN_TIMEOUT", "300"))
graceful_timeout = 60
keepalive = 5
# Never recycle the worker: recycling would stop live camera monitor threads.
max_requests = 0
max_requests_jitter = 0

accesslog = "-"          # stdout (captured by systemd/journald)
errorlog = "-"
loglevel = os.environ.get("GUNICORN_LOGLEVEL", "info")
