"""IMANI application entry point.

Run the development server with:

    flask --app app run --debug

or simply:

    python app.py

To serve HTTPS (required for phone camera access on a LAN), run:

    IMANI_HTTPS=1 IMANI_PORT=5443 python app.py
"""
from __future__ import annotations

import os
from pathlib import Path

from imani import create_app

app = create_app()

BASE_DIR = Path(__file__).resolve().parent


def _https_enabled() -> bool:
    return os.environ.get("IMANI_HTTPS", "0").lower() in ("1", "true", "yes", "on")


def _ssl_context():
    """Return a Flask ``ssl_context`` when HTTPS is requested.

    A self-signed certificate is created on first use under
    ``instance/certs/`` so phones can reach the app over TLS. Browsers will
    show a "not private" warning on a self-signed certificate; the operator
    accepts it once on the phone. For a warning-free deployment use a real
    certificate (or a tunnel) instead.
    """
    if not _https_enabled():
        return None

    cert = os.environ.get("IMANI_SSL_CERT") or str(
        BASE_DIR / "instance" / "certs" / "imani.crt"
    )
    key = os.environ.get("IMANI_SSL_KEY") or str(
        BASE_DIR / "instance" / "certs" / "imani.key"
    )
    try:
        from imani.certs import ensure_self_signed
        from imani.qr import lan_ip

        address = lan_ip()
        cert, key = ensure_self_signed(cert, key, [address] if address else [])
        return (cert, key)
    except Exception as exc:  # pragma: no cover - environment dependent
        print(f"[IMANI] Could not prepare a TLS certificate: {exc}")
        print("[IMANI] Falling back to HTTP. Phone cameras need HTTPS.")
        return None


if __name__ == "__main__":
    ssl_context = _ssl_context()
    scheme = "https" if ssl_context else "http"
    host = os.environ.get("IMANI_HOST", "127.0.0.1")
    port = int(os.environ.get("IMANI_PORT", "5000"))
    print(f"[IMANI] {scheme}://{host}:{port}")
    if scheme == "https":
        print("[IMANI] Accept the self-signed certificate warning on the phone once.")
    debug = os.environ.get("IMANI_DEBUG", "1").lower() in ("1", "true", "yes", "on")
    # The reloader watches the whole project, including instance/video_imports.
    # Writing a 4K snapshot or ZIP would then restart the server mid-request, so
    # it is opt-in for code work only.
    reloader = os.environ.get("IMANI_RELOADER", "0").lower() in ("1", "true", "yes", "on")
    app.run(
        host=host, port=port, debug=debug, use_reloader=reloader,
        ssl_context=ssl_context,
    )
