"""IMANI package — application factory."""
from __future__ import annotations

import os

from flask import Flask, render_template

from . import auth, db
from .config import DEFAULT_SECRET_KEY, ProductionConfig, Config
from .security import init_csrf


def create_app(config_object: type | None = None) -> Flask:
    """Application factory."""
    if config_object is None:
        config_object = (
            ProductionConfig
            if os.environ.get("IMANI_ENV") == "production"
            else Config
        )

    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(config_object)

    # Refuse to run in production with the placeholder secret key.
    if app.config.get("ENVIRONMENT") == "production":
        if app.config["SECRET_KEY"] == DEFAULT_SECRET_KEY:
            raise RuntimeError(
                "Refusing to start in production with the default SECRET_KEY. "
                "Set IMANI_SECRET_KEY to a long, random value."
            )

    os.makedirs(app.instance_path, exist_ok=True)

    # Behind nginx: trust X-Forwarded-Proto/Host/For so HTTPS and redirect URIs
    # are built correctly.
    if app.config.get("BEHIND_PROXY"):
        from werkzeug.middleware.proxy_fix import ProxyFix

        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    db.init_app(app)
    init_csrf(app)

    # --- Blueprints ---------------------------------------------------
    from .routes import admin, auth_routes, dashboard, home, individuals

    app.register_blueprint(home.bp)
    app.register_blueprint(auth_routes.bp)
    app.register_blueprint(individuals.bp)
    app.register_blueprint(admin.bp)
    app.register_blueprint(dashboard.bp)

    # --- Template globals --------------------------------------------
    @app.context_processor
    def inject_globals() -> dict:
        from . import oauth

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
            "google_enabled": oauth.is_configured(app.config),
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
