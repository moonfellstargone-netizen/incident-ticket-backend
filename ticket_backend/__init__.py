"""Application factory for the first ticket workflow."""

import logging
import os
from datetime import timedelta
from pathlib import Path

from flask import Flask


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    app.logger.setLevel(logging.INFO)
    default_database = Path(app.instance_path) / "tickets.sqlite3"
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("TICKET_SECRET_KEY"),
        DATABASE=os.environ.get("TICKET_DATABASE", str(default_database)),
        MAX_CONTENT_LENGTH=64 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=False,  # This milestone runs on local HTTP only.
        PERMANENT_SESSION_LIFETIME=timedelta(minutes=30),
    )
    if test_config is not None:
        app.config.update(test_config)

    if app.config["TESTING"]:
        if test_config is None or "DATABASE" not in test_config:
            raise ValueError("Tests must provide an explicit temporary DATABASE path.")
        if Path(app.config["DATABASE"]).resolve() == default_database.resolve():
            raise ValueError("Tests must not use the default development database.")

    secret = app.config.get("SECRET_KEY")
    if not isinstance(secret, (str, bytes)) or len(secret) < 32:
        raise ValueError("Set TICKET_SECRET_KEY to a random secret of at least 32 characters.")
    database = app.config["DATABASE"]
    if not database or str(database) == ":memory:":
        raise ValueError("DATABASE must point to a SQLite file.")
    app.config["DATABASE"] = str(Path(database).expanduser().resolve())

    from . import auth, db, errors, tickets

    db.init_app(app)
    errors.init_app(app)
    app.register_blueprint(auth.bp)
    app.register_blueprint(tickets.bp)
    return app
