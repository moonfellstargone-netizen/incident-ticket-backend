"""Local knowledge ingestion, retrieval, sources, and ticket visibility."""

import hashlib
from pathlib import Path
import sqlite3

import pytest

from ticket_backend.db import get_db
from ticket_backend.knowledge import (
    MAX_CHUNK_CHARS, MAX_DOCUMENTS, MAX_FILE_BYTES, import_documents, search_knowledge,
)


EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "knowledge"
SOURCE_FIELDS = {
    "chunk_id", "document_id", "slug", "title", "excerpt", "start_line", "end_line",
    "checksum", "score",
}


@pytest.fixture
def knowledge(app):
    with app.app_context():
        return import_documents(EXAMPLES)


def _write(directory, name="sample.md", content="# 样例标题\n\nSQLITE_BUSY database is locked 模拟资料。\n"):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(content.encode("utf-8"))
    return path


def _snapshot(app):
    with app.app_context():
        connection = get_db()
        return (
            [tuple(row) for row in connection.execute("SELECT * FROM knowledge_documents ORDER BY id")],
            [tuple(row) for row in connection.execute("SELECT * FROM knowledge_chunks ORDER BY id")],
        )


@pytest.mark.parametrize("path", [
    "/knowledge/search?q=502", "/knowledge/documents/1", "/tickets/1/knowledge",
])
def test_knowledge_requires_login(client, assert_error, path):
    assert_error(client.get(path), 401, "AUTH_REQUIRED")


@pytest.mark.parametrize(("question", "expected_slug"), [
    ("Nginx 502 upstream Connection refused", "http-502"),
    ("SQLITE_BUSY database is locked", "sqlite-locked"),
    ("登录后 CSRF_FAILED 使用旧令牌怎么办", "csrf-token"),
    ("Markdown 导入 UnicodeDecodeError 编码不是 UTF-8", "markdown-utf8"),
    ("DeepSeek 模型服务超时", "model-timeout"),
    ("title 数字 INVALID_REQUEST", "json-validation"),
])
def test_answerable_questions_find_expected_source(knowledge, submitter_client, question, expected_slug):
    client, _ = submitter_client
    response = client.get("/knowledge/search", query_string={"q": question})
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["status"] == "found"
    assert data["query"] == question
    assert 1 <= len(data["items"]) <= 3
    assert data["items"][0]["slug"] == expected_slug
    for source in data["items"]:
        assert set(source) == SOURCE_FIELDS
        assert source["score"] > 0


@pytest.mark.parametrize("question", [
    "火星基地氧气传感器如何校准", "HTTP 502 如何修复硬盘坏道",
    "如何排查故障并处理问题", "please how to fix", "啊",
    "Nginx HTTP 503", "登录 CSRF_UNKNOWN 令牌",
])
def test_unanswerable_and_similar_but_irrelevant_questions_are_empty(knowledge, app, question):
    with app.app_context():
        result = search_knowledge(question)
    assert result == {"query": question, "status": "insufficient_evidence", "items": []}


def test_empty_corpus_is_explicitly_insufficient(app):
    with app.app_context():
        assert search_knowledge("SQLITE_BUSY")["status"] == "insufficient_evidence"


@pytest.mark.parametrize("question", [
    "怎样查看模型超时的本地原文", "能否查看 DeepSeek 模型超时的原文检索",
    "模型超时以后是否还能查看本地原文", "哪些资料用于核对 DeepSeek 模型超时",
])
def test_common_question_words_do_not_dilute_local_evidence(knowledge, app, question):
    with app.app_context():
        result = search_knowledge(question)
    assert result["status"] == "found"
    assert result["items"][0]["slug"] == "model-timeout"


@pytest.mark.parametrize("question", [
    "怎样查看火星氧气传感器的校准资料", "是否还能查看 PostgreSQL 主从复制延迟",
    "哪些资料用于核对 Windows 蓝屏 MEMORY_MANAGEMENT", "能否查看 Nginx HTTP 503",
    "哪些资料", "能否查看",
])
def test_question_word_normalization_does_not_invent_unknown_evidence(knowledge, app, question):
    with app.app_context():
        result = search_knowledge(question)
    assert result["status"] == "insufficient_evidence"
    assert result["items"] == []


def test_source_ranges_and_checksums_match_imported_original(knowledge, submitter_client):
    client, _ = submitter_client
    items = client.get("/knowledge/search?q=SQLITE_BUSY").get_json()["data"]["items"]
    source = items[0]
    response = client.get(f"/knowledge/documents/{source['document_id']}")
    assert response.status_code == 200
    document = response.get_json()["data"]
    original = (EXAMPLES / f"{source['slug']}.md").read_bytes()
    assert document["content"] == original.decode("utf-8")
    assert source["checksum"] == document["checksum"] == hashlib.sha256(original).hexdigest()
    assert source["excerpt"] == "\n".join(
        document["content"].splitlines()[source["start_line"] - 1:source["end_line"]]
    )


def test_import_is_idempotent_and_cli_reports_unchanged(knowledge, app):
    before = _snapshot(app)
    with app.app_context():
        report = import_documents(EXAMPLES)
    assert report == {"created": 0, "updated": 0, "unchanged": 6, "chunks_written": 0}
    assert _snapshot(app) == before
    result = app.test_cli_runner().invoke(args=["import-knowledge", str(EXAMPLES)])
    assert result.exit_code == 0
    assert "unchanged=6" in result.output
    assert _snapshot(app) == before


def test_changed_source_replaces_chunks_without_reusing_reference_ids(app, tmp_path):
    directory = tmp_path / "docs"
    path = _write(directory)
    with app.app_context():
        first = import_documents(directory)
    before = _snapshot(app)
    previous_id = before[0][0][0]
    old_chunk_ids = {row[0] for row in before[1]}
    path.write_text("# 更新后标题\n\nCSRF_FAILED 登录后更换令牌。\n", encoding="utf-8")
    with app.app_context():
        second = import_documents(directory)
        connection = get_db()
        assert connection.execute("SELECT id FROM knowledge_documents").fetchone()[0] == previous_id
        new_ids = {row[0] for row in connection.execute("SELECT id FROM knowledge_chunks")}
        assert old_chunk_ids.isdisjoint(new_ids)
        assert min(new_ids) > max(old_chunk_ids)
        assert search_knowledge("SQLITE_BUSY")["status"] == "insufficient_evidence"
        assert search_knowledge("CSRF_FAILED")["status"] == "found"
    assert first["created"] == 1 and second["updated"] == 1
    assert _snapshot(app)[0][0][4] != before[0][0][4]


def test_crlf_checksum_and_multi_chunk_line_ranges_remain_exact(app, tmp_path):
    directory = tmp_path / "docs"
    content = "# 多行来源\r\n\r\n" + "\r\n".join(f"line_{number} SQLITE_BUSY " + "文本" * 80 for number in range(20))
    _write(directory, content=content)
    with app.app_context():
        report = import_documents(directory)
        chunks = get_db().execute("SELECT * FROM knowledge_chunks ORDER BY id").fetchall()
        assert report["chunks_written"] > 1
        for chunk in chunks:
            assert len(chunk["content"]) <= MAX_CHUNK_CHARS
            assert chunk["content"] == "\n".join(content.splitlines()[chunk["start_line"] - 1:chunk["end_line"]])
        document = get_db().execute("SELECT * FROM knowledge_documents").fetchone()
        assert document["checksum"] == hashlib.sha256(content.encode("utf-8")).hexdigest()


def test_import_does_not_delete_documents_missing_from_next_directory(app, tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    _write(first, "first.md")
    _write(second, "second.md")
    with app.app_context():
        import_documents(first)
        import_documents(second)
        assert get_db().execute("SELECT COUNT(*) FROM knowledge_documents").fetchone()[0] == 2


@pytest.mark.parametrize("content", [
    b"# title\n\x00 hidden", b"# title\n\xff", b"no heading\n", b"# \n",
    ("# " + "a" * 121).encode(), ("# title\n" + "x" * (MAX_CHUNK_CHARS + 1)).encode(),
    b"# title\n" + b"x" * MAX_FILE_BYTES,
], ids=["nul", "invalid-utf8", "missing-title", "blank-title", "long-title", "long-line", "large-file"])
def test_invalid_file_aborts_entire_batch_before_any_write(app, tmp_path, content):
    directory = tmp_path / "docs"
    _write(directory, "a-valid.md")
    (directory / "z-invalid.md").write_bytes(content)
    before = _snapshot(app)
    with app.app_context(), pytest.raises(ValueError):
        import_documents(directory)
    assert _snapshot(app) == before


@pytest.mark.parametrize("name", ["含中文.md", "bad name.md", "_bad.md", "x" * 81 + ".md"])
def test_invalid_slug_is_rejected(app, tmp_path, name):
    directory = tmp_path / "docs"
    _write(directory, name)
    with app.app_context(), pytest.raises(ValueError):
        import_documents(directory)


def test_too_many_documents_rejected(app, tmp_path):
    directory = tmp_path / "docs"
    for number in range(MAX_DOCUMENTS + 1):
        _write(directory, f"file-{number}.md")
    with app.app_context(), pytest.raises(ValueError):
        import_documents(directory)
    assert _snapshot(app) == ([], [])


def test_batch_size_limit_rejected_without_partial_import(app, tmp_path):
    directory = tmp_path / "docs"
    # Each line and file is permitted individually; the combined byte count is not.
    content = "# title\n" + ("x" * 1000 + "\n") * 120
    for number in range(9):
        _write(directory, f"file-{number}.md", content)
    with app.app_context(), pytest.raises(ValueError):
        import_documents(directory)
    assert _snapshot(app) == ([], [])


def test_subdirectories_are_not_imported_and_empty_import_is_rejected(app, tmp_path):
    directory = tmp_path / "docs"
    _write(directory / "nested")
    with app.app_context(), pytest.raises(ValueError):
        import_documents(directory)


def test_database_error_rolls_back_whole_batch_and_recovers(app, tmp_path):
    directory = tmp_path / "docs"
    _write(directory, "a-first.md")
    _write(directory, "z-last.md")
    with app.app_context():
        connection = get_db()
        connection.execute(
            "CREATE TRIGGER abort_chunk BEFORE INSERT ON knowledge_chunks "
            "WHEN NEW.document_id = 2 BEGIN SELECT RAISE(ABORT, 'simulated import failure'); END"
        )
        with pytest.raises(sqlite3.IntegrityError):
            import_documents(directory)
    assert _snapshot(app) == ([], [])
    with app.app_context():
        get_db().execute("DROP TRIGGER abort_chunk")
        assert import_documents(directory)["created"] == 2


@pytest.mark.parametrize("query_string", [
    {}, {"q": ""}, {"q": "   "}, {"q": "x" * 501}, {"q": "a\x00b"},
    {"q": "502", "limit": "0"}, {"q": "502", "limit": "6"},
    {"q": "502", "limit": "-1"}, {"q": "502", "limit": "1.0"},
    {"q": "502", "limit": "１"}, {"q": "502", "limit": "9" * 5000},
    {"q": "502", "unknown": "value"}, [("q", "502"), ("q", "SQLITE_BUSY")],
    [("q", "502"), ("limit", "1"), ("limit", "2")],
])
def test_search_rejects_invalid_parameters(submitter_client, assert_error, query_string):
    client, _ = submitter_client
    assert_error(client.get("/knowledge/search", query_string=query_string), 400, "INVALID_REQUEST")


@pytest.mark.parametrize("document_id", ["0", "-1", "abc", "１", "9" * 5000, str(2**63), "999"])
def test_document_ids_are_bounded_and_not_found(submitter_client, assert_error, document_id):
    client, _ = submitter_client
    assert_error(client.get(f"/knowledge/documents/{document_id}"), 404, "NOT_FOUND")


def test_query_trimming_and_deterministic_limit(knowledge, app):
    with app.app_context():
        first = search_knowledge("  SQLITE_BUSY  ", limit=1)
        second = search_knowledge("SQLITE_BUSY", limit=1)
    assert first == second
    assert first["query"] == "SQLITE_BUSY"
    assert len(first["items"]) == 1


def test_ticket_suggestions_use_visible_ticket_and_report_truncation(
    knowledge, app, submitter_client, authenticated_client, assert_error
):
    client, token = submitter_client
    response = client.post("/tickets", json={
        "title": "SQLITE_BUSY database is locked",
        "description": "模拟写事务没有释放。" + " " * 1000 + "结束",
    }, headers={"X-CSRF-Token": token})
    ticket = response.get_json()["data"]
    suggestion = client.get(f"/tickets/{ticket['id']}/knowledge")
    assert suggestion.status_code == 200
    data = suggestion.get_json()["data"]
    assert data["ticket_id"] == ticket["id"]
    assert data["query_truncated"] is True
    assert data["query"].startswith(ticket["title"])
    assert len(data["query"]) <= 500
    other, _ = authenticated_client("submitter_b")
    assert_error(other.get(f"/tickets/{ticket['id']}/knowledge"), 404, "NOT_FOUND")
    handler, _ = authenticated_client("handler")
    assert handler.get(f"/tickets/{ticket['id']}/knowledge").get_json()["data"] == data
    assert_error(client.get(f"/tickets/{ticket['id']}/knowledge?q=replace"), 400, "INVALID_REQUEST")


def test_cli_invalid_import_reports_failure_without_writes(app, tmp_path):
    directory = tmp_path / "docs"
    _write(directory, content="not markdown title")
    result = app.test_cli_runner().invoke(args=["import-knowledge", str(directory)])
    assert result.exit_code != 0
    assert "title" in result.output
    assert _snapshot(app) == ([], [])
