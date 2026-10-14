"""Public landing page."""
from __future__ import annotations

from flask import Blueprint, render_template

bp = Blueprint("home", __name__)


@bp.route("/")
def index():
    """The public front door — visible to everyone, signed in or not."""
    return render_template("home.html")
