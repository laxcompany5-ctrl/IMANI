"""IMANI package — application factory."""
from __future__ import annotations

import os

from flask import Flask, render_template

from . import auth, db
from .config import Config
from .security import init_csrf


def create_app(config_object: type | None = None) -> Flask:
    """Application factory."""
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(config_object or Config)

    os.makedirs(app.instance_path, exist_ok=True)

    db.init_app(app)
    init_csrf(app)

    # --- Blueprints ---------------------------------------------------
    from .routes import admin, auth_routes, dashboard

    app.register_blueprint(auth_routes.bp)
    app.register_blueprint(admin.bp)
    app.register_blueprint(dashboard.bp)

    # --- Template globals --------------------------------------------
    @app.context_processor
    def inject_globals() -> dict:
        user = auth.current_user()
        pending = 0
        if user is not None and user["role"] == "admin":
            from .models import UserRepository

            pending = UserRepository.count_by_status().get("pending", 0)
        return {
            "app_name": app.config["APP_NAME"],
            "app_tagline": app.config["APP_TAGLINE"],
            "current_user": user,
            "pending_count": pending,
        }

    # --- Error pages --------------------------------------------------
    @app.errorhandler(400)
    def bad_request(error):
        return render_template("error.html", code=400,
                               message=getattr(error, "description", "Bad request.")), 400

    @app.errorhandler(403)
    def forbidden(error):
        return render_template("error.html", code=403,
                               message="You are not authorised to view this page."), 403

    @app.errorhandler(404)
    def not_found(error):
        return render_template("error.html", code=404,
                               message="We could not find what you were looking for."), 404

    return app
