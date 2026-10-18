"""Guard against accidentally running the test suite on development data."""

from pathlib import Path

import pytest

import ticket_backend
from ticket_backend import create_app


TEST_SECRET = "test-only-secret-key-with-at-least-32-characters"


def test_testing_requires_database_in_explicit_configuration(tmp_path, monkeypatch):
    environment_database = tmp_path / "from-environment.sqlite3"
    monkeypatch.setenv("TICKET_DATABASE", str(environment_database))
    with pytest.raises(ValueError, match="DATABASE"):
        create_app({"TESTING": True, "SECRET_KEY": TEST_SECRET})
    assert not environment_database.exists()


def test_testing_refuses_default_development_database():
    default_database = Path(ticket_backend.__file__).resolve().parent.parent / "instance" / "tickets.sqlite3"
    with pytest.raises(ValueError, match="development database"):
        create_app(
            {
                "TESTING": True,
                "DATABASE": str(default_database),
                "SECRET_KEY": TEST_SECRET,
            }
        )


@pytest.mark.parametrize("secret", [None, "", "x" * 31, 12345])
def test_secret_key_must_be_a_string_or_bytes_with_at_least_32_characters(tmp_path, secret):
    database = tmp_path / "invalid-secret.sqlite3"
    with pytest.raises(ValueError, match="32"):
        create_app({"TESTING": True, "DATABASE": str(database), "SECRET_KEY": secret})
    assert not database.exists()


def test_factory_does_not_create_a_database_before_explicit_initialization(tmp_path):
    database = tmp_path / "not-initialized.sqlite3"
    application = create_app(
        {"TESTING": True, "DATABASE": str(database), "SECRET_KEY": TEST_SECRET}
    )
    assert application.config["DATABASE"] == str(database.resolve())
    assert not database.exists()
