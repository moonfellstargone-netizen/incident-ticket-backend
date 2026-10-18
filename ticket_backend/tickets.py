"""Create and read tickets with server-controlled ownership."""

from flask import Blueprint, g, jsonify, url_for

from .auth import csrf_required, require_login
from .db import get_db
from .errors import ApiError
from .validation import read_json_object, required_text

bp = Blueprint("tickets", __name__, url_prefix="/tickets")
PUBLIC_COLUMNS = "id, title, description, creator_id, status, created_at"


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


@bp.get("/<ticket_id>")
@require_login()
def get_ticket(ticket_id):
    # Check the length before int(), including paths beyond Python's digit limit.
    if (
        len(ticket_id) > 19
        or not ticket_id.isascii()
        or not ticket_id.isdecimal()
        or not 1 <= int(ticket_id) <= (1 << 63) - 1
    ):
        raise ApiError(404, "NOT_FOUND", "工单不存在。")
    query = f"SELECT {PUBLIC_COLUMNS} FROM tickets WHERE id = ?"
    parameters = [int(ticket_id)]
    if g.user["role"] != "handler":
        query += " AND creator_id = ?"
        parameters.append(g.user["id"])
    ticket = get_db().execute(query, parameters).fetchone()
    if ticket is None:
        raise ApiError(404, "NOT_FOUND", "工单不存在。")
    return jsonify(data=dict(ticket))
