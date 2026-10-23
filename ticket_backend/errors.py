"""Consistent API errors without returning internal exception details."""

import sqlite3

from flask import current_app, g, jsonify, request
from werkzeug.exceptions import HTTPException

from .observability import logged_path


class ApiError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


HTTP_ERRORS = {
    400: ("INVALID_REQUEST", "请求格式或字段不合法。"),
    401: ("AUTH_REQUIRED", "请先登录。"),
    403: ("FORBIDDEN", "无权执行此操作。"),
    404: ("NOT_FOUND", "资源不存在。"),
    405: ("METHOD_NOT_ALLOWED", "此接口不支持该请求方法。"),
    413: ("REQUEST_TOO_LARGE", "请求内容过大。"),
    415: ("UNSUPPORTED_MEDIA_TYPE", "请使用 JSON 请求内容。"),
}


def error_response(status, code, message):
    return jsonify(error={"code": code, "message": message}), status


def handle_api_error(error):
    return error_response(error.status, error.code, error.message)


def handle_http_error(error):
    code, message = HTTP_ERRORS.get(error.code, ("HTTP_ERROR", "请求无法处理。"))
    response = error.get_response()
    response.data = current_app.json.dumps({"error": {"code": code, "message": message}})
    response.content_type = "application/json"
    return response


def handle_internal_error(error):
    connection = g.get("db")
    if connection is not None:
        try:
            connection.rollback()
        except sqlite3.Error:
            current_app.logger.exception("Database rollback failed request_id=%s", g.request_id)
    user = g.get("user")
    current_app.logger.error(
        "Request failed request_id=%s method=%s path=%r user_id=%s",
        g.request_id,
        request.method,
        logged_path(),
        user["id"] if user is not None else None,
        exc_info=(type(error), error, error.__traceback__),
    )
    return error_response(500, "INTERNAL_ERROR", "服务器处理失败，请稍后重试。")


def init_app(app):
    app.register_error_handler(ApiError, handle_api_error)
    app.register_error_handler(HTTPException, handle_http_error)
    app.register_error_handler(Exception, handle_internal_error)
