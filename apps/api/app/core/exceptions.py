"""Application error hierarchy and the single, consistent error envelope.

Every error response has the shape::

    {"error": {"code": "...", "message": "...", "request_id": "...", "details": {}}}

Internal exception text and stack traces are never returned to clients. Only
messages written deliberately for users (on ``AppError`` subclasses) are exposed.
"""

from __future__ import annotations

from typing import Any, ClassVar

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import get_logger, request_id_var

logger = get_logger(__name__)


class ErrorBody(BaseModel):
    code: str
    message: str
    request_id: str | None
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    error: ErrorBody


class AppError(Exception):
    """Base class for errors whose message is safe to show to API clients."""

    status_code: ClassVar[int] = 500
    code: ClassVar[str] = "INTERNAL_ERROR"
    default_message: ClassVar[str] = "An unexpected error occurred."

    def __init__(
        self, message: str | None = None, *, details: dict[str, Any] | None = None
    ) -> None:
        self.message = message or self.default_message
        self.details = details or {}
        super().__init__(self.message)


class BadRequestError(AppError):
    status_code = 400
    code = "BAD_REQUEST"
    default_message = "The request is invalid."


class AuthenticationError(AppError):
    status_code = 401
    code = "UNAUTHENTICATED"
    default_message = "Authentication is required."


class PermissionDeniedError(AppError):
    status_code = 403
    code = "PERMISSION_DENIED"
    default_message = "You do not have permission to perform this action."


class QueryNotAllowedError(AppError):
    status_code = 403
    code = "QUERY_NOT_ALLOWED"
    default_message = "The requested operation is not permitted."


class NotFoundError(AppError):
    status_code = 404
    code = "NOT_FOUND"
    default_message = "The requested resource was not found."


class ConflictError(AppError):
    status_code = 409
    code = "CONFLICT"
    default_message = "The request conflicts with the current state of the resource."


class RateLimitedError(AppError):
    status_code = 429
    code = "RATE_LIMITED"
    default_message = "Too many requests. Please retry later."


class UnprocessableError(AppError):
    status_code = 422
    code = "UNPROCESSABLE"
    default_message = "The request could not be processed."


class DependencyUnavailableError(AppError):
    status_code = 503
    code = "DEPENDENCY_UNAVAILABLE"
    default_message = "A required service is temporarily unavailable."


_HTTP_STATUS_CODES: dict[int, tuple[str, str]] = {
    400: ("BAD_REQUEST", "The request is invalid."),
    401: ("UNAUTHENTICATED", "Authentication is required."),
    403: ("PERMISSION_DENIED", "You do not have permission to perform this action."),
    404: ("NOT_FOUND", "The requested resource was not found."),
    405: ("METHOD_NOT_ALLOWED", "The HTTP method is not allowed for this resource."),
    406: ("NOT_ACCEPTABLE", "The requested representation is not available."),
    409: ("CONFLICT", "The request conflicts with the current state of the resource."),
    413: ("PAYLOAD_TOO_LARGE", "The request payload is too large."),
    415: ("UNSUPPORTED_MEDIA_TYPE", "The request media type is not supported."),
    429: ("RATE_LIMITED", "Too many requests. Please retry later."),
}


def error_response(
    status_code: int,
    code: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body = ErrorResponse(
        error=ErrorBody(
            code=code,
            message=message,
            request_id=request_id_var.get(),
            details=details or {},
        )
    )
    return JSONResponse(status_code=status_code, content=body.model_dump(), headers=headers)


def _sanitise_validation_errors(exc: RequestValidationError) -> list[dict[str, Any]]:
    """Return validation errors without echoing user input back.

    Pydantic includes the offending ``input`` and sometimes ``ctx`` values; these
    can contain secrets (e.g. a password field) or reflected payloads, so only the
    location, type and message are returned.
    """
    sanitised: list[dict[str, Any]] = []
    for err in exc.errors()[:50]:
        sanitised.append(
            {
                "loc": [str(part) for part in err.get("loc", ())],
                "type": str(err.get("type", "value_error")),
                "msg": str(err.get("msg", "Invalid value")),
            }
        )
    return sanitised


async def _handle_app_error(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AppError)  # noqa: S101 - registered for AppError only
    if exc.status_code >= 500:
        logger.error("application error", extra={"code": exc.code})
    else:
        logger.info("request rejected", extra={"code": exc.code, "status": exc.status_code})
    headers = None
    retry_after = exc.details.get("retry_after_seconds") if exc.status_code == 429 else None
    if isinstance(retry_after, int):
        headers = {"Retry-After": str(retry_after)}
    if exc.status_code == 401:
        headers = {"WWW-Authenticate": "Bearer"}
    return error_response(
        exc.status_code, exc.code, exc.message, details=exc.details, headers=headers
    )


async def _handle_validation_error(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)  # noqa: S101
    return error_response(
        422,
        "VALIDATION_ERROR",
        "The request failed validation.",
        details={"errors": _sanitise_validation_errors(exc)},
    )


async def _handle_http_exception(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)  # noqa: S101
    code, message = _HTTP_STATUS_CODES.get(
        exc.status_code,
        (
            "HTTP_ERROR" if exc.status_code < 500 else "INTERNAL_ERROR",
            "The request could not be completed.",
        ),
    )
    headers = dict(exc.headers) if exc.headers else None
    return error_response(exc.status_code, code, message, headers=headers)


async def _handle_unexpected(_: Request, exc: Exception) -> JSONResponse:
    # Log the type only via exc_info (redacted by the formatter); never return it.
    logger.error("unhandled exception", exc_info=exc)
    return error_response(500, "INTERNAL_ERROR", "An unexpected error occurred.")


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _handle_app_error)
    app.add_exception_handler(RequestValidationError, _handle_validation_error)
    app.add_exception_handler(StarletteHTTPException, _handle_http_exception)
    app.add_exception_handler(Exception, _handle_unexpected)
