from __future__ import annotations

import httpx
from fastapi import FastAPI
from pydantic import BaseModel, Field

from app.core.exceptions import QueryNotAllowedError
from tests.conftest import AppFactory, client_for


class Body(BaseModel):
    name: str = Field(min_length=1)
    limit: int = Field(le=1000)


def _app_with_test_routes(app_factory: AppFactory) -> FastAPI:
    app = app_factory()

    @app.post("/_test/validate")
    async def validate(body: Body) -> dict[str, str]:
        return {"name": body.name}

    @app.get("/_test/app-error")
    async def app_error() -> None:
        raise QueryNotAllowedError(details={"reason": "write_statement"})

    @app.get("/_test/crash")
    async def crash() -> None:
        raise RuntimeError("database password is hunter2 at postgresql://u:hunter2@db/x")

    return app


def _assert_envelope(body: dict[str, object], code: str) -> dict[str, object]:
    assert set(body) == {"error"}
    error = body["error"]
    assert isinstance(error, dict)
    assert set(error) == {"code", "message", "request_id", "details"}
    assert error["code"] == code
    assert isinstance(error["request_id"], str)
    assert error["request_id"]
    return error


async def test_not_found_uses_error_envelope(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/nope")
    assert response.status_code == 404
    error = _assert_envelope(response.json(), "NOT_FOUND")
    assert str(error["request_id"]).startswith("req_")
    assert error["request_id"] == response.headers["x-request-id"]


async def test_method_not_allowed_uses_error_envelope(client: httpx.AsyncClient) -> None:
    response = await client.post("/health/live")
    assert response.status_code == 405
    _assert_envelope(response.json(), "METHOD_NOT_ALLOWED")


async def test_app_error_returns_code_message_and_details(app_factory: AppFactory) -> None:
    async with client_for(_app_with_test_routes(app_factory)) as client:
        response = await client.get("/_test/app-error")
    assert response.status_code == 403
    error = _assert_envelope(response.json(), "QUERY_NOT_ALLOWED")
    assert error["message"] == "The requested operation is not permitted."
    assert error["details"] == {"reason": "write_statement"}


async def test_validation_error_is_sanitised(app_factory: AppFactory) -> None:
    async with client_for(_app_with_test_routes(app_factory)) as client:
        response = await client.post("/_test/validate", json={"name": "", "limit": 5000})
    assert response.status_code == 422
    error = _assert_envelope(response.json(), "VALIDATION_ERROR")
    details = error["details"]
    assert isinstance(details, dict)
    errors = details["errors"]
    assert {tuple(e["loc"]) for e in errors} == {("body", "name"), ("body", "limit")}
    for e in errors:
        assert set(e) == {"loc", "type", "msg"}


async def test_unhandled_exception_returns_generic_500_with_request_id(
    app_factory: AppFactory,
) -> None:
    async with client_for(_app_with_test_routes(app_factory)) as client:
        response = await client.get("/_test/crash", headers={"X-Request-ID": "trace_abc12345"})
    assert response.status_code == 500
    error = _assert_envelope(response.json(), "INTERNAL_ERROR")
    assert error["message"] == "An unexpected error occurred."
    assert error["details"] == {}
    assert error["request_id"] == "trace_abc12345"
    assert response.headers["x-request-id"] == "trace_abc12345"
    assert response.headers["x-content-type-options"] == "nosniff"
