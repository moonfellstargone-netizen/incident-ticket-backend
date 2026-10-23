"""First-step ticket creation, access control, validation, and failure handling."""

from datetime import datetime
import logging

import pytest

from ticket_backend.db import get_db, init_db


VALID_TICKET = {"title": "接口请求失败", "description": "模拟故障：请求返回错误，需要人工排查。"}
TICKET_FIELDS = {
    "id", "title", "description", "creator_id", "status", "created_at",
    "resolution", "resolved_by",
}


def ticket_count(app):
    with app.app_context():
        return get_db().execute("SELECT COUNT(*) FROM tickets").fetchone()[0]


def test_create_persists_trimmed_ticket_and_get_returns_same_data(
    app, submitter_client, user_ids
):
    client, token = submitter_client
    response = client.post(
        "/tickets",
        json={"title": "  接口请求失败  ", "description": "\n  模拟请求失败，待排查。 \t"},
        headers={"X-CSRF-Token": token},
    )

    assert response.status_code == 201
    ticket = response.get_json()["data"]
    assert set(ticket) == TICKET_FIELDS
    assert type(ticket["id"]) is int
    assert ticket["id"] > 0
    assert ticket["title"] == "接口请求失败"
    assert ticket["description"] == "模拟请求失败，待排查。"
    assert ticket["creator_id"] == user_ids["submitter_a"]
    assert ticket["status"] == "pending"
    assert ticket["resolution"] is None
    assert ticket["resolved_by"] is None
    assert isinstance(ticket["created_at"], str)
    datetime.fromisoformat(ticket["created_at"].replace("Z", "+00:00"))

    retrieved = client.get(f"/tickets/{ticket['id']}")
    assert retrieved.status_code == 200
    assert retrieved.get_json()["data"] == ticket
    with app.app_context():
        row = get_db().execute("SELECT * FROM tickets WHERE id = ?", (ticket["id"],)).fetchone()
        assert row["title"] == ticket["title"]
        assert row["description"] == ticket["description"]
        assert row["creator_id"] == user_ids["submitter_a"]
        assert row["status"] == "pending"
    assert ticket_count(app) == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("title", ""),
        ("title", " \t\n "),
        ("title", "题" * 101),
        ("title", None),
        ("title", 123),
        ("title", True),
        ("title", []),
        ("title", {}),
        ("title", "接口\x00故障"),
        ("title", "接口\ud800故障"),
        ("description", ""),
        ("description", " \t\n "),
        ("description", "述" * 2001),
        ("description", None),
        ("description", 123),
        ("description", True),
        ("description", []),
        ("description", {}),
        ("description", "模拟\x00故障"),
        ("description", "模拟\udfff故障"),
    ],
    ids=[
        "title-empty", "title-whitespace", "title-too-long", "title-null",
        "title-integer", "title-boolean", "title-array", "title-object",
        "title-nul", "title-surrogate",
        "description-empty", "description-whitespace", "description-too-long", "description-null",
        "description-integer", "description-boolean", "description-array", "description-object",
        "description-nul", "description-surrogate",
    ],
)
def test_invalid_fields_are_rejected_without_writes(
    app, submitter_client, assert_error, field, value
):
    client, token = submitter_client
    payload = {**VALID_TICKET, field: value}
    response = client.post("/tickets", json=payload, headers={"X-CSRF-Token": token})
    assert_error(response, 400, "INVALID_REQUEST")
    assert ticket_count(app) == 0


@pytest.mark.parametrize(("title_length", "description_length"), [(1, 1), (100, 2000)])
def test_inclusive_length_limits_accept_valid_strings(
    submitter_client, title_length, description_length
):
    client, token = submitter_client
    response = client.post(
        "/tickets",
        json={"title": "题" * title_length, "description": "述" * description_length},
        headers={"X-CSRF-Token": token},
    )
    assert response.status_code == 201
    ticket = response.get_json()["data"]
    assert len(ticket["title"]) == title_length
    assert len(ticket["description"]) == description_length


def test_maximum_length_emoji_survive_json_escaping_and_database_round_trip(submitter_client):
    client, token = submitter_client
    payload = {"title": "\U0001f527" * 100, "description": "\U0001f4dd" * 2000}
    created = client.post("/tickets", json=payload, headers={"X-CSRF-Token": token})
    assert created.status_code == 201
    ticket = created.get_json()["data"]
    fetched = client.get(f"/tickets/{ticket['id']}")
    assert fetched.status_code == 200
    assert fetched.get_json()["data"]["title"] == payload["title"]
    assert fetched.get_json()["data"]["description"] == payload["description"]


@pytest.mark.parametrize("field", ["title", "description"])
def test_required_fields_cannot_be_omitted(app, submitter_client, assert_error, field):
    client, token = submitter_client
    payload = dict(VALID_TICKET)
    del payload[field]
    response = client.post("/tickets", json=payload, headers={"X-CSRF-Token": token})
    assert_error(response, 400, "INVALID_REQUEST")
    assert ticket_count(app) == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [("creator_id", 999), ("status", "resolved"), ("role", "handler"), ("unexpected", "x")],
)
def test_extra_fields_cannot_override_server_owned_values(
    app, submitter_client, assert_error, field, value
):
    client, token = submitter_client
    response = client.post(
        "/tickets",
        json={**VALID_TICKET, field: value},
        headers={"X-CSRF-Token": token},
    )
    assert_error(response, 400, "INVALID_REQUEST")
    assert ticket_count(app) == 0


@pytest.mark.parametrize("raw_json", ['{"title":', "null", "[]", '"text"'])
def test_bad_json_and_non_object_bodies_are_rejected(
    app, submitter_client, assert_error, raw_json
):
    client, token = submitter_client
    response = client.post(
        "/tickets",
        data=raw_json,
        content_type="application/json",
        headers={"X-CSRF-Token": token},
    )
    assert_error(response, 400, "INVALID_REQUEST")
    assert ticket_count(app) == 0


def test_non_json_request_has_explicit_media_type_error(app, submitter_client, assert_error):
    client, token = submitter_client
    response = client.post(
        "/tickets",
        data="title=example&description=example",
        content_type="application/x-www-form-urlencoded",
        headers={"X-CSRF-Token": token},
    )
    assert_error(response, 415, "UNSUPPORTED_MEDIA_TYPE")
    assert ticket_count(app) == 0


@pytest.mark.parametrize("csrf_token", [None, "wrong-token", "\u00e9"])
def test_creation_enforces_csrf_even_when_authenticated(
    app, submitter_client, assert_error, csrf_token
):
    client, _ = submitter_client
    headers = {} if csrf_token is None else {"X-CSRF-Token": csrf_token}
    response = client.post("/tickets", json=VALID_TICKET, headers=headers)
    assert_error(response, 403, "CSRF_FAILED")
    assert ticket_count(app) == 0


def test_unauthenticated_requests_cannot_create_or_read(app, client, assert_error):
    assert_error(client.get("/tickets/1"), 401, "AUTH_REQUIRED")
    assert_error(client.post("/tickets", json=VALID_TICKET), 401, "AUTH_REQUIRED")
    assert ticket_count(app) == 0


@pytest.mark.parametrize(
    "ticket_id", [0, -1, 2**63 - 1, 2**63, 10**100],
    ids=["zero", "negative", "maximum-sqlite-integer", "above-sqlite-limit", "huge-integer"],
)
def test_missing_or_out_of_sqlite_range_ids_return_404(submitter_client, assert_error, ticket_id):
    client, _ = submitter_client
    assert_error(client.get(f"/tickets/{ticket_id}"), 404, "NOT_FOUND")


def test_ticket_path_beyond_python_integer_digit_limit_returns_404(submitter_client, assert_error):
    client, _ = submitter_client
    response = client.get("/tickets/" + "9" * 5000)
    assert_error(response, 404, "NOT_FOUND")


def test_submitters_cannot_read_each_others_tickets(
    app, authenticated_client, user_ids, assert_error
):
    client_a, token_a = authenticated_client("submitter_a")
    client_b, token_b = authenticated_client("submitter_b")
    tickets = []
    for client, token, username in [
        (client_a, token_a, "submitter_a"),
        (client_b, token_b, "submitter_b"),
    ]:
        created = client.post("/tickets", json=VALID_TICKET, headers={"X-CSRF-Token": token})
        assert created.status_code == 201
        ticket = created.get_json()["data"]
        assert ticket["creator_id"] == user_ids[username]
        assert client.get(f"/tickets/{ticket['id']}").get_json()["data"] == ticket
        tickets.append(ticket)

    hidden_from_a = client_a.get(f"/tickets/{tickets[1]['id']}")
    hidden_from_b = client_b.get(f"/tickets/{tickets[0]['id']}")
    missing = client_a.get("/tickets/999999")
    expected = assert_error(missing, 404, "NOT_FOUND")
    assert assert_error(hidden_from_a, 404, "NOT_FOUND") == expected
    assert assert_error(hidden_from_b, 404, "NOT_FOUND") == expected
    assert ticket_count(app) == 2


def test_handler_can_read_but_cannot_create(app, submitter_client, authenticated_client, assert_error):
    submitter, token = submitter_client
    created = submitter.post("/tickets", json=VALID_TICKET, headers={"X-CSRF-Token": token})
    assert created.status_code == 201
    ticket = created.get_json()["data"]

    handler, handler_token = authenticated_client("handler")
    read = handler.get(f"/tickets/{ticket['id']}")
    assert read.status_code == 200
    assert read.get_json()["data"] == ticket
    forbidden = handler.post(
        "/tickets", json=VALID_TICKET, headers={"X-CSRF-Token": handler_token}
    )
    assert_error(forbidden, 403, "FORBIDDEN")
    assert ticket_count(app) == 1


def test_sql_like_text_round_trips_as_plain_data(app, submitter_client):
    client, token = submitter_client
    payload = {
        "title": "x'); DROP TABLE tickets; --",
        "description": "模拟输入包含 ' OR 1=1 --，应按普通文本保存。",
    }
    created = client.post("/tickets", json=payload, headers={"X-CSRF-Token": token})
    assert created.status_code == 201
    ticket = created.get_json()["data"]
    fetched = client.get(f"/tickets/{ticket['id']}")
    assert fetched.status_code == 200
    assert fetched.get_json()["data"]["title"] == payload["title"]
    assert fetched.get_json()["data"]["description"] == payload["description"]
    assert ticket_count(app) == 1


def test_sqlite_write_failure_is_logged_hidden_and_leaves_no_ticket(
    app, submitter_client, assert_error, caplog
):
    with app.app_context():
        database = get_db()
        database.execute(
            """
            CREATE TRIGGER simulate_ticket_write_failure
            BEFORE INSERT ON tickets
            BEGIN
                SELECT RAISE(ABORT, 'private_simulated_sqlite_failure');
            END
            """
        )
        database.commit()

    client, token = submitter_client
    with caplog.at_level(logging.ERROR):
        response = client.post("/tickets", json=VALID_TICKET, headers={"X-CSRF-Token": token})
    assert_error(response, 500, "INTERNAL_ERROR")
    body_text = response.get_data(as_text=True)
    for internal_detail in [
        "private_simulated_sqlite_failure",
        "Traceback",
        "INSERT INTO",
        "sqlite3",
        app.config["DATABASE"],
    ]:
        assert internal_detail not in body_text
    assert any(record.levelno >= logging.ERROR for record in caplog.records)
    assert ticket_count(app) == 0

    # After removing the simulated fault, the same operation must succeed.
    with app.app_context():
        database = get_db()
        database.execute("DROP TRIGGER simulate_ticket_write_failure")
        database.commit()
    recovered = client.post("/tickets", json=VALID_TICKET, headers={"X-CSRF-Token": token})
    assert recovered.status_code == 201
    assert ticket_count(app) == 1


def test_initializing_existing_database_does_not_delete_records(app, submitter_client):
    client, token = submitter_client
    created = client.post("/tickets", json=VALID_TICKET, headers={"X-CSRF-Token": token})
    assert created.status_code == 201
    ticket = created.get_json()["data"]
    with app.app_context():
        init_db()
        assert get_db().execute("SELECT COUNT(*) FROM users").fetchone()[0] == 3
    assert ticket_count(app) == 1
    assert client.get(f"/tickets/{ticket['id']}").get_json()["data"] == ticket
