"""Explicit model checks through the real ticket-answer route, without retries."""

import argparse
import json
from pathlib import Path
import secrets
import sys
import tempfile
from uuid import uuid4


REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY))

from scripts.demo_workflow import DemoFailure, require, verify_cli_setup  # noqa: E402
from ticket_backend import create_app  # noqa: E402
from ticket_backend.errors import ApiError  # noqa: E402
from ticket_backend.model_service import _api_key  # noqa: E402


SUITE_CASE_IDS = ("K01", "K02", "K03", "N01", "N02", "N04")
DRIVER = "Flask test client to local application; provider HTTP enabled"
SCOPE = "Response and original-source checks only; answer meaning requires human review."


def _prepare_check(app):
    with app.app_context():
        _api_key()  # Preflight only; never print or return the key.
    password = secrets.token_urlsafe(24)
    verify_cli_setup(app, app.config["DATABASE"], password)

    def login(username):
        client = app.test_client()
        initial = client.get("/auth/csrf").get_json()["data"]["csrf_token"]
        response = client.post(
            "/auth/login", json={"username": username, "password": password},
            headers={"X-CSRF-Token": initial},
        )
        require(response.status_code == 200, "Local demo login failed.")
        return client, response.get_json()["data"]["csrf_token"]

    owner, owner_token = login("submitter_a")
    created = owner.post(
        "/tickets", json={"title": "模拟 Nginx 502", "description": "模拟上游 Connection refused。"},
        headers={"X-CSRF-Token": owner_token},
    )
    require(created.status_code == 201, "Local simulated ticket creation failed.")
    handler, token = login("handler")
    return handler, token, created.get_json()["data"]["id"]


def _source_checks(handler, sources):
    checks = []
    for source in sources:
        response = handler.get(f"/knowledge/documents/{source['document_id']}")
        body = response.get_json()
        original = body.get("data") if isinstance(body, dict) else None
        available = response.status_code == 200 and isinstance(original, dict)
        excerpt = ("\n".join(original["content"].splitlines()[source["start_line"] - 1:source["end_line"]])
                   if available else None)
        checks.append({
            "document_id": source["document_id"], "slug": source["slug"],
            "start_line": source["start_line"], "end_line": source["end_line"],
            "original_available": available,
            "checksum_matches": available and source["checksum"] == original["checksum"],
            "slug_matches": available and source["slug"] == original["slug"],
            "excerpt_matches": available and source["excerpt"] == excerpt,
        })
    return checks


def _check_case(handler, token, ticket_id, question, expected_slugs=None):
    # Exactly one invocation; failures and refusals are never automatically retried.
    response = handler.post(
        f"/tickets/{ticket_id}/answer",
        json={"question": question},
        headers={"X-CSRF-Token": token},
    )
    report = {
        "status": "failed", "http_status": response.status_code,
        "request_id": response.headers.get("X-Request-ID"),
        "driver": DRIVER,
        "maximum_provider_attempts": 1,
        "question": question,
        "semantic_review": "pending manual review",
        "scope": SCOPE,
    }
    body = response.get_json()
    if response.status_code != 200:
        report["error"] = body["error"]
        return report
    result = body["data"]
    report["result"] = result
    report["source_checks"] = _source_checks(handler, result["sources"])
    checked = all(
        check["original_available"] and check["checksum_matches"]
        and check["slug_matches"] and check["excerpt_matches"]
        for check in report["source_checks"]
    )
    if not checked:
        report["error"] = {"code": "SOURCE_CHECK_FAILED", "message": "Returned sources did not match original documents."}
        return report
    if expected_slugs == []:
        report["checks"] = {
            "insufficient_evidence": result["status"] == "insufficient_evidence",
            "model_not_called": result.get("model_called") is False,
            "empty_answer": result.get("answer") is None,
            "empty_sources": result["sources"] == [],
        }
        if all(report["checks"].values()):
            report["status"] = "passed"
        else:
            report["error"] = {"code": "UNANSWERABLE_CASE_FAILED", "message": "An unanswerable case must return no answer or sources without calling the provider."}
        return report
    if result.get("model_called") is not True or result["status"] != "answered":
        report["status"] = "needs_review"
        return report
    if expected_slugs is not None:
        actual_slugs = {source["slug"] for source in result["sources"]}
        report["checks"] = {"expected_sources_present": set(expected_slugs) <= actual_slugs}
        if not report["checks"]["expected_sources_present"]:
            report["error"] = {"code": "EXPECTED_SOURCE_MISSING", "message": "The answer did not cite all expected source documents."}
            return report
    report["status"] = "passed"
    return report


def check_connection(app):
    handler, token, ticket_id = _prepare_check(app)
    return _check_case(handler, token, ticket_id, "Nginx 502 upstream Connection refused")


def _suite_cases():
    examples = json.loads((REPOSITORY / "examples" / "knowledge_questions.json").read_text(encoding="utf-8"))
    by_id = {case["id"]: case for case in examples}
    require(len(by_id) == len(examples), "Development case ids must be unique.")
    require(all(case_id in by_id for case_id in SUITE_CASE_IDS), "A fixed development case is missing.")
    return [by_id[case_id] for case_id in SUITE_CASE_IDS]


def check_suite(app):
    cases = _suite_cases()
    handler, token, ticket_id = _prepare_check(app)
    report = {
        "status": "passed", "mode": "suite", "driver": DRIVER,
        "dataset": "Six fixed development cases from examples/knowledge_questions.json; not a held-out accuracy evaluation.",
        "planned_case_ids": list(SUITE_CASE_IDS),
        "maximum_provider_attempts": len(cases),
        "expected_provider_calls": sum(bool(case["expected_slugs"]) for case in cases),
        "semantic_review": "pending manual review", "scope": SCOPE,
        "cases": [], "stopped_early": False,
    }
    for case in cases:
        result = _check_case(handler, token, ticket_id, case["question"], case["expected_slugs"])
        result.update(case_id=case["id"], expected_slugs=case["expected_slugs"])
        report["cases"].append(result)
        if result["status"] != "passed":
            report["status"] = result["status"]
            report["stopped_early"] = len(report["cases"]) < len(cases)
            report["stopped_after_case"] = case["id"]
            break
    report["executed_cases"] = len(report["cases"])
    report["reported_model_calls"] = sum(
        case.get("result", {}).get("model_called") is True for case in report["cases"]
    )
    report["call_count_scope"] = "reported_model_calls counts successful API responses marked model_called; failed requests may also consume a provider attempt and budget."
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Allow billable model requests within the existing CNY 10 budget.")
    parser.add_argument("--suite", action="store_true", help="Run six fixed development cases; at most six provider attempts, normally three. Requires --live.")
    arguments = parser.parse_args(argv)
    if not arguments.live:
        parser.error("Pass --live explicitly to allow billable model requests.")
    base = (REPOSITORY / "tests" / ".pytest-tmp").resolve()
    require(base.is_relative_to(REPOSITORY), "Temporary data must remain inside the repository.")
    base.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="live-model-", dir=base, ignore_cleanup_errors=True) as name:
        directory = Path(name).resolve()
        require(directory.parent == base, "Unexpected temporary directory.")
        app = create_app({
            "DATABASE": str(directory / "tickets.sqlite3"),
            "SECRET_KEY": secrets.token_hex(32),
            "MODEL_ENABLED": True,
            "MODEL_BUDGET_CNY": "10",
        })
        # The default persistent usage ledger is intentionally retained. Only
        # this newly created business database is temporary, never the budget.
        try:
            report = check_suite(app) if arguments.suite else check_connection(app)
        except ApiError as error:
            report = {"status": "failed", "error": {"code": error.code, "message": error.message}}
        except DemoFailure as error:
            report = {"status": "failed", "error": {"code": "LOCAL_CHECK_FAILED", "message": str(error)}}
        except Exception as error:
            report = {"status": "failed", "error": {"code": "LOCAL_CHECK_FAILED", "type": type(error).__name__}}
    report["temporary_data_removed"] = not directory.exists()
    report_path = REPOSITORY / "test-results" / f"live-model-{uuid4().hex}.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report["report_file"] = str(report_path)
    encoded = json.dumps(report, ensure_ascii=False, indent=2)
    report_path.write_text(encoded + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
