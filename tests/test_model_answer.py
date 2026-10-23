"""Real local retrieval with a strictly mocked, non-billable model transport."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from io import BytesIO
import json
from pathlib import Path
import sqlite3
import subprocess
from threading import Barrier
from unittest.mock import Mock
from urllib.error import HTTPError, URLError

import pytest

from ticket_backend import create_app
from ticket_backend import model_service
from ticket_backend.db import get_db
from ticket_backend.knowledge import import_documents, search_knowledge


ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples" / "knowledge"
QUESTION = "Nginx 502 upstream Connection refused"
FAKE_KEY = "test-only-never-a-real-provider-key"
TEST_SECRET = "test-only-secret-key-with-at-least-32-characters"


@pytest.fixture(autouse=True)
def transport(monkeypatch):
    # No test in this file can reach a real provider, even if developer secrets
    # and the opt-in flag happen to be present in the process environment.
    opener = Mock(side_effect=AssertionError("External network is forbidden in this suite."))
    monkeypatch.setattr(model_service, "_open_request", opener)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("TICKET_ENABLE_LLM", raising=False)
    monkeypatch.delenv("TICKET_MODEL_BUDGET_CNY", raising=False)
    return opener


@pytest.fixture
def model_case(app, submitter_client, authenticated_client, tmp_path, monkeypatch):
    app.config.update(
        MODEL_ENABLED=True,
        MODEL_BUDGET_CNY="10",
        MODEL_KEY_FILE=str(tmp_path / "missing-provider-key.txt"),
    )
    monkeypatch.setenv("DEEPSEEK_API_KEY", FAKE_KEY)
    with app.app_context():
        import_documents(EXAMPLES)
    submitter, token = submitter_client
    created = submitter.post(
        "/tickets",
        json={"title": "模拟 Nginx 502", "description": "模拟上游 Connection refused，等待处理。"},
        headers={"X-CSRF-Token": token},
    )
    assert created.status_code == 201
    ticket_id = created.get_json()["data"]["id"]
    handler, handler_token = authenticated_client("handler")
    return app, ticket_id, handler, handler_token


def post_answer(case, question=QUESTION):
    _, ticket_id, handler, token = case
    return handler.post(
        f"/tickets/{ticket_id}/answer", json={"question": question},
        headers={"X-CSRF-Token": token},
    )


def ledger_rows(app):
    path = Path(app.config["MODEL_USAGE_DATABASE"])
    if not path.exists():
        return []
    with closing(sqlite3.connect(path)) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute("SELECT * FROM model_calls ORDER BY id")]


def request_context(request):
    body = json.loads(request.data)
    return body, json.loads(body["messages"][1]["content"])


def provider_bytes(request, change_content=None, change_envelope=None):
    _, context = request_context(request)
    content = {
        "answer": "根据模拟资料，先核对 Nginx 上游地址、进程和监听端口，再复测。",
        "citation_ids": [context["sources"][0]["chunk_id"]],
        "insufficient_evidence": False,
    }
    if change_content is not None:
        change_content(content)
    envelope = {
        "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(content)}}],
        "usage": {"prompt_tokens": 123, "completion_tokens": 45},
    }
    if change_envelope is not None:
        change_envelope(envelope)
    return json.dumps(envelope).encode("utf-8")


def successful_transport(transport, change_content=None, change_envelope=None):
    transport.side_effect = lambda request, timeout: BytesIO(provider_bytes(
        request, change_content=change_content, change_envelope=change_envelope,
    ))


def test_model_is_disabled_by_default_and_testing_ignores_environment_opt_in(tmp_path, monkeypatch):
    config = {"TESTING": True, "DATABASE": str(tmp_path / "tickets.sqlite3"), "SECRET_KEY": TEST_SECRET}
    default = create_app(config)
    assert default.config["MODEL_ENABLED"] is False
    assert default.config["MODEL_BUDGET_CNY"] == "10"
    assert Path(default.config["MODEL_USAGE_DATABASE"]).parent == tmp_path.resolve()
    assert not Path(default.config["MODEL_USAGE_DATABASE"]).exists()
    monkeypatch.setenv("TICKET_ENABLE_LLM", "1")
    assert create_app(config).config["MODEL_ENABLED"] is False
    assert create_app({**config, "MODEL_ENABLED": True}).config["MODEL_ENABLED"] is True


def test_non_test_app_requires_explicit_environment_opt_in(tmp_path, monkeypatch):
    config = {"DATABASE": str(tmp_path / "tickets.sqlite3"), "SECRET_KEY": TEST_SECRET}
    assert create_app(config).config["MODEL_ENABLED"] is False
    monkeypatch.setenv("TICKET_ENABLE_LLM", "1")
    assert create_app(config).config["MODEL_ENABLED"] is True


def test_test_apps_refuse_development_budget_ledger(tmp_path):
    with pytest.raises(ValueError, match="development model usage database"):
        create_app({
            "TESTING": True, "DATABASE": str(tmp_path / "tickets.sqlite3"), "SECRET_KEY": TEST_SECRET,
            "MODEL_USAGE_DATABASE": str(ROOT / "instance" / "model-usage.sqlite3"),
        })


def test_default_key_file_is_excluded_by_git(tmp_path):
    app = create_app({"TESTING": True, "DATABASE": str(tmp_path / "tickets.sqlite3"), "SECRET_KEY": TEST_SECRET})
    assert Path(app.config["MODEL_KEY_FILE"]) == ROOT / "instance" / "deepseek_api_key.txt"
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "instance/deepseek_api_key.txt"],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0


def test_answer_requires_handler_session_csrf_and_existing_ticket(
    model_case, client, submitter_client, assert_error, transport,
):
    app, ticket_id, handler, token = model_case
    url = f"/tickets/{ticket_id}/answer"
    owner, owner_token = submitter_client
    assert_error(client.post(url, json={"question": QUESTION}), 401, "AUTH_REQUIRED")
    assert_error(owner.post(url, json={"question": QUESTION}, headers={"X-CSRF-Token": owner_token}), 403, "FORBIDDEN")
    assert_error(handler.post(url, json={"question": QUESTION}), 403, "CSRF_FAILED")
    assert_error(handler.post(
        "/tickets/999999/answer", json={"question": QUESTION}, headers={"X-CSRF-Token": token},
    ), 404, "NOT_FOUND")
    assert ledger_rows(app) == []
    transport.assert_not_called()


@pytest.mark.parametrize("payload", [
    {}, {"question": " "}, {"question": "x" * 501}, {"question": None},
    {"question": ["502"]}, {"question": "502\x00upstream"},
    {"question": QUESTION, "model": "forged-model"},
])
def test_invalid_questions_do_not_reach_transport_or_budget(model_case, payload, assert_error, transport):
    app, ticket_id, handler, token = model_case
    response = handler.post(
        f"/tickets/{ticket_id}/answer", json=payload, headers={"X-CSRF-Token": token},
    )
    assert_error(response, 400, "INVALID_REQUEST")
    assert ledger_rows(app) == []
    transport.assert_not_called()


def test_insufficient_retrieval_never_calls_model_even_when_enabled(model_case, transport):
    app, ticket_id, _, _ = model_case
    response = post_answer(model_case, "火星基地氧气传感器如何校准")
    assert response.status_code == 200
    assert response.get_json()["data"] == {
        "ticket_id": ticket_id, "question": "火星基地氧气传感器如何校准",
        "status": "insufficient_evidence", "answer": None, "sources": [], "model_called": False,
    }
    assert ledger_rows(app) == []
    transport.assert_not_called()


def test_disabled_model_returns_actual_retrieved_sources_without_generated_answer(model_case, transport):
    app, _, _, _ = model_case
    app.config["MODEL_ENABLED"] = False
    with app.app_context():
        expected = search_knowledge(QUESTION)["items"]
    response = post_answer(model_case)
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["status"] == "model_disabled"
    assert data["model_called"] is False and data["answer"] is None
    assert data["sources"] == expected and expected[0]["slug"] == "http-502"
    assert ledger_rows(app) == []
    transport.assert_not_called()


def test_missing_key_has_no_network_request_or_budget_reservation(model_case, monkeypatch, transport, assert_error):
    app, _, _, _ = model_case
    monkeypatch.delenv("DEEPSEEK_API_KEY")
    assert_error(post_answer(model_case), 503, "MODEL_NOT_CONFIGURED")
    assert ledger_rows(app) == []
    transport.assert_not_called()


def test_local_key_file_works_without_exposing_key_in_response_or_logs(
    model_case, monkeypatch, transport, caplog,
):
    app, _, _, _ = model_case
    monkeypatch.delenv("DEEPSEEK_API_KEY")
    Path(app.config["MODEL_KEY_FILE"]).write_text(FAKE_KEY + "\n", encoding="utf-8")
    successful_transport(transport)
    response = post_answer(model_case)
    assert response.status_code == 200
    sent = transport.call_args.args[0]
    assert sent.get_header("Authorization") == "Bearer " + FAKE_KEY
    assert FAKE_KEY not in response.get_data(as_text=True)
    assert FAKE_KEY not in caplog.text


def test_success_cites_only_this_retrieval_and_sources_are_returned_by_server(model_case, transport):
    app, ticket_id, handler, _ = model_case
    with app.app_context():
        expected = search_knowledge(QUESTION)["items"]
    successful_transport(transport)
    response = post_answer(model_case)
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["ticket_id"] == ticket_id
    assert data["status"] == "answered" and data["model_called"] is True
    assert data["provider"] == "deepseek" and data["model"] == "deepseek-flash"
    assert data["sources"] == [expected[0]]
    assert data["usage"] == {"prompt_tokens": 123, "completion_tokens": 45}
    source = data["sources"][0]
    original = handler.get(f"/knowledge/documents/{source['document_id']}").get_json()["data"]
    assert source["checksum"] == original["checksum"]
    assert source["excerpt"] == "\n".join(original["content"].splitlines()[source["start_line"] - 1:source["end_line"]])
    request, = transport.call_args.args
    body, context = request_context(request)
    assert request.full_url == "https://api.deepseek.com/chat/completions"
    assert transport.call_args.kwargs == {"timeout": 10}
    assert body["model"] == "deepseek-flash" and body["max_tokens"] == 600
    assert body["stream"] is False
    assert context["question"] == QUESTION
    assert context["sources"] == [
        {"chunk_id": row["chunk_id"], "title": row["title"], "text": row["excerpt"]} for row in expected
    ]
    rows = ledger_rows(app)
    assert len(rows) == 1
    assert rows[0]["request_id"] == response.headers["X-Request-ID"]
    assert rows[0]["status"] == "succeeded" and rows[0]["reserved_micros"] == 500_000
    assert rows[0]["estimated_cost_micros"] == 123 * 2 + 45 * 8


def test_model_can_report_insufficient_evidence_without_pretending_to_answer(model_case, transport):
    successful_transport(transport, change_content=lambda content: content.update(
        answer="", citation_ids=[], insufficient_evidence=True,
    ))
    response = post_answer(model_case)
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["status"] == "insufficient_evidence"
    assert data["answer"] is None and data["sources"] == [] and data["model_called"] is True


@pytest.mark.parametrize("case", [
    "fabricated", "duplicate", "empty", "boolean", "blank-answer", "extra-sources", "inconsistent-insufficient",
])
def test_invalid_citations_or_content_are_rejected_without_returning_generated_text(
    model_case, transport, assert_error, case,
):
    def change(content):
        if case == "fabricated":
            content["citation_ids"] = [999999]
        elif case == "duplicate":
            content["citation_ids"] *= 2
        elif case == "empty":
            content["citation_ids"] = []
        elif case == "boolean":
            content["citation_ids"] = [True]
        elif case == "blank-answer":
            content["answer"] = "  "
        elif case == "extra-sources":
            content["sources"] = [{"excerpt": "provider-forged-source"}]
        elif case == "inconsistent-insufficient":
            content["insufficient_evidence"] = True

    successful_transport(transport, change_content=change)
    response = post_answer(model_case)
    assert_error(response, 502, "MODEL_INVALID_ANSWER")
    assert "provider-forged-source" not in response.get_data(as_text=True)
    rows = ledger_rows(model_case[0])
    assert len(rows) == 1 and rows[0]["status"] == "invalid"
    assert rows[0]["reserved_micros"] == 500_000
    assert transport.call_count == 1


def test_existing_corpus_chunk_not_in_current_retrieval_cannot_be_cited(model_case, transport, assert_error):
    app, _, _, _ = model_case
    with app.app_context():
        current_ids = {item["chunk_id"] for item in search_knowledge(QUESTION)["items"]}
        unrelated = next(row[0] for row in get_db().execute("SELECT id FROM knowledge_chunks") if row[0] not in current_ids)
    successful_transport(transport, change_content=lambda content: content.update(citation_ids=[unrelated]))
    assert_error(post_answer(model_case), 502, "MODEL_INVALID_ANSWER")


def test_source_reimport_during_call_discards_stale_answer_and_retains_budget(
    model_case, transport, assert_error, tmp_path,
):
    app, _, _, _ = model_case
    app.config["MODEL_BUDGET_CNY"] = "0.5"
    with app.app_context():
        original = search_knowledge(QUESTION)["items"][0]
    updated_directory = tmp_path / "updated-knowledge"
    updated_directory.mkdir()
    original_file = EXAMPLES / f"{original['slug']}.md"
    (updated_directory / original_file.name).write_bytes(
        original_file.read_bytes() + "\n## 本次回归修订\n\n模拟参考资料已更新，需要重新核对。\n".encode("utf-8")
    )

    def respond_after_import(request, timeout):
        # Capture a valid response citing the snapshot sent to the provider,
        # then replace that source through the real importer while in flight.
        old_response = provider_bytes(request)
        with app.app_context():
            report = import_documents(updated_directory)
            assert report["updated"] == 1
            assert get_db().execute(
                "SELECT id FROM knowledge_chunks WHERE id = ?", (original["chunk_id"],),
            ).fetchone() is None
        return BytesIO(old_response)

    transport.side_effect = respond_after_import
    response = post_answer(model_case)
    assert_error(response, 409, "KNOWLEDGE_CHANGED")
    assert "根据模拟资料，先核对" not in response.get_data(as_text=True)
    with app.app_context():
        current = search_knowledge(QUESTION)["items"][0]
    assert current["chunk_id"] != original["chunk_id"]
    assert current["checksum"] != original["checksum"]
    row, = ledger_rows(app)
    assert row["status"] == "succeeded"  # Provider succeeded; its stale answer was discarded.
    assert row["request_id"] == response.headers["X-Request-ID"]
    assert row["reserved_micros"] == 500_000 and row["estimated_cost_micros"] > 0
    assert_error(post_answer(model_case), 429, "MODEL_BUDGET_EXHAUSTED")
    assert transport.call_count == 1 and len(ledger_rows(app)) == 1


@pytest.mark.parametrize("case", [
    "outer-json", "inner-json", "truncated", "missing-usage", "boolean-tokens",
    "negative-tokens", "too-many-output-tokens", "too-many-input-tokens", "oversize-response",
])
def test_invalid_provider_envelopes_return_safe_errors_and_keep_reservation(
    model_case, transport, assert_error, caplog, case,
):
    def respond(request, timeout):
        def change(envelope):
            if case == "inner-json":
                envelope["choices"][0]["message"]["content"] = "{" + FAKE_KEY
            elif case == "truncated":
                envelope["choices"][0]["finish_reason"] = "length"
            elif case == "missing-usage":
                del envelope["usage"]
            elif case == "boolean-tokens":
                envelope["usage"]["prompt_tokens"] = True
            elif case == "negative-tokens":
                envelope["usage"]["prompt_tokens"] = -1
            elif case == "too-many-output-tokens":
                envelope["usage"]["completion_tokens"] = 601
            elif case == "too-many-input-tokens":
                envelope["usage"]["prompt_tokens"] = 100001

        if case == "outer-json":
            return BytesIO(("not-json " + FAKE_KEY).encode())
        if case == "oversize-response":
            return BytesIO(b"x" * 65_537)
        return BytesIO(provider_bytes(request, change_envelope=change))

    transport.side_effect = respond
    response = post_answer(model_case)
    assert_error(response, 502, "MODEL_INVALID_ANSWER")
    assert FAKE_KEY not in response.get_data(as_text=True) and QUESTION not in response.get_data(as_text=True)
    assert FAKE_KEY not in caplog.text and QUESTION not in caplog.text
    row, = ledger_rows(model_case[0])
    assert row["status"] == "invalid" and row["reserved_micros"] == 500_000
    assert row["prompt_tokens"] is None and row["estimated_cost_micros"] is None
    assert transport.call_count == 1


@pytest.mark.parametrize(("failure", "status", "code"), [
    ("timeout", 504, "MODEL_TIMEOUT"), ("url-error", 504, "MODEL_TIMEOUT"),
    (429, 503, "MODEL_UNAVAILABLE"), (500, 503, "MODEL_UNAVAILABLE"),
    (401, 502, "MODEL_UPSTREAM_ERROR"),
])
def test_provider_failures_are_safe_not_retried_and_reservations_are_retained(
    model_case, transport, assert_error, caplog, failure, status, code,
):
    app, _, _, _ = model_case
    app.config["MODEL_BUDGET_CNY"] = "0.5"
    private_detail = FAKE_KEY + " " + QUESTION
    if failure == "timeout":
        error = TimeoutError(private_detail)
    elif failure == "url-error":
        error = URLError(private_detail)
    else:
        error = HTTPError(model_service.ENDPOINT, failure, private_detail, {}, BytesIO(private_detail.encode()))
    transport.side_effect = error
    response = post_answer(model_case)
    assert_error(response, status, code)
    assert private_detail not in response.get_data(as_text=True)
    assert FAKE_KEY not in response.get_data(as_text=True) and QUESTION not in response.get_data(as_text=True)
    assert FAKE_KEY not in caplog.text and QUESTION not in caplog.text
    row, = ledger_rows(app)
    assert row["status"] == "failed" and row["reserved_micros"] == 500_000
    assert_error(post_answer(model_case), 429, "MODEL_BUDGET_EXHAUSTED")
    assert len(ledger_rows(app)) == 1
    assert transport.call_count == 1


def test_budget_ledger_survives_application_recreation(model_case, transport, login_user, assert_error):
    app, ticket_id, _, _ = model_case
    app.config["MODEL_BUDGET_CNY"] = "1"
    successful_transport(transport)
    assert post_answer(model_case).status_code == 200
    restarted = create_app({
        "TESTING": True, "DATABASE": app.config["DATABASE"], "SECRET_KEY": TEST_SECRET,
        "MODEL_ENABLED": True, "MODEL_BUDGET_CNY": "1", "MODEL_KEY_FILE": app.config["MODEL_KEY_FILE"],
    })
    assert restarted.config["MODEL_USAGE_DATABASE"] == app.config["MODEL_USAGE_DATABASE"]
    new_handler = restarted.test_client()
    login = login_user(new_handler, "handler")
    restarted_case = (restarted, ticket_id, new_handler, login.get_json()["data"]["csrf_token"])
    assert post_answer(restarted_case).status_code == 200
    assert_error(post_answer(restarted_case), 429, "MODEL_BUDGET_EXHAUSTED")
    assert transport.call_count == 2
    assert sum(row["reserved_micros"] for row in ledger_rows(app)) == 1_000_000


def test_simultaneous_reservations_cannot_exceed_remaining_budget(
    model_case, authenticated_client, transport, assert_error,
):
    app, ticket_id, _, _ = model_case
    app.config["MODEL_BUDGET_CNY"] = "0.5"
    first, first_token = authenticated_client("handler")
    second, second_token = authenticated_client("handler")
    successful_transport(transport)
    ready = Barrier(2)

    def attempt(handler, token):
        ready.wait(timeout=10)
        return post_answer((app, ticket_id, handler, token))

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(attempt, client, token) for client, token in (
            (first, first_token), (second, second_token),
        )]
        responses = [future.result(timeout=15) for future in futures]
    assert sorted(response.status_code for response in responses) == [200, 429]
    assert transport.call_count == 1
    for response in responses:
        if response.status_code == 429:
            assert_error(response, 429, "MODEL_BUDGET_EXHAUSTED")
    row, = ledger_rows(app)
    assert row["reserved_micros"] == 500_000 and row["status"] == "succeeded"


@pytest.mark.parametrize("budget", ["0", "11", "NaN", "bad"])
def test_invalid_budget_rejects_before_network_or_reservation(model_case, transport, assert_error, budget):
    app, _, _, _ = model_case
    app.config["MODEL_BUDGET_CNY"] = budget
    assert_error(post_answer(model_case), 503, "MODEL_NOT_CONFIGURED")
    assert ledger_rows(app) == []
    transport.assert_not_called()


def test_insufficient_budget_never_calls_provider(model_case, transport, assert_error):
    app, _, _, _ = model_case
    app.config["MODEL_BUDGET_CNY"] = "0.49"
    assert_error(post_answer(model_case), 429, "MODEL_BUDGET_EXHAUSTED")
    assert ledger_rows(app) == []
    transport.assert_not_called()


def test_default_ten_yuan_budget_allows_at_most_twenty_attempts(model_case, transport, assert_error):
    successful_transport(transport)
    for _ in range(20):
        assert post_answer(model_case).status_code == 200
    assert_error(post_answer(model_case), 429, "MODEL_BUDGET_EXHAUSTED")
    assert transport.call_count == 20
    rows = ledger_rows(model_case[0])
    assert len(rows) == 20 and sum(row["reserved_micros"] for row in rows) == 10_000_000
