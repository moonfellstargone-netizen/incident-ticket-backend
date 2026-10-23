"""One explicitly requested model call through the real ticket-answer route."""

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


def check_connection(app):
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
    ticket_id = created.get_json()["data"]["id"]
    # Exactly one invocation; failures and refusals are never automatically retried.
    response = handler.post(
        f"/tickets/{ticket_id}/answer",
        json={"question": "Nginx 502 upstream Connection refused"},
        headers={"X-CSRF-Token": token},
    )
    report = {
        "status": "failed", "http_status": response.status_code,
        "request_id": response.headers.get("X-Request-ID"),
        "driver": "Flask test client to local application; provider HTTP enabled",
        "maximum_provider_attempts": 1,
    }
    body = response.get_json()
    if response.status_code != 200:
        report["error"] = body["error"]
        return report
    result = body["data"]
    report["result"] = result
    if result.get("model_called") is not True or result["status"] != "answered":
        report["status"] = "needs_review"
        return report
    for source in result["sources"]:
        original = handler.get(f"/knowledge/documents/{source['document_id']}").get_json()["data"]
        excerpt = "\n".join(original["content"].splitlines()[source["start_line"] - 1:source["end_line"]])
        require(source["checksum"] == original["checksum"] and source["excerpt"] == excerpt,
                "Returned source could not be checked against the original.")
    report["status"] = "passed"
    report["scope"] = "One response and source check; answer meaning still requires human review."
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Allow one billable call within the existing CNY 10 budget.")
    arguments = parser.parse_args(argv)
    if not arguments.live:
        parser.error("Pass --live explicitly to allow one billable model request.")
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
            report = check_connection(app)
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
    print(encoded)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
