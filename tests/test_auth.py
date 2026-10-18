"""Authentication, session boundaries, and real CSRF enforcement."""

import pytest

from ticket_backend.db import get_db


def test_login_identifies_user_and_rotates_csrf_token(client, user_ids, assert_error):
    before = client.get("/auth/csrf")
    assert before.status_code == 200
    previous_token = before.get_json()["data"]["csrf_token"]
    assert isinstance(previous_token, str)
    assert previous_token

    response = client.post(
        "/auth/login",
        json={"username": "submitter_a", "password": "test-password-for-local-fixtures"},
        headers={"X-CSRF-Token": previous_token},
    )
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["user"] == {
        "id": user_ids["submitter_a"],
        "username": "submitter_a",
        "role": "submitter",
    }
    new_token = data["csrf_token"]
    assert isinstance(new_token, str)
    assert new_token
    assert new_token != previous_token

    rejected = client.post(
        "/tickets",
        json={"title": "Stale token", "description": "This must not be created."},
        headers={"X-CSRF-Token": previous_token},
    )
    assert_error(rejected, 403, "CSRF_FAILED")

    accepted = client.post(
        "/tickets",
        json={"title": "New token", "description": "The new token must work."},
        headers={"X-CSRF-Token": new_token},
    )
    assert accepted.status_code == 201


@pytest.mark.parametrize("token", [None, "wrong-token", "\u00e9"])
def test_login_rejects_missing_invalid_or_non_ascii_csrf(client, assert_error, token):
    client.get("/auth/csrf")
    headers = {} if token is None else {"X-CSRF-Token": token}
    response = client.post(
        "/auth/login",
        json={"username": "submitter_a", "password": "test-password-for-local-fixtures"},
        headers=headers,
    )
    assert_error(response, 403, "CSRF_FAILED")
    assert_error(client.get("/tickets/1"), 401, "AUTH_REQUIRED")


def test_wrong_password_and_unknown_user_have_same_error(client, login_user, assert_error):
    wrong_password = login_user(client, "submitter_a", "incorrect-password")
    unknown_user = login_user(client, "unknown-user", "incorrect-password")
    injection_username = login_user(client, "' OR 1=1 --", "incorrect-password")

    expected = assert_error(wrong_password, 401, "INVALID_CREDENTIALS")
    assert assert_error(unknown_user, 401, "INVALID_CREDENTIALS") == expected
    assert assert_error(injection_username, 401, "INVALID_CREDENTIALS") == expected
    assert_error(client.get("/tickets/1"), 401, "AUTH_REQUIRED")


@pytest.mark.parametrize("field", ["username", "password"])
@pytest.mark.parametrize("invalid_text", ["bad\x00value", "bad\ud800value"], ids=["nul", "surrogate"])
def test_login_rejects_invalid_unicode_or_nul_without_authenticating(
    app, client, assert_error, field, invalid_text
):
    token = client.get("/auth/csrf").get_json()["data"]["csrf_token"]
    payload = {"username": "submitter_a", "password": "test-password-for-local-fixtures"}
    payload[field] = invalid_text
    response = client.post(
        "/auth/login", json=payload, headers={"X-CSRF-Token": token}
    )
    assert_error(response, 400, "INVALID_REQUEST")
    assert_error(client.get("/tickets/1"), 401, "AUTH_REQUIRED")
    with app.app_context():
        assert get_db().execute("SELECT COUNT(*) FROM users").fetchone()[0] == 3
        assert get_db().execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 0


@pytest.mark.parametrize("endpoint", ["/auth/login", "/tickets"], ids=["login", "tickets"])
def test_deeply_nested_json_with_valid_csrf_is_rejected_without_writes(
    app, client, login_user, assert_error, endpoint
):
    with app.app_context():
        users_before = [
            tuple(row)
            for row in get_db().execute(
                "SELECT id, username, password_hash, role FROM users ORDER BY id"
            ).fetchall()
        ]

    if endpoint == "/tickets":
        logged_in = login_user(client)
        assert logged_in.status_code == 200
        token = logged_in.get_json()["data"]["csrf_token"]
        prefix, suffix = '{"title":', ',"description":"simulated fault"}'
    else:
        token = client.get("/auth/csrf").get_json()["data"]["csrf_token"]
        prefix, suffix = '{"username":', ',"password":"test-password"}'

    # Build the raw request without making the test client's JSON encoder recurse.
    raw_json = prefix + "[" * 2000 + "0" + "]" * 2000 + suffix
    response = client.post(
        endpoint,
        data=raw_json,
        content_type="application/json",
        headers={"X-CSRF-Token": token},
    )
    assert_error(response, 400, "INVALID_REQUEST")
    with app.app_context():
        users_after = [
            tuple(row)
            for row in get_db().execute(
                "SELECT id, username, password_hash, role FROM users ORDER BY id"
            ).fetchall()
        ]
        assert users_after == users_before
        assert get_db().execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 0


def test_logout_requires_csrf_and_clears_authenticated_session(submitter_client, assert_error):
    client, csrf_token = submitter_client
    assert_error(client.post("/auth/logout"), 403, "CSRF_FAILED")

    response = client.post("/auth/logout", headers={"X-CSRF-Token": csrf_token})
    assert response.status_code == 200
    assert response.get_json()["data"]["logged_out"] is True
    assert_error(client.get("/tickets/1"), 401, "AUTH_REQUIRED")
    assert_error(
        client.post(
            "/tickets",
            json={"title": "After logout", "description": "Must be rejected."},
            headers={"X-CSRF-Token": csrf_token},
        ),
        401,
        "AUTH_REQUIRED",
    )


def test_logout_requires_login(client, assert_error):
    csrf_token = client.get("/auth/csrf").get_json()["data"]["csrf_token"]
    response = client.post("/auth/logout", headers={"X-CSRF-Token": csrf_token})
    assert_error(response, 401, "AUTH_REQUIRED")
