"""Correlate responses and metadata logs without logging submitted content."""

import logging
import re
import sqlite3

import pytest

from ticket_backend import auth


def request_id(response):
    value = response.headers["X-Request-ID"]
    assert re.fullmatch(r"[0-9a-f]{32}", value)
    assert response.headers["Cache-Control"] == "no-store"
    return value


def application_messages(app, caplog):
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == app.logger.name
    ]


def test_each_request_gets_a_new_server_identifier(client):
    supplied = "1" * 32
    first = client.get("/auth/csrf", headers={"X-Request-ID": supplied})
    second = client.get("/auth/csrf", headers={"X-Request-ID": supplied})
    first_id, second_id = request_id(first), request_id(second)
    assert len({supplied, first_id, second_id}) == 3
    assert set(first.get_json()) == {"data"}
    assert set(first.get_json()["data"]) == {"csrf_token"}


@pytest.mark.parametrize(
    ("method", "path", "status", "code"),
    [
        ("GET", "/tickets", 401, "AUTH_REQUIRED"),
        ("POST", "/auth/login", 403, "CSRF_FAILED"),
        ("GET", "/does-not-exist", 404, "NOT_FOUND"),
        ("DELETE", "/auth/csrf", 405, "METHOD_NOT_ALLOWED"),
    ],
)
def test_authentication_and_routing_errors_keep_headers_and_json(
    client, assert_error, method, path, status, code
):
    response = client.open(path, method=method)
    body = assert_error(response, status, code)
    request_id(response)
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message"}
    if status == 405:
        assert "GET" in response.headers["Allow"]


@pytest.mark.parametrize(
    ("content_type", "body", "status", "code"),
    [
        ("application/json", '{"title":', 400, "INVALID_REQUEST"),
        ("text/plain", "simulated request", 415, "UNSUPPORTED_MEDIA_TYPE"),
        ("application/json", "x" * (64 * 1024 + 1), 413, "REQUEST_TOO_LARGE"),
    ],
    ids=["malformed-json", "media-type", "oversized-body"],
)
def test_body_errors_also_get_response_metadata(
    submitter_client, assert_error, content_type, body, status, code
):
    client, token = submitter_client
    response = client.post(
        "/tickets", data=body, content_type=content_type,
        headers={"X-CSRF-Token": token},
    )
    assert_error(response, status, code)
    request_id(response)


def test_completed_request_records_metadata_and_response_identifier(
    app, submitter_client, user_ids, caplog
):
    client, _ = submitter_client
    caplog.clear()
    with caplog.at_level(logging.INFO):
        response = client.get("/tickets?limit=1")
    assert response.status_code == 200
    identifier = request_id(response)
    messages = application_messages(app, caplog)
    assert len(messages) == 1
    completed = messages[0]
    assert completed.startswith(f"Request completed request_id={identifier} ")
    assert "method=GET path='/tickets' status=200" in completed
    assert f"user_id={user_ids['submitter_a']}" in completed
    duration = re.search(r"duration_ms=(\d+\.\d+)\b", completed)
    assert duration and float(duration.group(1)) >= 0
    assert "limit=1" not in completed


def test_authentication_lookup_failure_has_matching_error_and_completion_id(
    app, submitter_client, assert_error, monkeypatch, caplog
):
    client, _ = submitter_client

    def fail_lookup():
        raise sqlite3.OperationalError("simulated_authentication_lookup_failure")

    monkeypatch.setattr(auth, "get_db", fail_lookup)
    caplog.clear()
    with caplog.at_level(logging.INFO):
        response = client.get("/tickets")
    assert_error(response, 500, "INTERNAL_ERROR")
    identifier = request_id(response)
    records = [record for record in caplog.records if record.name == app.logger.name]
    failed = [record for record in records if record.getMessage().startswith("Request failed ")]
    completed = [record for record in records if record.getMessage().startswith("Request completed ")]
    assert len(failed) == len(completed) == 1
    assert failed[0].levelno == logging.ERROR
    assert failed[0].exc_info is not None
    assert f"request_id={identifier}" in failed[0].getMessage()
    assert f"request_id={identifier}" in completed[0].getMessage()
    assert "status=500" in completed[0].getMessage()
    assert "simulated_authentication_lookup_failure" not in response.get_data(as_text=True)


def test_untrusted_path_is_bounded_and_control_characters_are_escaped(
    app, client, caplog
):
    path = "/missing\nforged-line\t" + "x" * 400 + "hidden-path-tail"
    caplog.clear()
    with caplog.at_level(logging.INFO):
        response = client.get("/missing%0Aforged-line%09" + "x" * 400 + "hidden-path-tail")
    assert response.status_code == 404
    messages = application_messages(app, caplog)
    assert len(messages) == 1
    assert f"path={path[:256]!r}" in messages[0]
    assert "\n" not in messages[0] and "\t" not in messages[0]
    assert "hidden-path-tail" not in messages[0]


def test_status_commit_log_and_completion_share_response_identifier(
    app, submitter_client, authenticated_client, user_ids, caplog
):
    submitter, token = submitter_client
    created = submitter.post(
        "/tickets", json={"title": "simulated fault", "description": "simulated data"},
        headers={"X-CSRF-Token": token},
    )
    ticket_id = created.get_json()["data"]["id"]
    handler, handler_token = authenticated_client("handler")
    caplog.clear()
    with caplog.at_level(logging.INFO):
        response = handler.patch(
            f"/tickets/{ticket_id}/status", json={"status": "in_progress"},
            headers={"X-CSRF-Token": handler_token},
        )
    assert response.status_code == 200
    identifier = request_id(response)
    messages = application_messages(app, caplog)
    assert len(messages) == 2
    changed = [message for message in messages if message.startswith("Ticket status changed ")]
    completed = [message for message in messages if message.startswith("Request completed ")]
    assert len(changed) == len(completed) == 1
    assert all(f"request_id={identifier}" in message for message in messages)
    assert f"ticket_id={ticket_id} actor_id={user_ids['handler']}" in changed[0]
    assert "old_status=pending new_status=in_progress" in changed[0]


def test_request_content_tokens_cookies_and_query_values_are_not_logged(
    app, client, authenticated_client, caplog
):
    password = "test-password-for-local-fixtures"
    cookie_marker = "private-cookie-value-for-test"
    query_marker = "private-query-value-for-test"
    title = "private-title-content-for-test"
    description = "private-description-content-for-test"
    resolution = "private-resolution-content-for-test"
    client.set_cookie("ignored-cookie", cookie_marker)
    caplog.clear()
    with caplog.at_level(logging.INFO):
        initial_token = client.get("/auth/csrf").get_json()["data"]["csrf_token"]
        logged_in = client.post(
            "/auth/login", json={"username": "submitter_a", "password": password},
            headers={"X-CSRF-Token": initial_token},
        )
        assert logged_in.status_code == 200
        token = logged_in.get_json()["data"]["csrf_token"]
        session_cookie = client.get_cookie(app.config["SESSION_COOKIE_NAME"]).value
        created = client.post(
            "/tickets", json={"title": title, "description": description},
            headers={"X-CSRF-Token": token},
        )
        assert created.status_code == 201
        ticket_id = created.get_json()["data"]["id"]
        rejected = client.get("/tickets", query_string={"unexpected": query_marker})
        assert rejected.status_code == 400
        handler, handler_token = authenticated_client("handler")
        for payload in [
            {"status": "in_progress"},
            {"status": "resolved", "resolution": resolution},
        ]:
            response = handler.patch(
                f"/tickets/{ticket_id}/status", json=payload,
                headers={"X-CSRF-Token": handler_token},
            )
            assert response.status_code == 200
    messages = "\n".join(application_messages(app, caplog))
    assert messages
    for secret in (
        password, cookie_marker, query_marker, title, description, resolution,
        initial_token, token, handler_token, session_cookie,
    ):
        assert secret not in messages
