"""Google sign-in (OpenID Connect), sessions and CSRF protection."""
import functools
import hmac
import secrets

from authlib.integrations.base_client.errors import OAuthError
from authlib.integrations.flask_client import OAuth
from flask import (
    Blueprint, abort, current_app, flash, g, jsonify, redirect, render_template, request, session, url_for,
)

from .db import get_db

bp = Blueprint("auth", __name__)
oauth = OAuth()

GOOGLE_METADATA_URL = "https://accounts.google.com/.well-known/openid-configuration"
DEV_USER_SUB = "dev-local-user"


def init_app(app):
    oauth.init_app(app)
    if app.config["GOOGLE_CLIENT_ID"] and app.config["GOOGLE_CLIENT_SECRET"]:
        oauth.register(
            "google",
            client_id=app.config["GOOGLE_CLIENT_ID"],
            client_secret=app.config["GOOGLE_CLIENT_SECRET"],
            server_metadata_url=GOOGLE_METADATA_URL,
            client_kwargs={"scope": "openid email profile"},
        )
    app.before_request(_load_user)
    app.before_request(_check_csrf)
    app.jinja_env.globals["csrf_token"] = csrf_token


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


def _check_csrf():
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    sent = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token") or ""
    if not sent or not hmac.compare_digest(sent, session.get("csrf", "")):
        abort(400, "Your session expired or the form was out of date. Reload the page and try again.")


def _load_user():
    user_id = session.get("user_id")
    g.user = None
    if user_id is not None:
        g.user = get_db().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if g.user is None:
            session.clear()


def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            if request.path.startswith("/api/"):
                return jsonify(error="Not signed in"), 401
            return redirect(url_for("auth.login", next=request.full_path))
        return view(*args, **kwargs)

    return wrapped


def _safe_next(target):
    return target if target and target.startswith("/") and not target.startswith("//") else None


def _upsert_user(sub, email, name, picture):
    db = get_db()
    db.execute(
        """INSERT INTO users (google_sub, email, name, picture) VALUES (?, ?, ?, ?)
           ON CONFLICT (google_sub) DO UPDATE SET
             email = excluded.email, name = excluded.name, picture = excluded.picture""",
        (sub, email, name, picture),
    )
    db.commit()
    return db.execute("SELECT id FROM users WHERE google_sub = ?", (sub,)).fetchone()["id"]


def _start_session(user_id):
    session.clear()
    session["user_id"] = user_id
    session.permanent = True


@bp.get("/login")
def login():
    if g.user is not None:
        return redirect(url_for("views.dashboard"))
    return render_template(
        "login.html",
        google_enabled=oauth.create_client("google") is not None,
        dev_login=current_app.config["DEV_LOGIN"],
        next=_safe_next(request.args.get("next")),
    )


@bp.get("/login/google")
def login_google():
    client = oauth.create_client("google")
    if client is None:
        flash("Google sign-in isn't configured yet. Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in .env.", "error")
        return redirect(url_for("auth.login"))
    session["next"] = _safe_next(request.args.get("next"))
    return client.authorize_redirect(url_for("auth.google_callback", _external=True))


@bp.get("/auth/google/callback")
def google_callback():
    client = oauth.create_client("google")
    if client is None:
        abort(404)
    try:
        token = client.authorize_access_token()
    except OAuthError as exc:
        flash(f"Google sign-in failed: {exc.description or exc.error}", "error")
        return redirect(url_for("auth.login"))
    info = token.get("userinfo") or client.userinfo()
    email = (info.get("email") or "").lower()
    if not email or not info.get("email_verified"):
        flash("Your Google account needs a verified email address.", "error")
        return redirect(url_for("auth.login"))
    allowed = current_app.config["ALLOWED_EMAILS"]
    if allowed and email not in allowed:
        flash(f"{email} isn't allowed to use this app.", "error")
        return redirect(url_for("auth.login"))
    user_id = _upsert_user(info["sub"], email, info.get("name"), info.get("picture"))
    next_url = session.get("next")
    _start_session(user_id)
    return redirect(next_url or url_for("views.dashboard"))


@bp.post("/login/dev")
def login_dev():
    if not current_app.config["DEV_LOGIN"]:
        abort(404)
    _start_session(_upsert_user(DEV_USER_SUB, "dev@localhost", "Local Dev User", None))
    return redirect(_safe_next(request.form.get("next")) or url_for("views.dashboard"))


@bp.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))
