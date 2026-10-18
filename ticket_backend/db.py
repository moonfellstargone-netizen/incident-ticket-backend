"""SQLite connections and non-destructive setup commands."""

import sqlite3
from pathlib import Path

import click
from flask import current_app, g
from flask.cli import with_appcontext
from werkzeug.security import generate_password_hash

from .validation import is_utf8_text

DEMO_USERS = (
    ("submitter_a", "submitter"),
    ("submitter_b", "submitter"),
    ("handler", "handler"),
)


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(current_app.config["DATABASE"], timeout=5)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(error=None):
    connection = g.pop("db", None)
    if connection is not None:
        connection.close()


def init_db():
    Path(current_app.config["DATABASE"]).parent.mkdir(parents=True, exist_ok=True)
    with current_app.open_resource("schema.sql") as schema:
        get_db().executescript(schema.read().decode("utf-8"))


def seed_demo_users(password):
    if not is_utf8_text(password) or not password.strip() or not 8 <= len(password) <= 256:
        raise ValueError("Demo password must contain 8 to 256 characters.")
    connection = get_db()
    created = 0
    with connection:
        for username, role in DEMO_USERS:
            cursor = connection.execute(
                "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?) "
                "ON CONFLICT(username) DO NOTHING",
                (username, generate_password_hash(password), role),
            )
            created += cursor.rowcount
    return created


@click.command("init-db")
@with_appcontext
def init_db_command():
    """Create missing tables without deleting existing data."""
    init_db()
    click.echo("Database initialized; existing data preserved.")


@click.command("seed-demo")
@click.password_option(help="Set a password for the three local demo accounts.")
@with_appcontext
def seed_demo_command(password):
    """Create local demo accounts; do not reset existing accounts."""
    try:
        created = seed_demo_users(password)
    except ValueError as error:
        raise click.ClickException(str(error)) from error
    click.echo(f"Created {created} demo account(s); existing accounts unchanged.")


def init_app(app):
    app.teardown_appcontext(close_db)
    app.cli.add_command(init_db_command)
    app.cli.add_command(seed_demo_command)
