"""Session authentication and CSRF protection for the local ticket API."""

from functools import wraps
import secrets

from flask import Blueprint, g, jsonify, request, session
from werkzeug.security import check_password_hash

from .db import get_db
from .errors import ApiError
from .validation import is_utf8_text, read_json_object, required_text


bp = Blueprint("auth", __name__, url_prefix="/auth")

_CSRF_TOKEN_BYTES = 32
_CSRF_TOKEN_LENGTH = 43
_CSRF_ALPHABET = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
)
_MAX_USER_ID = (1 << 63) - 1


@bp.before_app_request
def load_logged_in_user():
    """Resolve the current user from the database, including their current role."""
    g.user = None
    if "user_id" not in session:
        # Anonymous sessions still need to retain their CSRF token for login.
        return

    user_id = session["user_id"]
    # bool is an int subclass; oversized integers cannot be bound by SQLite.
    if type(user_id) is not int or not 1 <= user_id <= _MAX_USER_ID:
        session.clear()
        return

    user = get_db().execute(
        "SELECT id, username, role FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    if user is None:
        session.clear()
        return

    g.user = user


def require_login(role=None):
    """Require a logged-in user and, optionally, a database-backed role."""

    def decorate(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            user = getattr(g, "user", None)
            if user is None:
                raise ApiError(401, "AUTH_REQUIRED", "请先登录。")
            if role is not None and user["role"] != role:
                raise ApiError(403, "FORBIDDEN", "当前账号没有执行此操作的权限。")
            return view(*args, **kwargs)

        return wrapped

    return decorate


def _has_valid_token_format(value):
    # Validate both values before compare_digest, which rejects non-ASCII str.
    return (
        isinstance(value, str)
        and len(value) == _CSRF_TOKEN_LENGTH
        and value.isascii()
        and all(character in _CSRF_ALPHABET for character in value)
    )


def _get_or_create_csrf_token():
    token = session.get("csrf_token")
    if not _has_valid_token_format(token):
        token = secrets.token_urlsafe(_CSRF_TOKEN_BYTES)
        session["csrf_token"] = token
    return token


def csrf_required(view):
    """Require the session's current CSRF token in X-CSRF-Token."""

    @wraps(view)
    def wrapped(*args, **kwargs):
        expected = session.get("csrf_token")
        supplied = request.headers.get("X-CSRF-Token")
        if (
            not _has_valid_token_format(expected)
            or not _has_valid_token_format(supplied)
            or not secrets.compare_digest(expected, supplied)
        ):
            raise ApiError(403, "CSRF_FAILED", "CSRF 校验失败，请重新获取令牌。")
        return view(*args, **kwargs)

    return wrapped


@bp.get("/csrf")
def get_csrf_token():
    response = jsonify(data={"csrf_token": _get_or_create_csrf_token()})
    response.headers["Cache-Control"] = "no-store"
    return response


@bp.post("/login")
@csrf_required
def login():
    body = read_json_object({"username", "password"})
    username = required_text(body["username"], "username", 64)
    password = body["password"]

    if not is_utf8_text(password) or not 1 <= len(password) <= 256:
        raise ApiError(400, "INVALID_REQUEST", "密码须为 1 至 256 个字符的字符串。")

    user = get_db().execute(
        "SELECT id, username, password_hash, role FROM users WHERE username = ?",
        (username,),
    ).fetchone()
    if user is None or not check_password_hash(user["password_hash"], password):
        raise ApiError(401, "INVALID_CREDENTIALS", "账号或密码错误。")

    # Keep only the user id as identity data and replace the pre-login token.
    session.clear()
    session["user_id"] = user["id"]
    session.permanent = True
    response = jsonify(
        data={
            "user": {
                "id": user["id"],
                "username": user["username"],
                "role": user["role"],
            },
            "csrf_token": _get_or_create_csrf_token(),
        }
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@bp.post("/logout")
@require_login()
@csrf_required
def logout():
    session.clear()
    return jsonify(data={"logged_out": True})
