"""A real deferred-constraint COMMIT failure must not persist partial writes."""

import logging

import pytest

from ticket_backend.db import get_db


@pytest.mark.parametrize("target_status", ["in_progress", "resolved"])
def test_commit_failure_rolls_back_ticket_and_trigger_writes_then_recovers(
    app, submitter_client, authenticated_client, assert_error, caplog, target_status,
):
    submitter, submitter_token = submitter_client
    created = submitter.post(
        "/tickets",
        json={"title": "模拟提交失败", "description": "仅用于个人项目的数据库故障验证。"},
        headers={"X-CSRF-Token": submitter_token},
    )
    assert created.status_code == 201
    ticket_id = created.get_json()["data"]["id"]
    handler, handler_token = authenticated_client("handler")
    status_url = f"/tickets/{ticket_id}/status"
    headers = {"X-CSRF-Token": handler_token}
    if target_status == "resolved":
        assert handler.patch(
            status_url, json={"status": "in_progress"}, headers=headers,
        ).status_code == 200

    with app.app_context():
        database = get_db()
        before = dict(database.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone())
        history_before = [dict(row) for row in database.execute(
            "SELECT * FROM ticket_history WHERE ticket_id = ? ORDER BY id", (ticket_id,),
        ).fetchall()]
        # This insert succeeds inside the trigger. The foreign key is checked
        # only at COMMIT, after the UPDATE and the view's final SELECT succeed.
        database.executescript("""
            CREATE TABLE simulated_deferred_reference (
                user_id INTEGER NOT NULL REFERENCES users(id)
                    DEFERRABLE INITIALLY DEFERRED
            );
            CREATE TRIGGER simulated_commit_failure
            AFTER UPDATE ON tickets
            BEGIN
                INSERT INTO simulated_deferred_reference (user_id) VALUES (-1);
            END;
        """)
        database.commit()

    payload = {"status": target_status}
    if target_status == "resolved":
        payload["resolution"] = "模拟调整配置并复测通过。"
    caplog.clear()
    with caplog.at_level(logging.INFO):
        failed = handler.patch(status_url, json=payload, headers=headers)
    assert_error(failed, 500, "INTERNAL_ERROR")
    assert any(
        record.exc_info and "FOREIGN KEY constraint failed" in str(record.exc_info[1])
        for record in caplog.records
    )
    assert not any("Ticket status changed" in record.getMessage() for record in caplog.records)
    for detail in ("FOREIGN KEY", "simulated_deferred_reference", "Traceback", app.config["DATABASE"]):
        assert detail not in failed.get_data(as_text=True)
    with app.app_context():
        database = get_db()
        assert dict(database.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()) == before
        assert [dict(row) for row in database.execute(
            "SELECT * FROM ticket_history WHERE ticket_id = ? ORDER BY id", (ticket_id,),
        ).fetchall()] == history_before
        assert database.execute("SELECT COUNT(*) FROM simulated_deferred_reference").fetchone()[0] == 0
        database.execute("DROP TRIGGER simulated_commit_failure")
        database.commit()

    caplog.clear()
    with caplog.at_level(logging.INFO):
        recovered = handler.patch(status_url, json=payload, headers=headers)
    assert recovered.status_code == 200
    result = recovered.get_json()["data"]
    assert result["status"] == target_status
    if target_status == "resolved":
        assert result["resolution"] == payload["resolution"]
        assert result["resolved_by"] is not None
    else:
        assert result["resolution"] is None and result["resolved_by"] is None
    with app.app_context():
        database = get_db()
        assert dict(database.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()) == result
        history_after = [dict(row) for row in database.execute(
            "SELECT * FROM ticket_history WHERE ticket_id = ? ORDER BY id", (ticket_id,),
        ).fetchall()]
        assert history_after[:-1] == history_before
        assert history_after[-1]["to_status"] == target_status
        assert history_after[-1]["request_id"] == recovered.headers["X-Request-ID"]
        assert all(row["request_id"] != failed.headers["X-Request-ID"] for row in history_after)
        assert database.execute("SELECT COUNT(*) FROM simulated_deferred_reference").fetchone()[0] == 0
    assert sum("Ticket status changed" in record.getMessage() for record in caplog.records) == 1
