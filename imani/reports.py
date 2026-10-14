"""Helpers for reporting and CSV exports."""
from __future__ import annotations

import csv
import io
from datetime import datetime

from flask import Response


def csv_response(filename: str, header, rows) -> Response:
    """Build a downloadable ``text/csv`` response.

    Values are written with :mod:`csv`, so commas, quotes and newlines in the
    data are escaped correctly.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(header)
    writer.writerows(rows)
    return Response(
        buffer.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def timestamp() -> str:
    """A readable 'generated at' stamp for printable reports."""
    return datetime.now().strftime("%Y-%m-%d %H:%M")
