"""Gunicorn configuration for IMANI.

Usage:
    gunicorn --config gunicorn.conf.py wsgi:app

Tune via environment variables (see .env.example).
"""
from __future__ import annotations

import multiprocessing
import os

# Bind to localhost only — nginx terminates TLS and proxies to us.
bind = os.environ.get("GUNICORN_BIND", "127.0.0.1:8000")

# SQLite is the datastore, so keep the worker count modest and use threads.
workers = int(os.environ.get("GUNICORN_WORKERS", min(4, multiprocessing.cpu_count())))
threads = int(os.environ.get("GUNICORN_THREADS", "4"))

timeout = int(os.environ.get("GUNICORN_TIMEOUT", "30"))
graceful_timeout = 30
keepalive = 5
max_requests = 1000
max_requests_jitter = 100

accesslog = "-"          # stdout (captured by systemd/journald)
errorlog = "-"
loglevel = os.environ.get("GUNICORN_LOGLEVEL", "info")
