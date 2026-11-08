"""
对外错误脱敏。
细节只进服务端日志。
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

log = logging.getLogger("cityudg")

_PUBLIC = {
    400: "请求格式不正确。",
    401: "认证失败。",
    403: "没有权限。",
    404: "接口不存在。",
    413: "请求体过大。",
    429: "请求过于频繁，请稍后再试。",
    500: "服务内部错误。",
    502: "服务暂时不可用，请稍后重试。",
    503: "服务暂时不可用，请稍后重试。",
    504: "请求超时，请稍后重试。",
}

SAFE_STATUSES = (400, 401, 403, 404, 413, 429)


def public_message(status: int) -> str:
    return _PUBLIC.get(status) or _PUBLIC[502]


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def record(request_id: str, status: int, exc: Any, **extra: Any) -> None:
    detail = f"{type(exc).__name__}: {exc}" if exc is not None else "-"
    if extra:
        detail += " | " + " ".join(f"{k}={v!r}" for k, v in extra.items())
    log.warning("[%s] status=%s %s", request_id, status, detail)


def normalize_status(status: int) -> int:
    if status in SAFE_STATUSES:
        return status
    return 502
