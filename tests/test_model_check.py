"""The one-call helper is tested with a non-billable transport only."""

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


def test_live_flag_is_required_before_preparing_data_or_calling_provider(monkeypatch):
    factory = Mock(side_effect=AssertionError("App must not be created without opt-in."))
    monkeypatch.setattr(check_model, "create_app", factory)
    with pytest.raises(SystemExit) as stopped:
        check_model.main([])
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
    assert report["error"]["code"] == "MODEL_UPSTREAM_ERROR"
    assert FAKE_KEY not in json.dumps(report)


def test_missing_key_fails_before_any_network_or_local_database_setup(check_app, monkeypatch):
    app, transport = check_app
    monkeypatch.delenv("DEEPSEEK_API_KEY")
    with pytest.raises(ApiError) as failure:
        check_model.check_connection(app)
    assert failure.value.code == "MODEL_NOT_CONFIGURED"
    transport.assert_not_called()
    assert not Path(app.config["DATABASE"]).exists()
    assert not Path(app.config["MODEL_USAGE_DATABASE"]).exists()


def test_main_keeps_budget_and_report_while_cleaning_only_its_temporary_data(
    check_app, tmp_path, monkeypatch,
):
    _, transport = check_app
    replica = tmp_path / "replica"
    ledger = tmp_path / "persistent-usage.sqlite3"
    configurations = []

    def isolated_factory(config):
        configurations.append(config)
        return create_app({**config, "TESTING": True, "MODEL_USAGE_DATABASE": str(ledger)})

    def respond(request, timeout):
        context = json.loads(json.loads(request.data)["messages"][1]["content"])
        content = {"answer": "模拟建议核对上游监听端口。", "insufficient_evidence": False,
                   "citation_ids": [context["sources"][0]["chunk_id"]]}
        return BytesIO(json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(content)}}],
                                  "usage": {"prompt_tokens": 123, "completion_tokens": 40}}).encode())

    monkeypatch.setattr(check_model, "REPOSITORY", replica)
    monkeypatch.setattr(check_model, "create_app", isolated_factory)
    transport.side_effect = respond
    assert check_model.main(["--live"]) == 0
    assert transport.call_count == 1
    assert configurations[0]["MODEL_ENABLED"] is True
    assert not configurations[0].get("TESTING", False)
    assert "MODEL_USAGE_DATABASE" not in configurations[0]  # Production uses its persistent default.
    assert not Path(configurations[0]["DATABASE"]).exists()
    assert ledger.exists()
    report_file, = (replica / "test-results").glob("live-model-*.json")
    encoded = report_file.read_text(encoding="utf-8")
    assert FAKE_KEY not in encoded
    report = json.loads(encoded)
    assert report["status"] == "passed" and report["temporary_data_removed"] is True
