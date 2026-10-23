"""Connect visible tickets, local retrieval, and explicitly enabled model calls."""

from flask import Blueprint, current_app, jsonify

from .auth import csrf_required, require_login
from .db import get_db
from .errors import ApiError
from .knowledge import search_knowledge
from .model_service import generate_answer
from .tickets import get_visible_ticket
from .validation import read_json_object, required_text


bp = Blueprint("assistant", __name__)


@bp.post("/tickets/<ticket_id>/answer")
@require_login("handler")
@csrf_required
def answer_ticket(ticket_id):
    ticket = get_visible_ticket(ticket_id)
    payload = read_json_object({"question"})
    question = required_text(payload["question"], "question", 500)
    retrieved = search_knowledge(question, limit=3)
    common = {"ticket_id": ticket["id"], "question": question}
    if not retrieved["items"]:
        return jsonify(data={**common, "status": "insufficient_evidence", "answer": None,
                             "sources": [], "model_called": False})
    if not current_app.config["MODEL_ENABLED"]:
        return jsonify(data={**common, "status": "model_disabled", "answer": None,
                             "sources": retrieved["items"], "model_called": False})
    generated = generate_answer(question, retrieved["items"])
    # Imports may replace chunks while a remote request is in flight. Discard an
    # answer whose cited snapshot can no longer be checked against current data.
    for source in generated["sources"]:
        current = get_db().execute(
            "SELECT d.checksum, c.content FROM knowledge_chunks c "
            "JOIN knowledge_documents d ON d.id=c.document_id WHERE c.id=? AND d.id=?",
            (source["chunk_id"], source["document_id"]),
        ).fetchone()
        if current is None or current["checksum"] != source["checksum"] or current["content"] != source["excerpt"]:
            raise ApiError(409, "KNOWLEDGE_CHANGED", "参考资料已更新，请重新检索后再生成回答。")
    return jsonify(data={**common, **generated, "model_called": True})
