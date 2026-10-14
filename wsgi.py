"""WSGI entry point for production servers (gunicorn / uWSGI).

    gunicorn --config gunicorn.conf.py wsgi:app
"""
from __future__ import annotations

from imani import create_app

app = create_app()
