"""Optional panel authentication, persisted separately from contact backups."""

import os
import secrets
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit

import click
from flask import (
    Blueprint, Response, current_app, flash, g, jsonify, redirect,
    render_template, request, session, url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

from .i18n import get_language_options, get_message, get_ui_strings

bp = Blueprint("security", __name__)
LOGIN_LIMIT = 5
LOGIN_WINDOW = 300
SECURITY_SCHEMA_VERSION = 1


def _connection():
    connection = sqlite3.connect(current_app.config["SECURITY_DATABASE"], timeout=30)
    connection.row_factory = sqlite3.Row
    return connection


def initialize_security(app):
    path = Path(app.config["SECURITY_DATABASE"])
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
    os.close(descriptor)
    with app.app_context(), closing(_connection()) as db:
        db.execute("BEGIN IMMEDIATE")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version > SECURITY_SCHEMA_VERSION:
            raise RuntimeError("Panel security schema is newer than supported; refusing to start.")
        if version == 0:
            db.execute("""CREATE TABLE IF NOT EXISTS panel_security (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                auth_enabled INTEGER NOT NULL DEFAULT 0 CHECK (auth_enabled IN (0, 1)),
                username TEXT NOT NULL DEFAULT 'admin',
                password_hash TEXT NOT NULL DEFAULT '',
                revision TEXT NOT NULL,
                secret_key TEXT NOT NULL
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS login_attempts (
                client TEXT PRIMARY KEY, failures INTEGER NOT NULL, started REAL NOT NULL
            )""")
            db.execute("""INSERT OR IGNORE INTO panel_security
                (id, revision, secret_key) VALUES (1, ?, ?)""",
                (secrets.token_hex(32), secrets.token_hex(32)))
            db.execute(f"PRAGMA user_version = {SECURITY_SCHEMA_VERSION}")
        stored = db.execute("SELECT secret_key FROM panel_security WHERE id = 1").fetchone()
        if stored is None:
            raise RuntimeError("Panel security settings are missing; refusing to start.")
        stored_key = stored[0]
        db.commit()
    if not app.config.get("SECRET_KEY") or app.config["SECRET_KEY"] == "change-me":
        app.config["SECRET_KEY"] = stored_key

    @app.cli.command("reset-panel-auth")
    @click.option("--yes", is_flag=True, help="Reset without interactive confirmation.")
    def reset_panel_auth(yes):
        """Disable panel login after losing the administrator password."""
        if not yes:
            click.confirm("Disable panel login and invalidate existing sessions?", abort=True)
        with closing(_connection()) as db:
            db.execute("UPDATE panel_security SET auth_enabled = 0, password_hash = '', revision = ? WHERE id = 1",
                       (secrets.token_hex(32),))
            db.execute("DELETE FROM login_attempts")
            db.commit()
        click.echo("Panel authentication disabled. Contact data has not been changed.")


def get_security_settings():
    if "panel_security" not in g:
        with closing(_connection()) as db:
            row = db.execute("SELECT auth_enabled, username, password_hash, revision FROM panel_security WHERE id = 1").fetchone()
        if row is None:
            raise RuntimeError("Panel security settings are missing; refusing to grant access.")
        g.panel_security = dict(row)
    return g.panel_security


def csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]


def _valid_csrf():
    expected = session.get("csrf_token", "")
    return bool(expected) and secrets.compare_digest(expected.encode(), request.form.get("csrf_token", "").encode())


def _authenticated(settings):
    return settings["auth_enabled"] and session.get("auth_revision") == settings["revision"]


def _page_context():
    from .routes import _get_language
    language = _get_language()
    return {
        "ui": get_ui_strings(language), "current_language": language,
        "languages": get_language_options(),
        "automatic_language": session.get("language") not in get_language_options(),
    }


def _message(key):
    from .routes import _get_language
    return get_message(_get_language(), key)


def _rotate_session(settings=None):
    language = session.get("language")
    session.clear()
    if language in get_language_options():
        session["language"] = language
    if settings and settings["auth_enabled"]:
        session["auth_revision"] = settings["revision"]
    csrf_token()


@bp.app_context_processor
def security_context():
    settings = get_security_settings()
    return {
        "csrf_token": csrf_token(),
        "panel_auth_enabled": bool(settings["auth_enabled"]),
        "panel_authenticated": bool(_authenticated(settings)),
    }


@bp.before_app_request
def protect_panel():
    public = {"main.phonebook", "main.set_language", "security.login", "static"}
    if request.endpoint is None or request.endpoint in public:
        return None
    settings = get_security_settings()
    if not settings["auth_enabled"]:
        return None
    if not _authenticated(settings):
        if request.endpoint == "main.status_api":
            return jsonify(error="authentication_required"), 401
        next_path = request.full_path.rstrip("?") if request.method == "GET" else url_for("main.index")
        return redirect(url_for("security.login", next=next_path))
    if request.method == "POST" and not _valid_csrf():
        return Response(_message("security_invalid_request"), status=403)
    return None


@bp.after_app_request
def prevent_panel_caching(response):
    if request.endpoint not in {"main.phonebook", "static"}:
        response.headers["Cache-Control"] = "no-store"
    return response


def _safe_next(value):
    try:
        parsed = urlsplit(value)
    except ValueError:
        return url_for("main.index")
    if value.startswith("/") and not value.startswith("//") and "\\" not in value and not parsed.scheme and not parsed.netloc:
        return value
    return url_for("main.index")


def _login_blocked(client):
    with closing(_connection()) as db:
        row = db.execute("SELECT failures, started FROM login_attempts WHERE client = ?", (client,)).fetchone()
    return row and row["failures"] >= LOGIN_LIMIT and time.time() - row["started"] < LOGIN_WINDOW


def _record_login_failure(client):
    now = time.time()
    with closing(_connection()) as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("DELETE FROM login_attempts WHERE started <= ?", (now - LOGIN_WINDOW,))
        db.execute("""INSERT INTO login_attempts (client, failures, started) VALUES (?, 1, ?)
            ON CONFLICT(client) DO UPDATE SET failures = failures + 1""", (client, now))
        db.commit()


@bp.route("/login", methods=["GET", "POST"])
def login():
    settings = get_security_settings()
    if not settings["auth_enabled"] or _authenticated(settings):
        return redirect(url_for("main.index"))
    if request.method == "POST":
        if not _valid_csrf():
            return Response(_message("security_invalid_request"), status=403)
        client = request.remote_addr or "unknown"
        if _login_blocked(client):
            flash(_message("security_login_limited"), "error")
            return render_template("security.html", page_kind="login", **_page_context()), 429
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password", "")
        if (len(password) <= 1024 and username == settings["username"]
                and check_password_hash(settings["password_hash"], password)):
            # Re-read after verification in case another worker changed credentials.
            g.pop("panel_security", None)
            fresh = get_security_settings()
            if fresh["revision"] == settings["revision"]:
                with closing(_connection()) as db:
                    db.execute("DELETE FROM login_attempts WHERE client = ?", (client,))
                    db.commit()
                _rotate_session(fresh)
                return redirect(_safe_next(request.args.get("next", "/")))
        _record_login_failure(client)
        flash(_message("security_login_failed"), "error")
    return render_template("security.html", page_kind="login", **_page_context())


@bp.route("/logout", methods=["POST"])
def logout():
    _rotate_session()
    return redirect(url_for("security.login"))


@bp.route("/settings", methods=["GET", "POST"])
def settings():
    state = get_security_settings()
    if request.method == "POST":
        if not _valid_csrf():
            return Response(_message("security_invalid_request"), status=403)
        enabled = request.form.get("auth_enabled") == "yes"
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password", "")
        error = None
        current_password = request.form.get("current_password", "")
        if state["auth_enabled"] and (len(current_password) > 1024 or not check_password_hash(state["password_hash"], current_password)):
            error = "security_current_password_invalid"
        elif enabled and (not username or len(username) > 80):
            error = "security_username_required"
        elif enabled and (password or not state["auth_enabled"]) and not 12 <= len(password) <= 1024:
            error = "security_password_length"
        elif enabled and password != request.form.get("password_confirm", ""):
            error = "security_password_mismatch"
        if error:
            flash(_message(error), "error")
            return render_template("security.html", page_kind="settings", security=state, **_page_context()), 400
        password_hash = (generate_password_hash(password) if password else state["password_hash"]) if enabled else ""
        revision = secrets.token_hex(32)
        with closing(_connection()) as db:
            db.execute("BEGIN IMMEDIATE")
            current_revision = db.execute("SELECT revision FROM panel_security WHERE id = 1").fetchone()[0]
            if current_revision != request.form.get("security_revision"):
                db.rollback()
                flash(_message("security_settings_changed"), "error")
                return redirect(url_for("security.settings"))
            db.execute("""UPDATE panel_security SET auth_enabled = ?, username = ?,
                password_hash = ?, revision = ? WHERE id = 1""",
                (int(enabled), username if enabled else state["username"], password_hash, revision))
            db.execute("DELETE FROM login_attempts")
            db.commit()
        g.pop("panel_security", None)
        _rotate_session(get_security_settings())
        flash(_message("security_settings_saved"), "success")
        return redirect(url_for("security.settings"))
    return render_template("security.html", page_kind="settings", security=state, **_page_context())
