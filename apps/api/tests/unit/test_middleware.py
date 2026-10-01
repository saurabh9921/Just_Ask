from __future__ import annotations

import httpx

from app.core.middleware import SECURITY_HEADERS, new_request_id, resolve_request_id
from tests.conftest import TEST_EXTENSION_ORIGIN


def test_new_request_ids_are_unique_and_prefixed() -> None:
    ids = {new_request_id() for _ in range(1000)}
    assert len(ids) == 1000
    assert all(i.startswith("req_") and len(i) == 36 for i in ids)


def test_resolve_request_id_accepts_safe_ids() -> None:
    assert resolve_request_id("abc-DEF_123456") == "abc-DEF_123456"


def test_resolve_request_id_replaces_missing_or_unsafe_ids() -> None:
    for bad in (None, "", "short", "x" * 65, "abc 12345678", 'abc"}{12345', "abc\n12345678"):
        resolved = resolve_request_id(bad)
        assert resolved.startswith("req_"), bad


async def test_response_carries_generated_request_id(client: httpx.AsyncClient) -> None:
    response = await client.get("/health/live")
    assert response.headers["x-request-id"].startswith("req_")


async def test_response_echoes_safe_incoming_request_id(client: httpx.AsyncClient) -> None:
    response = await client.get("/health/live", headers={"X-Request-ID": "ext_0123456789"})
    assert response.headers["x-request-id"] == "ext_0123456789"


async def test_security_headers_present(client: httpx.AsyncClient) -> None:
    response = await client.get("/health/live")
    for name, value in SECURITY_HEADERS:
        assert response.headers[name] == value
    # HSTS only in production (TLS terminates at nginx/ingress)
    assert "strict-transport-security" not in response.headers


async def test_cors_allows_configured_extension_origin(client: httpx.AsyncClient) -> None:
    response = await client.options(
        "/health/live",
        headers={"Origin": TEST_EXTENSION_ORIGIN, "Access-Control-Request-Method": "GET"},
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == TEST_EXTENSION_ORIGIN
    assert "access-control-allow-credentials" not in response.headers
