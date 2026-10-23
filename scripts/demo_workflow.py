"""Verify the simulated ticket workflow over real, temporary loopback HTTP.

Run from the repository root with: python scripts/demo_workflow.py
No development database, external service, or additional dependency is used.
"""

from contextlib import closing, contextmanager
from http.cookiejar import CookieJar
from importlib.metadata import version
import json
from pathlib import Path
import platform
import secrets
import sqlite3
import sys
import tempfile
from threading import Thread
from urllib.error import HTTPError, URLError
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener


REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY))

from ticket_backend import create_app  # noqa: E402
from werkzeug.serving import WSGIRequestHandler, make_server  # noqa: E402


TICKET_FIELDS = {
    "id", "title", "description", "creator_id", "status", "resolution",
    "resolved_by", "created_at",
}
LIST_FIELDS = {"items", "total", "limit", "offset"}


class DemoFailure(Exception):
    """An acceptance check failed; messages must never contain credentials."""


def require(condition, message):
    # Keep acceptance checks enabled when the interpreter is run with -O.
    if not condition:
        raise DemoFailure(message)


class QuietRequestHandler(WSGIRequestHandler):
    def log_request(self, code="-", size="-"):
        # Report checks as JSON instead of one access-log line per request.
        pass


@contextmanager
def local_server(app):
    server = make_server("127.0.0.1", 0, app, request_handler=QuietRequestHandler)
    worker = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    started = False
    try:
        worker.start()
        started = True
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        if started:
            server.shutdown()
        server.server_close()
        if started:
            worker.join(timeout=5)
            require(not worker.is_alive(), "Temporary HTTP server did not stop.")


class Browser:
    """One independent HTTP cookie jar per simulated user."""

    def __init__(self, base_url, request_counter):
        self.base_url = base_url
        self.opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(CookieJar()))
        self.csrf_token = None
        self.request_counter = request_counter
        self.request_ids = set()

    def request(self, method, path, payload=None, *, expected=200, csrf=True, error=None):
        headers = {"Accept": "application/json"}
        encoded = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            encoded = json.dumps(payload).encode("utf-8")
        if csrf and method in {"POST", "PATCH"} and self.csrf_token:
            headers["X-CSRF-Token"] = self.csrf_token
        request = Request(self.base_url + path, data=encoded, headers=headers, method=method)
        self.request_counter[0] += 1
        try:
            response = self.opener.open(request, timeout=10)
        except HTTPError as caught:
            response = caught
        except URLError:
            raise DemoFailure(f"Loopback HTTP transport failed: {method} {path}.") from None
        with response:
            status = response.status
            content_type = response.headers.get_content_type()
            request_id = response.headers.get("X-Request-ID", "")
            cache_control = response.headers.get("Cache-Control", "")
            content = response.read()
        require(status == expected, f"{method} {path}: expected HTTP {expected}, received {status}.")
        require(content_type == "application/json", f"{method} {path}: response was not JSON.")
        require(len(request_id) == 32 and all(char in "0123456789abcdef" for char in request_id),
                f"{method} {path}: missing server-generated request id.")
        require(request_id not in self.request_ids, f"{method} {path}: request id was reused.")
        self.request_ids.add(request_id)
        require(cache_control == "no-store", f"{method} {path}: response allowed caching.")
        try:
            body = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise DemoFailure(f"{method} {path}: response contained invalid JSON.") from None
        require(isinstance(body, dict), f"{method} {path}: expected a JSON object.")
        if error is not None:
            details = body.get("error")
            require(set(body) == {"error"}, f"{method} {path}: unexpected error envelope.")
            require(isinstance(details, dict), f"{method} {path}: missing error details.")
            require(set(details) == {"code", "message"}, f"{method} {path}: unexpected error fields.")
            require(details["code"] == error, f"{method} {path}: unexpected error code.")
            require(isinstance(details["message"], str) and details["message"],
                    f"{method} {path}: error message was empty.")
            return details
        require(set(body) == {"data"}, f"{method} {path}: unexpected success envelope.")
        return body["data"]

    def login(self, username, password):
        initial = self.request("GET", "/auth/csrf")
        token = initial.get("csrf_token") if isinstance(initial, dict) else None
        require(isinstance(token, str) and bool(token), "Missing pre-login CSRF token.")
        self.csrf_token = token
        logged_in = self.request("POST", "/auth/login", {"username": username, "password": password})
        require(isinstance(logged_in, dict), "Unexpected login data.")
        require(set(logged_in) == {"user", "csrf_token"}, "Unexpected login fields.")
        user = logged_in["user"]
        require(isinstance(user, dict) and set(user) == {"id", "username", "role"},
                "Unexpected public user fields.")
        require(user["username"] == username, "Login returned another username.")
        require(type(user["id"]) is int and user["id"] > 0, "Login returned an invalid user id.")
        new_token = logged_in["csrf_token"]
        require(isinstance(new_token, str) and new_token and new_token != token,
                "Login did not replace the CSRF token.")
        self.csrf_token = new_token
        return user


def ticket_snapshot(database):
    # sqlite3 connection context managers manage transactions, not connection closure.
    with closing(sqlite3.connect(database)) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT id, title, description, creator_id, status, resolution, resolved_by, "
            "created_at FROM tickets ORDER BY id"
        ).fetchall()
        return [dict(row) for row in rows]


def verify_ticket(ticket):
    require(isinstance(ticket, dict) and set(ticket) == TICKET_FIELDS,
            "A ticket response did not contain the agreed eight public fields.")
    require(type(ticket["id"]) is int and ticket["id"] > 0, "Invalid ticket id.")
    require(isinstance(ticket["created_at"], str) and bool(ticket["created_at"]),
            "Missing server-generated creation time.")
    return ticket


def verify_list(data, expected_tickets):
    require(isinstance(data, dict) and set(data) == LIST_FIELDS, "Unexpected list fields.")
    require(data["total"] == len(expected_tickets), "Unexpected visible ticket count.")
    require(data["limit"] == 20 and data["offset"] == 0, "Unexpected default pagination.")
    require(data["items"] == expected_tickets, "Unexpected visible ticket contents.")
    for ticket in data["items"]:
        verify_ticket(ticket)


def verify_cli_setup(app, database, password):
    runner = app.test_cli_runner()
    initialized = runner.invoke(args=["init-db"])
    require(initialized.exit_code == 0, "The init-db CLI command failed.")
    # Exercise the real password prompt without putting the password in command arguments.
    seeded = runner.invoke(args=["seed-demo"], input=f"{password}\n{password}\n")
    require(seeded.exit_code == 0, "The seed-demo CLI command failed.")
    with closing(sqlite3.connect(database)) as connection:
        users = connection.execute("SELECT username, role FROM users ORDER BY username").fetchall()
    require(users == [("handler", "handler"), ("submitter_a", "submitter"),
                      ("submitter_b", "submitter")], "CLI setup did not create the three demo accounts.")
    require(ticket_snapshot(database) == [], "The isolated database was not initially empty.")


def run_workflow(database):
    app_config = {
        "DATABASE": str(database),
        "SECRET_KEY": secrets.token_hex(32),
        "TESTING": False,
    }
    app = create_app(app_config)
    password = secrets.token_urlsafe(24)
    verify_cli_setup(app, database, password)
    checks = ["real_init_db_and_seed_demo_cli"]
    request_counter = [0]
    title = "[SIMULATED] Internal API timeout"
    description = "[SIMULATED] A manually reported timeout; no real user or production data."
    resolution = "[SIMULATED] Corrected the example timeout configuration and verified the request."

    with local_server(app) as base_url:
        submitter_a = Browser(base_url, request_counter)
        user_a = submitter_a.login("submitter_a", password)
        require(user_a["role"] == "submitter", "Unexpected submitter role.")
        initial = verify_ticket(submitter_a.request(
            "POST", "/tickets", {"title": f"  {title}  ", "description": description}, expected=201
        ))
        require(initial["title"] == title and initial["description"] == description,
                "Created ticket text did not match validated input.")
        require(initial["creator_id"] == user_a["id"] and initial["status"] == "pending",
                "Created ticket identity or initial state was incorrect.")
        require(initial["resolution"] is None and initial["resolved_by"] is None,
                "A new ticket already contained resolution data.")
        require(ticket_snapshot(database) == [initial], "Creation was not persisted correctly.")
        path = f"/tickets/{initial['id']}"
        status_path = path + "/status"
        verify_list(submitter_a.request("GET", "/tickets"), [initial])
        require(submitter_a.request("GET", path) == initial, "Submitter detail differed from creation.")
        checks.append("submitter_creation_list_detail_and_persistence")

        submitter_b = Browser(base_url, request_counter)
        user_b = submitter_b.login("submitter_b", password)
        require(user_b["id"] != user_a["id"], "Submitters did not have distinct identities.")
        verify_list(submitter_b.request("GET", "/tickets"), [])
        submitter_b.request("GET", path, expected=404, error="NOT_FOUND")
        submitter_b.request("PATCH", status_path, {"status": "in_progress"},
                            expected=403, error="FORBIDDEN")
        require(ticket_snapshot(database) == [initial], "Denied user access changed database contents.")
        checks.append("cross_user_visibility_and_role_rejection_without_mutation")

        handler = Browser(base_url, request_counter)
        handler_user = handler.login("handler", password)
        require(handler_user["role"] == "handler", "Unexpected handler role.")
        verify_list(handler.request("GET", "/tickets"), [initial])
        require(handler.request("GET", path) == initial, "Handler could not view the ticket.")
        checks.append("handler_sees_submitted_ticket")

        handler.request("PATCH", status_path, {"status": "in_progress"}, csrf=False,
                        expected=403, error="CSRF_FAILED")
        require(ticket_snapshot(database) == [initial], "Missing CSRF rejection changed database contents.")
        checks.append("missing_csrf_rejected_without_mutation")

        handler.request("PATCH", status_path, {"status": "resolved", "resolution": resolution},
                        expected=409, error="INVALID_TRANSITION")
        require(ticket_snapshot(database) == [initial], "Skipped-state rejection changed database contents.")
        checks.append("pending_to_resolved_rejected_without_mutation")

        in_progress = dict(initial, status="in_progress")
        require(verify_ticket(handler.request("PATCH", status_path, {"status": "in_progress"}))
                == in_progress, "The pending to in_progress response was incorrect.")
        require(ticket_snapshot(database) == [in_progress], "The in_progress state was not persisted.")
        checks.append("pending_to_in_progress")

        handler.request("PATCH", status_path, {"status": "resolved", "resolution": " \t\n "},
                        expected=400, error="INVALID_REQUEST")
        require(ticket_snapshot(database) == [in_progress], "Empty resolution rejection changed database contents.")
        checks.append("blank_resolution_rejected_without_mutation")

        final = dict(in_progress, status="resolved", resolution=resolution, resolved_by=handler_user["id"])
        require(verify_ticket(handler.request(
            "PATCH", status_path, {"status": "resolved", "resolution": f"  {resolution}  "}
        )) == final, "Resolution, status, or handler identity was incorrect.")
        require(ticket_snapshot(database) == [final], "Resolution was not atomically persisted.")
        checks.append("in_progress_to_resolved_with_trimmed_result_and_handler")

        for target, payload in (
            ("resolved", {"status": "resolved", "resolution": "[SIMULATED] Must not replace result."}),
            ("in_progress", {"status": "in_progress"}),
            ("pending", {"status": "pending"}),
        ):
            handler.request("PATCH", status_path, payload, expected=409, error="INVALID_TRANSITION")
            require(ticket_snapshot(database) == [final], f"Rejected transition to {target} changed database contents.")
        checks.append("repeat_and_backward_transitions_rejected_without_mutation")

        require(submitter_a.request("GET", path) == final, "Submitter could not read the final result.")
        verify_list(submitter_a.request("GET", "/tickets?status=resolved"), [final])
        verify_list(handler.request("GET", "/tickets?status=pending"), [])
        checks.append("submitter_reads_final_result_and_filtered_lists")
        require(submitter_a.request("POST", "/auth/logout", {}) == {"logged_out": True},
                "Logout response was incorrect.")
        submitter_a.csrf_token = None
        submitter_a.request("GET", "/tickets", expected=401, error="AUTH_REQUIRED")
        submitter_a.request("GET", path, expected=401, error="AUTH_REQUIRED")
        checks.append("logout_revokes_client_access")

    # Both the original server and every request context are closed before reopening.
    require(ticket_snapshot(database) == [final], "Data was lost after the first server stopped.")
    restarted_app = create_app(dict(app_config, SECRET_KEY=secrets.token_hex(32)))
    with local_server(restarted_app) as restarted_url:
        restarted_client = Browser(restarted_url, request_counter)
        restarted_user = restarted_client.login("submitter_a", password)
        require(restarted_user["id"] == user_a["id"], "The persisted account identity changed.")
        require(verify_ticket(restarted_client.request("GET", path)) == final,
                "The recreated app could not read the persisted final result.")
        verify_list(restarted_client.request("GET", "/tickets"), [final])
    require(ticket_snapshot(database) == [final], "The restart check changed the final result.")
    checks.append("fresh_app_and_server_reopen_existing_sqlite_without_reinitialization")
    return {
        "checks": checks,
        "http_requests": request_counter[0],
        "response_headers": {"request_id": "verified per response", "cache_control": "no-store"},
    }


def main():
    base = (REPOSITORY / "tests" / ".pytest-tmp").resolve()
    require(base.is_relative_to(REPOSITORY) and base != REPOSITORY,
            "Temporary directory must remain inside the repository.")
    base.mkdir(parents=True, exist_ok=True)
    # Cleanup applies only to this newly created random directory, never an existing run.
    with tempfile.TemporaryDirectory(prefix="http-demo-", dir=base, ignore_cleanup_errors=True) as name:
        temporary = Path(name).resolve()
        require(temporary.parent == base, "Unexpected temporary directory location.")
        database = (temporary / "tickets.sqlite3").resolve()
        require(database.parent == temporary, "Unexpected temporary database location.")
        results = run_workflow(database)
    cleanup = {"removed": not temporary.exists()}
    if temporary.exists():
        cleanup["retained_ignored_directory"] = temporary.relative_to(REPOSITORY).as_posix()
    print(json.dumps({
        "status": "passed",
        "data": "simulated accounts and incidents only",
        "transport": "real loopback HTTP; temporary servers stopped",
        **results,
        "temporary_data": cleanup,
        "environment": {
            "python": platform.python_version(),
            "os": platform.platform(),
            "flask": version("Flask"),
            "werkzeug": version("Werkzeug"),
            "sqlite": sqlite3.sqlite_version,
        },
        "scope": "functional verification; not a public deployment or performance test",
    }, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    try:
        main()
    except DemoFailure as error:
        print(json.dumps({"status": "failed", "reason": str(error)}), file=sys.stderr)
        raise SystemExit(1)
    except Exception as error:
        # Do not dump response bodies, CLI input/output, local secrets, or cookies.
        print(json.dumps({"status": "failed", "reason": "Unexpected demo error", "type": type(error).__name__}),
              file=sys.stderr)
        raise SystemExit(1)
