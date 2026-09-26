"""One error shape for the whole API: {"detail": {"code": ..., "message": ...}}.

`code` is stable and machine-readable (the frontends branch on it);
`message` is written for the person looking at the screen.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException


class ApiError(HTTPException):
    def __init__(self, status_code: int, code: str, message: str, **extra: Any) -> None:
        detail: dict[str, Any] = {"code": code, "message": message, **extra}
        super().__init__(status_code=status_code, detail=detail)
        self.code = code


def not_found(what: str = "Not found") -> ApiError:
    # Cross-tenant access deliberately looks exactly like a missing row.
    return ApiError(404, "not_found", what)


def bad_request(code: str, message: str, **extra: Any) -> ApiError:
    return ApiError(400, code, message, **extra)


def conflict(code: str, message: str, **extra: Any) -> ApiError:
    return ApiError(409, code, message, **extra)


def forbidden(message: str = "You don't have permission to do that.", code: str = "forbidden") -> ApiError:
    return ApiError(403, code, message)


def unauthorized(message: str = "Please sign in again.", code: str = "unauthorized") -> ApiError:
    return ApiError(401, code, message)


def too_many(message: str, retry_after: int) -> ApiError:
    err = ApiError(429, "rate_limited", message, retry_after=retry_after)
    err.headers = {"Retry-After": str(retry_after)}
    return err
