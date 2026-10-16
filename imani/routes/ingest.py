"""Machine-to-machine ingestion API for CCTV deployments (Bit 8).

A capture device authenticates with a shared key in the ``X-API-Key`` header.
This route is exempt from the session CSRF check (see ``config``) because it is
not driven by a browser session. The API is disabled — every request is
rejected — until ``IMANI_INGEST_API_KEY`` is set.
"""
from __future__ import annotations

from flask import Blueprint, current_app, jsonify, request

from .. import capture

bp = Blueprint("ingest", __name__, url_prefix="/ingest")


@bp.route("/capture", methods=("POST",), endpoint="capture")
def ingest_capture():
    """Accept one detected person and resolve their identity."""
    key = current_app.config.get("INGEST_API_KEY", "")
    provided = request.headers.get("X-API-Key")
    if not capture.api_key_ok(provided, key):
        # No key configured, or the wrong one: an identical response, so the
        # endpoint reveals nothing about whether ingestion is switched on.
        return jsonify({"error": "invalid or missing API key"}), 403

    payload = request.get_json(silent=True)
    if payload is None:
        # Also accept form-encoded bodies from simple devices.
        payload = request.form.to_dict() or None
    if not payload:
        return jsonify({"error": "expected a JSON object body"}), 400

    try:
        result = capture.ingest(payload, actor="cctv")
    except capture.CaptureError as exc:
        return jsonify({"error": "invalid capture", "problems": exc.problems}), 400

    return jsonify(result), 201
