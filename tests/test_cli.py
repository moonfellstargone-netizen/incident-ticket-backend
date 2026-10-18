"""Exercise the same command entry points used for local setup."""

from pathlib import Path

import pytest
from werkzeug.security import check_password_hash

from ticket_backend import create_app
from ticket_backend.db import get_db


FIRST_PASSWORD = "cli-fixture-password-2026"
SECOND_PASSWORD = "different-cli-fixture-password-2026"


@pytest.fixture
def uninitialized_cli_app(tmp_path):
    return create_app(
        {
            "TESTING": True,
            "DATABASE": str(tmp_path / "cli.sqlite3"),
            "SECRET_KEY": "cli-test-only-secret-key-with-at-least-32-characters",
        }
    )


def invoke_successfully(runner, command, password=None):
    arguments = {"args": [command]}
    if password is not None:
        # Exercise both hidden prompts instead of passing a password option.
        arguments["input"] = f"{password}\n{password}\n"
    result = runner.invoke(**arguments)
    assert result.exit_code == 0, result.output
    assert result.exception is None
    if password is not None:
        assert password not in result.output
    return result


def database_snapshot(application):
    with application.app_context():
        database = get_db()
        users = [
            tuple(row)
            for row in database.execute(
                "SELECT id, username, password_hash, role FROM users ORDER BY id"
            ).fetchall()
        ]
        tickets = [
            tuple(row)
            for row in database.execute("SELECT * FROM tickets ORDER BY id").fetchall()
        ]
        return users, tickets


def test_real_init_command_creates_tables_from_an_uninitialized_database(uninitialized_cli_app):
    application = uninitialized_cli_app
    assert not Path(application.config["DATABASE"]).exists()
    result = invoke_successfully(application.test_cli_runner(), "init-db")
    assert "Database initialized" in result.output

    with application.app_context():
        tables = {
            row["name"]
            for row in get_db().execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert {"users", "tickets"}.issubset(tables)
    assert database_snapshot(application) == ([], [])


def test_seed_command_prompts_and_creates_three_accounts_with_real_password_hashes(
    uninitialized_cli_app,
):
    application = uninitialized_cli_app
    runner = application.test_cli_runner()
    invoke_successfully(runner, "init-db")
    result = invoke_successfully(runner, "seed-demo", FIRST_PASSWORD)
    assert "Created 3 demo account(s)" in result.output

    users, tickets = database_snapshot(application)
    assert {(username, role) for _, username, _, role in users} == {
        ("submitter_a", "submitter"),
        ("submitter_b", "submitter"),
        ("handler", "handler"),
    }
    for _, _, password_hash, _ in users:
        assert password_hash != FIRST_PASSWORD
        assert password_hash not in result.output
        assert check_password_hash(password_hash, FIRST_PASSWORD)
    assert tickets == []


def test_repeated_setup_preserves_tickets_passwords_and_existing_roles(uninitialized_cli_app):
    application = uninitialized_cli_app
    runner = application.test_cli_runner()
    invoke_successfully(runner, "init-db")
    invoke_successfully(runner, "seed-demo", FIRST_PASSWORD)
    with application.app_context():
        database = get_db()
        database.execute("UPDATE users SET role = ? WHERE username = ?", ("handler", "submitter_a"))
        creator = database.execute(
            "SELECT id FROM users WHERE username = ?", ("submitter_b",)
        ).fetchone()
        database.execute(
            "INSERT INTO tickets (title, description, creator_id) VALUES (?, ?, ?)",
            ("Preserve this ticket", "Simulated fixture, keep after setup.", creator["id"]),
        )
        database.commit()
    before = database_snapshot(application)

    invoke_successfully(runner, "init-db")
    assert database_snapshot(application) == before

    second_seed = invoke_successfully(runner, "seed-demo", SECOND_PASSWORD)
    assert "Created 0 demo account(s)" in second_seed.output
    assert FIRST_PASSWORD not in second_seed.output
    assert database_snapshot(application) == before
    users, tickets = before
    assert len(tickets) == 1
    assert next(role for _, username, _, role in users if username == "submitter_a") == "handler"
    for _, _, password_hash, _ in users:
        assert check_password_hash(password_hash, FIRST_PASSWORD)
        assert not check_password_hash(password_hash, SECOND_PASSWORD)
