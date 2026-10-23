"""Ticket creation, visibility, pagination, and transactional status history."""

from flask import Blueprint, current_app, g, jsonify, request, url_for

from .auth import csrf_required, require_login
from .db import get_db
from .errors import ApiError
from .validation import read_json_object, required_text

bp = Blueprint("tickets", __name__, url_prefix="/tickets")
PUBLIC_COLUMNS = (
    "id, title, description, creator_id, status, resolution, resolved_by, created_at"
)
HISTORY_COLUMNS = (
    "id, ticket_id, actor_id, from_status, to_status, request_id, created_at"
)
_MAX_SQLITE_INTEGER = (1 << 63) - 1
_STATUSES = frozenset({"pending", "in_progress", "resolved"})


def _parse_ticket_id(value):
    # Check before int(), including paths beyond Python's digit conversion limit.
    if len(value) > 19 or not value.isascii() or not value.isdecimal():
        raise ApiError(404, "NOT_FOUND", "工单不存在。")
    ticket_id = int(value)
    if not 1 <= ticket_id <= _MAX_SQLITE_INTEGER:
        raise ApiError(404, "NOT_FOUND", "工单不存在。")
    return ticket_id


def _pagination_integer(name, default, minimum, maximum):
    value = request.args.get(name)
    if value is None:
        return default
    if len(value) > 19 or not value.isascii() or not value.isdecimal():
        raise ApiError(400, "INVALID_REQUEST", f"{name} 必须是长度不超过 19 位的 ASCII 十进制整数。")
    number = int(value)
    if not minimum <= number <= maximum:
        raise ApiError(400, "INVALID_REQUEST", f"{name} 必须在 {minimum}～{maximum} 之间。")
    return number


def _status_change_payload():
    # The required fields depend on the requested target status.
    try:
        payload = request.get_json()
    except RecursionError as error:
        raise ApiError(400, "INVALID_REQUEST", "JSON 嵌套层级过深。") from error
    if not isinstance(payload, dict):
        raise ApiError(400, "INVALID_REQUEST", "请求内容必须是 JSON 对象。")
    target_status = payload.get("status")
    if not isinstance(target_status, str) or target_status not in _STATUSES:
        raise ApiError(400, "INVALID_REQUEST", "status 必须是 pending、in_progress 或 resolved。")
    expected_fields = {"status", "resolution"} if target_status == "resolved" else {"status"}
    if set(payload) != expected_fields:
        raise ApiError(400, "INVALID_REQUEST", "请求字段缺失或多余。")
    resolution = (
        required_text(payload["resolution"], "resolution", 2000)
        if target_status == "resolved"
        else None
    )
    return target_status, resolution


@bp.post("")
@require_login("submitter")
@csrf_required
def create_ticket():
    payload = read_json_object({"title", "description"})
    title = required_text(payload["title"], "title", 100)
    description = required_text(payload["description"], "description", 2000)
    connection = get_db()
    # Commit on success and roll back on failure before returning a response.
    with connection:
        cursor = connection.execute(
            "INSERT INTO tickets (title, description, creator_id) VALUES (?, ?, ?)",
            (title, description, g.user["id"]),
        )
        ticket = connection.execute(
            f"SELECT {PUBLIC_COLUMNS} FROM tickets WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
    response = jsonify(data=dict(ticket))
    response.status_code = 201
    response.headers["Location"] = url_for("tickets.get_ticket", ticket_id=ticket["id"])
    return response


@bp.get("")
@require_login()
def list_tickets():
    allowed_parameters = {"status", "limit", "offset"}
    for name, values in request.args.lists():
        if name not in allowed_parameters or len(values) != 1:
            raise ApiError(400, "INVALID_REQUEST", "查询参数不支持或重复。")

    status = request.args.get("status")
    if status is not None and status not in _STATUSES:
        raise ApiError(400, "INVALID_REQUEST", "status 必须是 pending、in_progress 或 resolved。")
    limit = _pagination_integer("limit", 20, 1, 100)
    offset = _pagination_integer("offset", 0, 0, _MAX_SQLITE_INTEGER)

    filters = []
    parameters = []
    if g.user["role"] != "handler":
        filters.append("creator_id = ?")
        parameters.append(g.user["id"])
    if status is not None:
        filters.append("status = ?")
        parameters.append(status)
    where = " WHERE " + " AND ".join(filters) if filters else ""
    connection = get_db()
    total = connection.execute(
        "SELECT COUNT(*) FROM tickets" + where, parameters
    ).fetchone()[0]
    rows = connection.execute(
        f"SELECT {PUBLIC_COLUMNS} FROM tickets" + where + " ORDER BY id DESC LIMIT ? OFFSET ?",
        [*parameters, limit, offset],
    ).fetchall()
    return jsonify(
        data={"items": [dict(row) for row in rows], "total": total, "limit": limit, "offset": offset}
    )


def get_visible_ticket(ticket_id):
    """Return a ticket visible to the current user; caller must require login."""
    ticket_id = _parse_ticket_id(ticket_id)
    query = f"SELECT {PUBLIC_COLUMNS} FROM tickets WHERE id = ?"
    parameters = [ticket_id]
    if g.user["role"] != "handler":
        query += " AND creator_id = ?"
        parameters.append(g.user["id"])
    ticket = get_db().execute(query, parameters).fetchone()
    if ticket is None:
        raise ApiError(404, "NOT_FOUND", "工单不存在。")
    return ticket


@bp.get("/<ticket_id>")
@require_login()
def get_ticket(ticket_id):
    return jsonify(data=dict(get_visible_ticket(ticket_id)))


@bp.get("/<ticket_id>/history")
@require_login()
def get_ticket_history(ticket_id):
    ticket = get_visible_ticket(ticket_id)
    rows = get_db().execute(
        f"SELECT {HISTORY_COLUMNS} FROM ticket_history WHERE ticket_id = ? ORDER BY id ASC",
        (ticket["id"],),
    ).fetchall()
    # This workflow permits at most two status changes, so no pagination is needed.
    return jsonify(data={"items": [dict(row) for row in rows], "total": len(rows)})


@bp.patch("/<ticket_id>/status")
@require_login("handler")
@csrf_required
def update_ticket_status(ticket_id):
    ticket_id = _parse_ticket_id(ticket_id)
    target_status, resolution = _status_change_payload()
    connection = get_db()
    with connection:
        current = connection.execute(
            "SELECT status FROM tickets WHERE id = ?", (ticket_id,)
        ).fetchone()
        if current is None:
            raise ApiError(404, "NOT_FOUND", "工单不存在。")
        old_status = current["status"]
        allowed_transitions = {("pending", "in_progress"), ("in_progress", "resolved")}
        if (old_status, target_status) not in allowed_transitions:
            raise ApiError(409, "INVALID_TRANSITION", "工单状态只允许从待处理到处理中，再到已解决。")

        resolved_by = g.user["id"] if target_status == "resolved" else None
        cursor = connection.execute(
            "UPDATE tickets SET status = ?, resolution = ?, resolved_by = ? "
            "WHERE id = ? AND status = ?",
            (target_status, resolution, resolved_by, ticket_id, old_status),
        )
        if cursor.rowcount != 1:
            raise ApiError(409, "INVALID_TRANSITION", "工单状态已改变，请重新查询后再操作。")
        connection.execute(
            "INSERT INTO ticket_history "
            "(ticket_id, actor_id, from_status, to_status, request_id) VALUES (?, ?, ?, ?, ?)",
            (ticket_id, g.user["id"], old_status, target_status, g.request_id),
        )
        ticket = connection.execute(
            f"SELECT {PUBLIC_COLUMNS} FROM tickets WHERE id = ?", (ticket_id,)
        ).fetchone()

    # Log only after the transaction commits; omit the submitted text.
    current_app.logger.info(
        "Ticket status changed request_id=%s ticket_id=%s actor_id=%s old_status=%s new_status=%s",
        g.request_id,
        ticket_id,
        g.user["id"],
        old_status,
        target_status,
    )
    return jsonify(data=dict(ticket))
