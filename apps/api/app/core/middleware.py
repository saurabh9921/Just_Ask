"""Pure-ASGI middleware: correlation IDs, security headers, access logs, metrics.

Implemented as raw ASGI (not ``BaseHTTPMiddleware``) so streaming responses and
WebSockets pass through without buffering.
"""

from __future__ import annotations

import re
import time
import uuid
from typing import Final

from opentelemetry import propagate, trace
from opentelemetry.trace import SpanKind, Status, StatusCode
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.exceptions import error_response
from app.core.logging import get_logger, request_id_var
from app.core.metrics import HTTP_REQUEST_DURATION, HTTP_REQUESTS_IN_PROGRESS, HTTP_REQUESTS_TOTAL

logger = get_logger("app.access")
_tracer = trace.get_tracer("app.http")

REQUEST_ID_HEADER: Final = "x-request-id"
# Accept a caller-supplied ID only if it is short and strictly alphanumeric-ish,
# preventing log injection (newlines, JSON fragments) and header smuggling.
_SAFE_REQUEST_ID: Final = re.compile(r"^[A-Za-z0-9_\-]{8,64}$")

SECURITY_HEADERS: Final[tuple[tuple[str, str], ...]] = (
    ("x-content-type-options", "nosniff"),
    ("x-frame-options", "DENY"),
    ("referrer-policy", "no-referrer"),
    ("cross-origin-opener-policy", "same-origin"),
    ("cross-origin-resource-policy", "same-site"),
    ("permissions-policy", "camera=(), microphone=(), geolocation=(), payment=()"),
    ("content-security-policy", "default-src 'none'; frame-ancestors 'none'"),
    ("cache-control", "no-store"),
)


def new_request_id() -> str:
    return f"req_{uuid.uuid4().hex}"


def resolve_request_id(candidate: str | None) -> str:
    if candidate and _SAFE_REQUEST_ID.fullmatch(candidate):
        return candidate
    return new_request_id()


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", []):
        if key == name:
            try:
                return str(value.decode("latin-1"))
            except UnicodeDecodeError:
                return None
    return None


def _carrier(scope: Scope) -> dict[str, str]:
    return {k.decode("latin-1"): v.decode("latin-1") for k, v in scope.get("headers", [])}


def _route_template(scope: Scope) -> str:
    route = scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) else "unmatched"


class RequestContextMiddleware:
    """Assigns a request ID, adds security headers, logs and measures requests."""

    def __init__(self, app: ASGIApp, *, hsts: bool = False) -> None:
        self.app = app
        self._hsts = hsts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return

        request_id = resolve_request_id(_header(scope, REQUEST_ID_HEADER.encode()))
        token = request_id_var.set(request_id)
        scope.setdefault("state", {})["request_id"] = request_id
        try:
            if scope["type"] == "websocket":
                await self.app(scope, receive, send)
            else:
                await self._handle_http(scope, receive, send, request_id)
        finally:
            request_id_var.reset(token)

    def _decorate_headers(self, message: Message, request_id: str) -> None:
        headers = MutableHeaders(scope=message)
        headers[REQUEST_ID_HEADER] = request_id
        for name, value in SECURITY_HEADERS:
            headers.setdefault(name, value)
        if self._hsts:
            headers.setdefault("strict-transport-security", "max-age=31536000; includeSubDomains")

    async def _handle_http(
        self, scope: Scope, receive: Receive, send: Send, request_id: str
    ) -> None:
        method: str = scope["method"]
        state = {"status": 500, "started": False}
        started = time.perf_counter()
        HTTP_REQUESTS_IN_PROGRESS.labels(method=method).inc()

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                state["started"] = True
                state["status"] = int(message["status"])
                self._decorate_headers(message, request_id)
            await send(message)

        span = _tracer.start_span(
            f"HTTP {method}",
            kind=SpanKind.SERVER,
            context=propagate.extract(_carrier(scope)),
            attributes={"http.request.method": method, "erp.request_id": request_id},
        )
        try:
            with trace.use_span(span, end_on_exit=False):
                await self.app(scope, receive, send_wrapper)
        except Exception as exc:
            # Handled here (not by Starlette's outer ServerErrorMiddleware) so the
            # error envelope still carries the request ID and security headers.
            logger.exception("unhandled exception")
            span.record_exception(exc)
            if state["started"]:
                raise
            response = error_response(500, "INTERNAL_ERROR", "An unexpected error occurred.")
            await response(scope, receive, send_wrapper)
        finally:
            HTTP_REQUESTS_IN_PROGRESS.labels(method=method).dec()
            _record(scope, span, method, int(state["status"]), time.perf_counter() - started)


def _record(scope: Scope, span: trace.Span, method: str, status: int, elapsed: float) -> None:
    route = _route_template(scope)
    span.update_name(f"{method} {route}")
    span.set_attribute("http.route", route)
    span.set_attribute("http.response.status_code", status)
    if status >= 500:
        span.set_status(Status(StatusCode.ERROR))
    span.end()
    HTTP_REQUESTS_TOTAL.labels(method=method, route=route, status=str(status)).inc()
    HTTP_REQUEST_DURATION.labels(method=method, route=route).observe(elapsed)
    # Path template only - query strings may carry identifiers and are never logged.
    if route not in {"/health/live", "/metrics"}:
        logger.info(
            "request completed",
            extra={
                "method": method,
                "route": route,
                "status": status,
                "duration_ms": round(elapsed * 1000, 2),
            },
        )
