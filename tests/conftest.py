"""Isolated application and accounts used by the first-step API tests."""

import pytest
from werkzeug.security import generate_password_hash

from ticket_backend import create_app
from ticket_backend.db import get_db, init_db


TEST_PASSWORD = "test-password-for-local-fixtures"


@pytest.fixture(scope="session")
def password_hash():
    # Keep the real password hashing behavior without repeating account setup work.
    return generate_password_hash(TEST_PASSWORD)


@pytest.fixture
def app(tmp_path, password_hash):
    application = create_app(
        {
            "TESTING": True,
            "DATABASE": str(tmp_path / "tickets.sqlite3"),
            "SECRET_KEY": "test-only-secret-key-with-at-least-32-characters",
        }
    )
    with application.app_context():
        init_db()
        database = get_db()
        database.executemany(
            "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
            [
                ("submitter_a", password_hash, "submitter"),
                ("submitter_b", password_hash, "submitter"),
                ("handler", password_hash, "handler"),
            ],
        )
        database.commit()
    return application


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def user_ids(app):
    with app.app_context():
        rows = get_db().execute("SELECT id, username FROM users").fetchall()
        return {row["username"]: row["id"] for row in rows}


@pytest.fixture
def login_user():
    def login(client, username="submitter_a", password=TEST_PASSWORD):
        csrf_response = client.get("/auth/csrf")
        assert csrf_response.status_code == 200
        csrf_token = csrf_response.get_json()["data"]["csrf_token"]
        return client.post(
            "/auth/login",
            json={"username": username, "password": password},
            headers={"X-CSRF-Token": csrf_token},
        )

    return login


@pytest.fixture
def authenticated_client(app, login_user):
    def make_client(username="submitter_a"):
        test_client = app.test_client()
        response = login_user(test_client, username)
        assert response.status_code == 200
        return test_client, response.get_json()["data"]["csrf_token"]

    return make_client


@pytest.fixture
def submitter_client(authenticated_client):
    return authenticated_client("submitter_a")


@pytest.fixture
def assert_error():
    def check(response, status_code, code):
        assert response.status_code == status_code
        assert response.is_json
        body = response.get_json()
        assert "data" not in body
        assert body["error"]["code"] == code
        assert isinstance(body["error"]["message"], str)
        assert body["error"]["message"]
        return body

    return check
