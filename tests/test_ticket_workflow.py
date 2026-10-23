"""Ticket visibility, status rules, rollback, and competing-handler regression tests."""

from concurrent.futures import ThreadPoolExecutor
import logging
from threading import Barrier

import pytest

from ticket_backend import tickets as tickets_module
from ticket_backend.db import get_db


TICKET_FIELDS = {
    "id", "title", "description", "creator_id", "status", "created_at",
    "resolution", "resolved_by",
}


def create_ticket(client_and_token, title="模拟故障"):
    client, token = client_and_token
    response = client.post(
        "/tickets",
        json={"title": title, "description": "个人项目模拟数据：接口请求失败。"},
        headers={"X-CSRF-Token": token},
    )
    assert response.status_code == 201
    return response.get_json()["data"]


def change_status(client_and_token, ticket_id, status, **fields):
    client, token = client_and_token
    return client.patch(
        f"/tickets/{ticket_id}/status",
        json={"status": status, **fields},
        headers={"X-CSRF-Token": token},
    )


def saved_ticket(app, ticket_id):
    with app.app_context():
        row = get_db().execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        return dict(row)


@pytest.fixture
def handler_client(authenticated_client):
    return authenticated_client("handler")


@pytest.fixture
def pending_ticket(submitter_client):
    return create_ticket(submitter_client)


@pytest.fixture
def visible_tickets(authenticated_client):
    clients = {
        username: authenticated_client(username)
        for username in ("submitter_a", "submitter_b", "handler")
    }
    tickets = []
    for username, status in [
        ("submitter_a", "pending"),
        ("submitter_a", "resolved"),
        ("submitter_a", "in_progress"),
        ("submitter_b", "pending"),
        ("submitter_b", "resolved"),
    ]:
        ticket = create_ticket(clients[username], f"{username} {status}")
        if status in {"in_progress", "resolved"}:
            response = change_status(clients["handler"], ticket["id"], "in_progress")
            assert response.status_code == 200
            ticket = response.get_json()["data"]
        if status == "resolved":
            response = change_status(
                clients["handler"], ticket["id"], "resolved", resolution="模拟排查后恢复服务。"
            )
            assert response.status_code == 200
            ticket = response.get_json()["data"]
        tickets.append((username, ticket))
    return clients, tickets


def test_complete_workflow_preserves_owner_and_records_handler(
    app, submitter_client, handler_client, pending_ticket, user_ids
):
    ticket_id = pending_ticket["id"]
    for status, fields in [
        ("in_progress", {}),
        ("resolved", {"resolution": "  模拟定位：超时配置错误，调整后复测通过。 \n"}),
    ]:
        response = change_status(handler_client, ticket_id, status, **fields)
        assert response.status_code == 200
        ticket = response.get_json()["data"]
        assert set(ticket) == TICKET_FIELDS
        assert ticket["status"] == status
        for field in ("id", "title", "description", "creator_id", "created_at"):
            assert ticket[field] == pending_ticket[field]
        if status == "resolved":
            assert ticket["resolution"] == fields["resolution"].strip()
            assert ticket["resolved_by"] == user_ids["handler"]
        else:
            assert ticket["resolution"] is None
            assert ticket["resolved_by"] is None
        assert saved_ticket(app, ticket_id) == ticket
        submitter, _ = submitter_client
        assert submitter.get(f"/tickets/{ticket_id}").get_json()["data"] == ticket


def test_lists_and_totals_apply_owner_and_status_filters(visible_tickets):
    clients, tickets = visible_tickets
    for username, (client, _) in clients.items():
        for status in (None, "pending", "in_progress", "resolved"):
            expected = [
                ticket
                for owner, ticket in reversed(tickets)
                if (username == "handler" or owner == username)
                and (status is None or ticket["status"] == status)
            ]
            response = client.get("/tickets", query_string={} if status is None else {"status": status})
            assert response.status_code == 200
            assert response.get_json()["data"] == {
                "items": expected, "total": len(expected), "limit": 20, "offset": 0,
            }
            assert all(set(ticket) == TICKET_FIELDS for ticket in expected)


def test_empty_list_has_explicit_pagination(submitter_client):
    client, _ = submitter_client
    assert client.get("/tickets").get_json()["data"] == {
        "items": [], "total": 0, "limit": 20, "offset": 0,
    }


def test_pagination_orders_by_id_and_retains_filtered_total(visible_tickets):
    clients, tickets = visible_tickets
    handler, _ = clients["handler"]
    all_tickets = [ticket for _, ticket in reversed(tickets)]
    pages = []
    for offset in (0, 2, 4):
        response = handler.get("/tickets", query_string={"limit": 2, "offset": offset})
        assert response.status_code == 200
        data = response.get_json()["data"]
        assert data["total"] == 5
        assert data["limit"] == 2
        assert data["offset"] == offset
        pages.extend(data["items"])
    assert pages == all_tickets

    submitter, _ = clients["submitter_a"]
    filtered = submitter.get(
        "/tickets", query_string={"status": "pending", "limit": 1, "offset": 1}
    ).get_json()["data"]
    assert filtered == {"items": [], "total": 1, "limit": 1, "offset": 1}
    last_offset = handler.get(
        "/tickets", query_string={"limit": 100, "offset": 2**63 - 1}
    )
    assert last_offset.status_code == 200
    assert last_offset.get_json()["data"] == {
        "items": [], "total": 5, "limit": 100, "offset": 2**63 - 1,
    }


@pytest.mark.parametrize(
    "query",
    [
        [("limit", "0")], [("limit", "101")], [("limit", "-1")],
        [("limit", "+1")], [("limit", "1.0")], [("limit", "1e1")],
        [("limit", " 1")], [("limit", "")], [("limit", "１")],
        [("limit", "1" * 20)], [("limit", "9" * 5000)],
        [("offset", "-1")], [("offset", "")], [("offset", "9223372036854775808")],
        [("offset", "０")], [("offset", "0.0")],
        [("status", "")], [("status", "unknown")], [("status", "PENDING")],
        [("status", "pending' OR 1=1 --")],
        [("limit", "1"), ("limit", "2")],
        [("offset", "0"), ("offset", "0")],
        [("status", "pending"), ("status", "pending")],
        [("creator_id", "1")], [("role", "handler")],
    ],
    ids=[
        "limit-zero", "limit-above-max", "limit-negative", "limit-plus-sign",
        "limit-decimal", "limit-exponent", "limit-space", "limit-empty",
        "limit-nonascii", "limit-too-many-digits", "limit-over-python-digit-limit",
        "offset-negative", "offset-empty", "offset-over-sqlite-limit",
        "offset-nonascii", "offset-decimal", "status-empty", "status-unknown",
        "status-uppercase", "status-sql-text", "limit-duplicate", "offset-duplicate",
        "status-duplicate", "creator-filter-forbidden", "role-filter-forbidden",
    ],
)
def test_invalid_list_queries_are_explicitly_rejected(submitter_client, assert_error, query):
    client, _ = submitter_client
    assert_error(client.get("/tickets", query_string=query), 400, "INVALID_REQUEST")


def test_new_endpoints_require_login(client, assert_error):
    assert_error(client.get("/tickets"), 401, "AUTH_REQUIRED")
    assert_error(
        client.patch("/tickets/1/status", json={"status": "in_progress"}),
        401, "AUTH_REQUIRED",
    )


@pytest.mark.parametrize("username", ["submitter_a", "submitter_b"])
def test_submitters_cannot_change_even_owned_ticket(
    app, pending_ticket, authenticated_client, assert_error, username
):
    account = authenticated_client(username)
    before = saved_ticket(app, pending_ticket["id"])
    response = change_status(account, pending_ticket["id"], "in_progress")
    assert_error(response, 403, "FORBIDDEN")
    assert saved_ticket(app, pending_ticket["id"]) == before


@pytest.mark.parametrize("csrf", [None, "wrong-token", "é"])
def test_status_change_rejects_missing_or_wrong_csrf(
    app, pending_ticket, handler_client, assert_error, csrf
):
    handler, _ = handler_client
    before = saved_ticket(app, pending_ticket["id"])
    response = handler.patch(
        f"/tickets/{pending_ticket['id']}/status",
        json={"status": "in_progress"},
        headers={} if csrf is None else {"X-CSRF-Token": csrf},
    )
    assert_error(response, 403, "CSRF_FAILED")
    assert saved_ticket(app, pending_ticket["id"]) == before


@pytest.mark.parametrize(
    ("source", "target"),
    [
        ("pending", "pending"), ("pending", "resolved"),
        ("in_progress", "pending"), ("in_progress", "in_progress"),
        ("resolved", "pending"), ("resolved", "in_progress"), ("resolved", "resolved"),
    ],
)
def test_all_forbidden_status_transitions_leave_every_field_unchanged(
    app, pending_ticket, handler_client, assert_error, source, target
):
    ticket_id = pending_ticket["id"]
    if source in {"in_progress", "resolved"}:
        assert change_status(handler_client, ticket_id, "in_progress").status_code == 200
    if source == "resolved":
        assert change_status(
            handler_client, ticket_id, "resolved", resolution="原始处理结果，禁止覆盖。"
        ).status_code == 200
    before = saved_ticket(app, ticket_id)
    fields = {"resolution": "试图覆盖处理结果。"} if target == "resolved" else {}
    response = change_status(handler_client, ticket_id, target, **fields)
    assert_error(response, 409, "INVALID_TRANSITION")
    assert saved_ticket(app, ticket_id) == before


@pytest.mark.parametrize(
    "payload",
    [
        {}, {"status": None}, {"status": 1}, {"status": []}, {"status": "unknown"},
        {"status": "resolved"}, {"status": "resolved", "resolution": ""},
        {"status": "resolved", "resolution": " \t\n"},
        {"status": "resolved", "resolution": "述" * 2001},
        {"status": "resolved", "resolution": None},
        {"status": "resolved", "resolution": 123},
        {"status": "resolved", "resolution": True},
        {"status": "resolved", "resolution": []},
        {"status": "resolved", "resolution": {}},
        {"status": "resolved", "resolution": "模拟\x00结果"},
        {"status": "resolved", "resolution": "模拟\udfff结果"},
        {"status": "in_progress", "resolution": "不能提前填写结果"},
        {"status": "in_progress", "resolved_by": 999},
        {"status": "in_progress", "creator_id": 999},
        {"status": "resolved", "resolution": "结果", "resolved_by": 999},
    ],
    ids=[
        "missing-status", "status-null", "status-number", "status-array", "unknown-status",
        "missing-resolution", "empty-resolution", "blank-resolution", "long-resolution",
        "null-resolution", "number-resolution", "boolean-resolution", "array-resolution",
        "object-resolution", "nul-resolution", "surrogate-resolution", "premature-resolution",
        "forged-handler", "forged-owner", "forged-resolved-handler",
    ],
)
def test_invalid_status_payloads_cannot_write(
    app, pending_ticket, handler_client, assert_error, payload
):
    ticket_id = pending_ticket["id"]
    assert change_status(handler_client, ticket_id, "in_progress").status_code == 200
    before = saved_ticket(app, ticket_id)
    handler, token = handler_client
    response = handler.patch(
        f"/tickets/{ticket_id}/status", json=payload, headers={"X-CSRF-Token": token}
    )
    assert_error(response, 400, "INVALID_REQUEST")
    assert saved_ticket(app, ticket_id) == before


@pytest.mark.parametrize("raw_json", ['{"status":', "null", "[]", '"text"'])
def test_status_change_rejects_malformed_or_non_object_json(
    app, pending_ticket, handler_client, assert_error, raw_json
):
    handler, token = handler_client
    before = saved_ticket(app, pending_ticket["id"])
    response = handler.patch(
        f"/tickets/{pending_ticket['id']}/status", data=raw_json,
        content_type="application/json", headers={"X-CSRF-Token": token},
    )
    assert_error(response, 400, "INVALID_REQUEST")
    assert saved_ticket(app, pending_ticket["id"]) == before


def test_status_change_requires_json_media_type(
    app, pending_ticket, handler_client, assert_error
):
    handler, token = handler_client
    before = saved_ticket(app, pending_ticket["id"])
    response = handler.patch(
        f"/tickets/{pending_ticket['id']}/status", data="status=in_progress",
        content_type="application/x-www-form-urlencoded", headers={"X-CSRF-Token": token},
    )
    assert_error(response, 415, "UNSUPPORTED_MEDIA_TYPE")
    assert saved_ticket(app, pending_ticket["id"]) == before


@pytest.mark.parametrize("resolution", ["一", "述" * 2000, "🔧" * 2000])
def test_resolution_inclusive_lengths_and_unicode_round_trip(
    app, pending_ticket, handler_client, resolution
):
    ticket_id = pending_ticket["id"]
    assert change_status(handler_client, ticket_id, "in_progress").status_code == 200
    response = change_status(handler_client, ticket_id, "resolved", resolution=resolution)
    assert response.status_code == 200
    assert response.get_json()["data"]["resolution"] == resolution
    assert saved_ticket(app, ticket_id)["resolution"] == resolution


@pytest.mark.parametrize(
    "ticket_id", ["0", "-1", "999999", str(2**63), "not-a-number", "9" * 5000],
    ids=["zero", "negative", "missing", "sqlite-overflow", "text", "over-python-digit-limit"],
)
def test_status_change_invalid_or_missing_id_is_not_found(handler_client, assert_error, ticket_id):
    response = change_status(handler_client, ticket_id, "in_progress")
    assert_error(response, 404, "NOT_FOUND")


@pytest.mark.parametrize("target_status", ["in_progress", "resolved"])
def test_simulated_database_update_failure_rolls_back_and_recovers(
    app, pending_ticket, handler_client, assert_error, caplog, target_status
):
    ticket_id = pending_ticket["id"]
    if target_status == "resolved":
        assert change_status(handler_client, ticket_id, "in_progress").status_code == 200
    before = saved_ticket(app, ticket_id)
    with app.app_context():
        database = get_db()
        database.execute(
            """
            CREATE TRIGGER simulated_update_failure
            AFTER UPDATE ON tickets
            BEGIN
                SELECT RAISE(ABORT, 'private_simulated_update_failure');
            END
            """
        )
        database.commit()
    fields = {"resolution": "模拟修复并复测。"} if target_status == "resolved" else {}
    with caplog.at_level(logging.ERROR):
        response = change_status(handler_client, ticket_id, target_status, **fields)
    assert_error(response, 500, "INTERNAL_ERROR")
    for internal_detail in (
        "private_simulated_update_failure", "Traceback", "UPDATE tickets", "sqlite3",
        app.config["DATABASE"],
    ):
        assert internal_detail not in response.get_data(as_text=True)
    assert any(record.levelno >= logging.ERROR for record in caplog.records)
    assert saved_ticket(app, ticket_id) == before

    with app.app_context():
        database = get_db()
        database.execute("DROP TRIGGER simulated_update_failure")
        database.commit()
    recovered = change_status(handler_client, ticket_id, target_status, **fields)
    assert recovered.status_code == 200
    assert saved_ticket(app, ticket_id) == recovered.get_json()["data"]
    assert saved_ticket(app, ticket_id)["status"] == target_status


def test_two_handlers_cannot_overwrite_each_others_resolution(
    app, pending_ticket, handler_client, authenticated_client, password_hash,
    assert_error, monkeypatch,
):
    """Force two independent test-client requests to read the same old status."""
    with app.app_context():
        database = get_db()
        cursor = database.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
            ("handler_second", password_hash, "handler"),
        )
        second_id = cursor.lastrowid
        first_id = database.execute("SELECT id FROM users WHERE username = 'handler'").fetchone()[0]
        database.commit()
    second_handler = authenticated_client("handler_second")
    ticket_id = pending_ticket["id"]
    assert change_status(handler_client, ticket_id, "in_progress").status_code == 200
    both_read = Barrier(2)
    original_get_db = tickets_module.get_db

    class CoordinatedCursor:
        def __init__(self, cursor):
            self.cursor = cursor

        def fetchone(self):
            row = self.cursor.fetchone()
            self.cursor.close()
            both_read.wait(timeout=10)
            return row

    class CoordinatedConnection:
        def __init__(self, connection):
            self.connection = connection

        def __enter__(self):
            self.connection.__enter__()
            return self

        def __exit__(self, *args):
            return self.connection.__exit__(*args)

        def execute(self, statement, parameters=()):
            cursor = self.connection.execute(statement, parameters)
            if statement == "SELECT status FROM tickets WHERE id = ?":
                return CoordinatedCursor(cursor)
            return cursor

    monkeypatch.setattr(tickets_module, "get_db", lambda: CoordinatedConnection(original_get_db()))
    attempts = [
        (handler_client, first_id, "第一位处理人的结果。"),
        (second_handler, second_id, "第二位处理人的结果。"),
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(change_status, account, ticket_id, "resolved", resolution=resolution)
            for account, _, resolution in attempts
        ]
        responses = [future.result(timeout=15) for future in futures]
    assert sorted(response.status_code for response in responses) == [200, 409]
    for (_, actor_id, resolution), response in zip(attempts, responses):
        if response.status_code == 200:
            result = response.get_json()["data"]
            assert result["status"] == "resolved"
            assert result["resolved_by"] == actor_id
            assert result["resolution"] == resolution
            assert saved_ticket(app, ticket_id) == result
        else:
            assert_error(response, 409, "INVALID_TRANSITION")
