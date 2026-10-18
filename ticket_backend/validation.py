"""Strict JSON field validation shared by the small set of endpoints."""

from flask import request

from .errors import ApiError


def read_json_object(expected_fields):
    if not request.is_json:
        raise ApiError(415, "UNSUPPORTED_MEDIA_TYPE", "请使用 JSON 请求内容。")
    try:
        payload = request.get_json()
    except RecursionError as error:
        raise ApiError(400, "INVALID_REQUEST", "JSON 嵌套层级过深。") from error
    if not isinstance(payload, dict) or set(payload) != set(expected_fields):
        raise ApiError(400, "INVALID_REQUEST", "请求字段缺失、多余或内容不是 JSON 对象。")
    return payload


def is_utf8_text(value):
    if not isinstance(value, str) or "\x00" in value:
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def required_text(value, field, maximum):
    if not is_utf8_text(value) or not 1 <= len(value.strip()) <= maximum:
        raise ApiError(400, "INVALID_REQUEST", f"{field} 必须是去除首尾空白后长度为 1～{maximum} 的字符串。")
    return value.strip()
