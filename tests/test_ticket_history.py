"""Visible, server-authored history must commit with the ticket state."""

from datetime import datetime
import logging

import pytest

from ticket_backend.db import get_db, init_db


HISTORY_FIELDS = {
    "id", "ticket_id", "actor_id", "from_status", "to_status", "request_id", "created_at",
}


@pytest.fixture
def history_ticket(submitter_client):
    client, token = submitter_client
    response = client.post(
        "/tickets",
        json={"title": "模拟接口超时", "description": "个人项目的模拟故障，仅用于验证。"},
        headers={"X-CSRF-Token": token},
    )
    assert response.status_code == 201
    return response.get_json()["data"]


def stored_history(app, ticket_id):
    with app.app_context():
        return [
            dict(row)
            for row in get_db().execute(
                "SELECT * FROM ticket_history WHERE ticket_id = ? ORDER BY id", (ticket_id,),
            ).fetchall()
        ]


def test_new_ticket_has_no_status_history(history_ticket, submitter_client, authenticated_client):
    owner, _ = submitter_client
    handler, _ = authenticated_client("handler")
    for client in (owner, handler):
        response = client.get(f"/tickets/{history_ticket['id']}/history")
        assert response.status_code == 200
        assert response.get_json() == {"data": {"items": [], "total": 0}}


def test_full_flow_records_actor_order_and_response_request_ids(
    app, history_ticket, submitter_client, authenticated_client, user_ids,
):
    owner, _ = submitter_client
    handler, token = authenticated_client("handler")
    ticket_id = history_ticket["id"]
    request_ids = []
    for payload in (
        {"status": "in_progress"},
        {"status": "resolved", "resolution": "模拟修正超时配置并验证。"},
    ):
        response = handler.patch(
            f"/tickets/{ticket_id}/status", json=payload, headers={"X-CSRF-Token": token},
        )
        assert response.status_code == 200
        request_ids.append(response.headers["X-Request-ID"])

    histories = []
    for client in (owner, handler):
        response = client.get(f"/tickets/{ticket_id}/history")
        assert response.status_code == 200
        body = response.get_json()["data"]
        assert set(body) == {"items", "total"}
        assert body["total"] == 2
        histories.append(body["items"])
    assert histories[0] == histories[1] == stored_history(app, ticket_id)
    rows = histories[0]
    assert [row["id"] for row in rows] == sorted({row["id"] for row in rows})
    assert [(row["from_status"], row["to_status"]) for row in rows] == [
        ("pending", "in_progress"), ("in_progress", "resolved"),
    ]
    for row, request_id in zip(rows, request_ids):
        assert set(row) == HISTORY_FIELDS
        assert row["ticket_id"] == ticket_id
        assert row["actor_id"] == user_ids["handler"]
        assert row["request_id"] == request_id
        assert row["created_at"].endswith("Z")
        assert datetime.fromisoformat(row["created_at"].replace("Z", "+00:00")).utcoffset().total_seconds() == 0


def test_history_uses_ticket_visibility_and_never_leaks_other_ticket_events(
    history_ticket, submitter_client, authenticated_client, client, assert_error,
):
    owner, _ = submitter_client
    other, other_token = authenticated_client("submitter_b")
    handler, token = authenticated_client("handler")
    ticket_id = history_ticket["id"]
    updated = handler.patch(
        f"/tickets/{ticket_id}/status", json={"status": "in_progress"},
        headers={"X-CSRF-Token": token},
    )
    assert updated.status_code == 200
    other_created = other.post(
        "/tickets", json={"title": "另一个模拟故障", "description": "另一个用户的工单。"},
        headers={"X-CSRF-Token": other_token},
    )
    assert other_created.status_code == 201
    other_id = other_created.get_json()["data"]["id"]
    assert other.get(f"/tickets/{other_id}/history").get_json()["data"] == {"items": [], "total": 0}
    assert handler.get(f"/tickets/{other_id}/history").get_json()["data"] == {"items": [], "total": 0}
    assert owner.get(f"/tickets/{ticket_id}/history").get_json()["data"]["total"] == 1
    assert_error(client.get(f"/tickets/{ticket_id}/history"), 401, "AUTH_REQUIRED")
    expected = assert_error(other.get("/tickets/999999/history"), 404, "NOT_FOUND")
    assert assert_error(other.get(f"/tickets/{ticket_id}/history"), 404, "NOT_FOUND") == expected


@pytest.mark.parametrize("ticket_id", ["0", "-1", str(2**63), "not-a-number", "9" * 5000])
def test_history_rejects_invalid_ticket_ids(submitter_client, assert_error, ticket_id):
    client, _ = submitter_client
    assert_error(client.get(f"/tickets/{ticket_id}/history"), 404, "NOT_FOUND")


def test_history_insert_failure_rolls_back_state_then_recovers(
    app, history_ticket, submitter_client, authenticated_client, assert_error, caplog,
):
    owner, _ = submitter_client
    handler, token = authenticated_client("handler")
    ticket_id = history_ticket["id"]
    with app.app_context():
        database = get_db()
        database.execute("""
            CREATE TRIGGER simulated_history_insert_failure
            AFTER INSERT ON ticket_history
            BEGIN
                SELECT RAISE(ABORT, 'private_simulated_history_failure');
            END
        """)
        database.commit()

    caplog.clear()
    with caplog.at_level(logging.INFO):
        failed = handler.patch(
            f"/tickets/{ticket_id}/status", json={"status": "in_progress"},
            headers={"X-CSRF-Token": token},
        )
    assert_error(failed, 500, "INTERNAL_ERROR")
    assert not any("Ticket status changed" in record.getMessage() for record in caplog.records)
    for internal in ("private_simulated_history_failure", "ticket_history", "Traceback"):
        assert internal not in failed.get_data(as_text=True)
    assert owner.get(f"/tickets/{ticket_id}").get_json()["data"] == history_ticket
    assert stored_history(app, ticket_id) == []

    with app.app_context():
        database = get_db()
        database.execute("DROP TRIGGER simulated_history_insert_failure")
        database.commit()
    recovered = handler.patch(
        f"/tickets/{ticket_id}/status", json={"status": "in_progress"},
        headers={"X-CSRF-Token": token},
    )
    assert recovered.status_code == 200
    rows = stored_history(app, ticket_id)
    assert len(rows) == 1
    assert rows[0]["request_id"] == recovered.headers["X-Request-ID"]


def test_reinitializing_schema_preserves_ticket_and_history(
    app, history_ticket, submitter_client, authenticated_client,
):
    owner, _ = submitter_client
    handler, token = authenticated_client("handler")
    ticket_id = history_ticket["id"]
    changed = handler.patch(
        f"/tickets/{ticket_id}/status", json={"status": "in_progress"},
        headers={"X-CSRF-Token": token},
    )
    assert changed.status_code == 200
    before = stored_history(app, ticket_id)
    with app.app_context():
        init_db()
        init_db()
    assert stored_history(app, ticket_id) == before
    assert owner.get(f"/tickets/{ticket_id}").get_json()["data"] == changed.get_json()["data"]


def test_existing_database_adds_history_table_without_fabricating_old_events(
    app, history_ticket, submitter_client, user_ids,
):
    ticket_id = history_ticket["id"]
    # Model an older database that already contains a resolved ticket but has
    # never stored history. Only this test's isolated database is changed.
    with app.app_context():
        database = get_db()
        database.execute("DROP TABLE ticket_history")
        database.execute(
            "UPDATE tickets SET status = 'resolved', resolution = ?, resolved_by = ? WHERE id = ?",
            ("旧版已保存的模拟处理结果。", user_ids["handler"], ticket_id),
        )
        database.commit()
        before = dict(database.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone())
        init_db()
        assert dict(database.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()) == before
    owner, _ = submitter_client
    assert owner.get(f"/tickets/{ticket_id}/history").get_json()["data"] == {"items": [], "total": 0}
