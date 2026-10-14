"""IMANI application entry point.

Run the development server with:

    flask --app app run --debug

or simply:

    python app.py
"""
from __future__ import annotations

import os

from imani import create_app

app = create_app()


if __name__ == "__main__":
    # Port 5000 is often taken by other local services; override with
    #   IMANI_PORT=5055 python app.py
    app.run(
        host=os.environ.get("IMANI_HOST", "127.0.0.1"),
        port=int(os.environ.get("IMANI_PORT", "5000")),
        debug=True,
    )
