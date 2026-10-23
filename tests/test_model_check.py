"""Model check helpers use fake keys and a non-billable transport only."""

from io import BytesIO
import json
from pathlib import Path
from unittest.mock import Mock
from urllib.error import HTTPError

import pytest

from scripts import check_model
from ticket_backend import create_app, model_service
from ticket_backend.errors import ApiError


FAKE_KEY = "test-only-model-check-key"


@pytest.fixture
def check_app(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", FAKE_KEY)
    app = create_app({"TESTING": True, "DATABASE": str(tmp_path / "check.sqlite3"),
                      "SECRET_KEY": "local-check-test-secret-with-at-least-32-characters",
                      "MODEL_ENABLED": True, "MODEL_KEY_FILE": str(tmp_path / "missing-key.txt")})
    transport = Mock(side_effect=AssertionError("Real network forbidden in tests."))
    monkeypatch.setattr(model_service, "_open_request", transport)
    return app, transport


@pytest.mark.parametrize("arguments", [[], ["--suite"]])
def test_live_flag_is_required_before_preparing_data_or_calling_provider(monkeypatch, arguments):
    factory = Mock(side_effect=AssertionError("App must not be created without opt-in."))
    monkeypatch.setattr(check_model, "create_app", factory)
    with pytest.raises(SystemExit) as stopped:
        check_model.main(arguments)
    assert stopped.value.code == 2
    factory.assert_not_called()


@pytest.mark.parametrize("insufficient", [False, True])
def test_helper_uses_one_call_and_reports_answer_or_refusal(check_app, insufficient):
    app, transport = check_app

    def respond(request, timeout):
        messages = json.loads(request.data)["messages"]
        context = json.loads(messages[1]["content"])
        content = {"answer": "" if insufficient else "模拟资料建议核对上游进程和监听端口。",
                   "citation_ids": [] if insufficient else [context["sources"][0]["chunk_id"]],
                   "insufficient_evidence": insufficient}
        return BytesIO(json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(content)}}],
                                  "usage": {"prompt_tokens": 123, "completion_tokens": 40}}).encode())

    transport.side_effect = respond
    report = check_model.check_connection(app)
    assert transport.call_count == 1
    assert report["maximum_provider_attempts"] == 1
    assert report["status"] == ("needs_review" if insufficient else "passed")
    assert report["result"]["model_called"] is True
    if not insufficient:
        assert report["result"]["sources"][0]["slug"] == "http-502"
    assert FAKE_KEY not in json.dumps(report)


def test_helper_returns_safe_provider_error_without_retry(check_app):
    app, transport = check_app
    transport.side_effect = HTTPError(model_service.ENDPOINT, 402, FAKE_KEY, {}, BytesIO(FAKE_KEY.encode()))
    report = check_model.check_connection(app)
    assert transport.call_count == 1
    assert report["status"] == "failed"
    assert report["error"]["code"] == "MODEL_BALANCE_INSUFFICIENT"
    assert FAKE_KEY not in json.dumps(report)


def _answered_response(request, timeout):
    context = json.loads(json.loads(request.data)["messages"][1]["content"])
    content = {"answer": "模拟建议：请核对已引用资料中的对应排查步骤。", "insufficient_evidence": False,
               "citation_ids": [context["sources"][0]["chunk_id"]]}
    return BytesIO(json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(content)}}],
                              "usage": {"prompt_tokens": 123, "completion_tokens": 40}}).encode())


def test_suite_checks_three_answerable_and_three_unanswerable_cases_without_extra_calls(check_app):
    app, transport = check_app
    transport.side_effect = _answered_response
    report = check_model.check_suite(app)
    assert transport.call_count == 3
    assert report["status"] == "passed"
    assert report["executed_cases"] == 6 and report["stopped_early"] is False
    assert report["maximum_provider_attempts"] == 6 and report["expected_provider_calls"] == 3
    assert report["reported_model_calls"] == 3
    assert report["semantic_review"] == "pending manual review"
    assert "fixed development cases" in report["dataset"]
    assert "not a held-out" in report["dataset"]
    assert [case["case_id"] for case in report["cases"]] == list(check_model.SUITE_CASE_IDS)
    for case in report["cases"][:3]:
        assert case["result"]["model_called"] is True
        assert set(case["expected_slugs"]) <= {source["slug"] for source in case["result"]["sources"]}
        assert case["checks"]["expected_sources_present"] is True
        assert case["source_checks"]
        assert all(source["original_available"] and source["checksum_matches"]
                   and source["excerpt_matches"] and source["slug_matches"] for source in case["source_checks"])
    for case in report["cases"][3:]:
        assert case["result"]["status"] == "insufficient_evidence"
        assert case["result"]["model_called"] is False
        assert all(case["checks"].values())
    requests = [json.loads(json.loads(call.args[0].data)["messages"][1]["content"])["question"]
                for call in transport.call_args_list]
    assert requests == [case["question"] for case in report["cases"][:3]]
    assert FAKE_KEY not in json.dumps(report)


def test_suite_stops_after_provider_failure_and_does_not_retry(check_app):
    app, transport = check_app

    def respond(request, timeout):
        if transport.call_count == 2:
            raise HTTPError(model_service.ENDPOINT, 402, FAKE_KEY, {}, BytesIO(FAKE_KEY.encode()))
        return _answered_response(request, timeout)

    transport.side_effect = respond
    report = check_model.check_suite(app)
    assert report["status"] == "failed" and report["stopped_early"] is True
    assert transport.call_count == report["executed_cases"] == 2
    assert report["stopped_after_case"] == "K02"
    assert report["cases"][0]["status"] == "passed"
    assert report["cases"][1]["error"]["code"] == "MODEL_BALANCE_INSUFFICIENT"
    assert report["reported_model_calls"] == 1  # The failed attempt is not misreported as a result.
    assert FAKE_KEY not in json.dumps(report)


def test_suite_rejects_answer_when_expected_document_is_missing(check_app, monkeypatch):
    app, transport = check_app
    transport.side_effect = _answered_response
    cases = check_model._suite_cases()
    cases[0]["expected_slugs"] = ["csrf-token"]  # Deliberately wrong expectation for SQLITE_BUSY.
    monkeypatch.setattr(check_model, "_suite_cases", lambda: cases)
    report = check_model.check_suite(app)
    assert report["status"] == "failed" and transport.call_count == 1
    assert report["stopped_after_case"] == "K01"
    assert report["cases"][0]["error"]["code"] == "EXPECTED_SOURCE_MISSING"


def test_suite_rejects_unanswerable_case_that_wrongly_calls_provider(check_app, monkeypatch):
    from ticket_backend import assistant

    app, transport = check_app
    transport.side_effect = _answered_response
    search = assistant.search_knowledge
    unanswerable = next(case["question"] for case in check_model._suite_cases() if case["id"] == "N01")

    def wrong_retrieval(question, limit=3):
        # A false positive is deliberately introduced to prove this is an acceptance gate.
        return search("Nginx 502 upstream Connection refused" if question == unanswerable else question, limit)

    monkeypatch.setattr(assistant, "search_knowledge", wrong_retrieval)
    report = check_model.check_suite(app)
    assert report["status"] == "failed" and report["stopped_after_case"] == "N01"
    assert report["executed_cases"] == transport.call_count == 4
    assert report["cases"][-1]["error"]["code"] == "UNANSWERABLE_CASE_FAILED"
    assert report["cases"][-1]["checks"]["model_not_called"] is False


def test_suite_compares_excerpts_with_original_instead_of_trusting_a_valid_citation(check_app, monkeypatch):
    app, transport = check_app
    transport.side_effect = _answered_response
    original_view = app.view_functions["knowledge.get_document"]

    def changed_original(document_id):
        response = original_view(document_id)
        body = response.get_json()
        body["data"]["content"] = "A deliberately different original for this non-billable test."
        response.set_data(json.dumps(body))
        return response

    monkeypatch.setitem(app.view_functions, "knowledge.get_document", changed_original)
    report = check_model.check_suite(app)
    assert report["status"] == "failed" and transport.call_count == 1
    assert report["cases"][0]["error"]["code"] == "SOURCE_CHECK_FAILED"
    assert report["cases"][0]["source_checks"][0]["excerpt_matches"] is False


@pytest.mark.parametrize("check", [check_model.check_connection, check_model.check_suite])
def test_missing_key_fails_before_any_network_or_local_database_setup(check_app, monkeypatch, check):
    app, transport = check_app
    monkeypatch.delenv("DEEPSEEK_API_KEY")
    with pytest.raises(ApiError) as failure:
        check(app)
    assert failure.value.code == "MODEL_NOT_CONFIGURED"
    transport.assert_not_called()
    assert not Path(app.config["DATABASE"]).exists()
    assert not Path(app.config["MODEL_USAGE_DATABASE"]).exists()


@pytest.mark.parametrize("suite", [False, True])
def test_main_keeps_budget_and_report_while_cleaning_only_its_temporary_data(
    check_app, tmp_path, monkeypatch, capsys, suite,
):
    _, transport = check_app
    replica = tmp_path / "replica"
    ledger = tmp_path / "persistent-usage.sqlite3"
    configurations = []

    def isolated_factory(config):
        configurations.append(config)
        return create_app({**config, "TESTING": True, "MODEL_USAGE_DATABASE": str(ledger)})

    if suite:
        questions = (check_model.REPOSITORY / "examples" / "knowledge_questions.json").read_text(encoding="utf-8")
        (replica / "examples").mkdir(parents=True)
        (replica / "examples" / "knowledge_questions.json").write_text(questions, encoding="utf-8")
    monkeypatch.setattr(check_model, "REPOSITORY", replica)
    monkeypatch.setattr(check_model, "create_app", isolated_factory)
    transport.side_effect = _answered_response
    assert check_model.main(["--live", "--suite"] if suite else ["--live"]) == 0
    assert transport.call_count == (3 if suite else 1)
    assert configurations[0]["MODEL_ENABLED"] is True
    assert not configurations[0].get("TESTING", False)
    assert "MODEL_USAGE_DATABASE" not in configurations[0]  # Production uses its persistent default.
    assert not Path(configurations[0]["DATABASE"]).exists()
    assert ledger.exists()
    report_file, = (replica / "test-results").glob("live-model-*.json")
    encoded = report_file.read_text(encoding="utf-8")
    assert FAKE_KEY not in encoded
    assert "模拟建议" in encoded  # UTF-8 report remains readable without terminal encoding limits.
    report = json.loads(encoded)
    assert report["status"] == "passed" and report["temporary_data_removed"] is True
    terminal = capsys.readouterr().out
    assert terminal.isascii()
    assert json.loads(terminal) == report
