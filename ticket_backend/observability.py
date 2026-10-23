"""Small, request-local identifiers and metadata-only completion logs."""

from time import perf_counter
from uuid import uuid4

from flask import current_app, g, request


def logged_path():
    """Bound untrusted path input; callers use %r to escape control characters."""
    return request.path[:256]


def start_request():
    # Generate locally rather than accepting a caller-controlled log identifier.
    g.request_id = uuid4().hex
    g.request_started_at = perf_counter()


def finish_request(response):
    response.headers["X-Request-ID"] = g.request_id
    response.headers["Cache-Control"] = "no-store"
    user = g.get("user")
    current_app.logger.info(
        "Request completed request_id=%s method=%s path=%r status=%s duration_ms=%.3f user_id=%s",
        g.request_id,
        request.method,
        logged_path(),
        response.status_code,
        (perf_counter() - g.request_started_at) * 1000,
        user["id"] if user is not None else None,
    )
    return response


def init_app(app):
    # Register before authentication so failures loading the user also have an id.
    app.before_request(start_request)
    app.after_request(finish_request)
