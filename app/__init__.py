"""Portfolio Manager web application."""
import logging
import os
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, render_template
from werkzeug.middleware.proxy_fix import ProxyFix

DEFAULT_SECRET = "dev-insecure-change-me"
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _flag(name, default="false"):
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


def create_app(test_config=None):
    load_dotenv(PROJECT_ROOT / ".env")
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY") or DEFAULT_SECRET,
        DATABASE=os.environ.get("DATABASE_PATH") or os.path.join(app.instance_path, "portfolio.db"),
        GOOGLE_CLIENT_ID=os.environ.get("GOOGLE_CLIENT_ID", ""),
        GOOGLE_CLIENT_SECRET=os.environ.get("GOOGLE_CLIENT_SECRET", ""),
        ALLOWED_EMAILS={e.strip().lower() for e in os.environ.get("ALLOWED_EMAILS", "").split(",") if e.strip()},
        DEV_LOGIN=_flag("DEV_LOGIN"),
        BASE_CURRENCY="CAD",
        BENCHMARK_SYMBOL="VFV.TO",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=_flag("SESSION_COOKIE_SECURE"),
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
        MAX_CONTENT_LENGTH=5 * 1024 * 1024,
    )
    if test_config:
        app.config.update(test_config)
    # Behind a proxy that terminates HTTPS (e.g. PythonAnywhere), trust its X-Forwarded-*
    # headers so external URLs such as the Google callback use https.
    if _flag("BEHIND_PROXY", "true" if "PYTHONANYWHERE_DOMAIN" in os.environ else "false"):
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    os.makedirs(app.instance_path, exist_ok=True)

    logging.basicConfig(level=logging.INFO)
    if app.config["SECRET_KEY"] == DEFAULT_SECRET and not app.testing:
        app.logger.warning("SECRET_KEY is not set; using an insecure development key.")

    from . import auth, db, views

    db.init_app(app)
    auth.init_app(app)
    app.register_blueprint(auth.bp)
    app.register_blueprint(views.bp)

    @app.errorhandler(400)
    @app.errorhandler(404)
    def http_error(err):
        return render_template("error.html", error=err), err.code

    return app
