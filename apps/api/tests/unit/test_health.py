from __future__ import annotations

import httpx

from tests.conftest import AppFactory, FakeCheck, client_for


async def test_live_returns_ok_without_touching_dependencies(app_factory: AppFactory) -> None:
    # Every dependency is down, liveness must still succeed.
    app = app_factory([FakeCheck("postgres", fail=True), FakeCheck("redis", fail=True)])
    async with client_for(app) as client:
        response = await client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": "0.1.0"}


async def test_ready_when_all_required_checks_pass(client: httpx.AsyncClient) -> None:
    response = await client.get("/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert set(body["checks"]) == {"postgres", "redis", "qdrant"}
    assert all(c["status"] == "up" and c["required"] for c in body["checks"].values())


async def test_not_ready_when_required_dependency_down(app_factory: AppFactory) -> None:
    app = app_factory([FakeCheck("postgres", fail=True), FakeCheck("redis"), FakeCheck("qdrant")])
    async with client_for(app) as client:
        response = await client.get("/health/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["postgres"]["status"] == "down"
    assert body["checks"]["redis"]["status"] == "up"


async def test_optional_dependency_down_does_not_fail_readiness(app_factory: AppFactory) -> None:
    app = app_factory(
        [FakeCheck("postgres"), FakeCheck("redis"), FakeCheck("qdrant", fail=True)],
        required={"postgres", "redis"},
    )
    async with client_for(app) as client:
        response = await client.get("/health/ready")
    assert response.status_code == 200
    assert response.json()["checks"]["qdrant"] == {
        "status": "down",
        "latency_ms": response.json()["checks"]["qdrant"]["latency_ms"],
        "required": False,
    }


async def test_slow_check_times_out_and_is_reported_down(app_factory: AppFactory) -> None:
    # settings.readiness_check_timeout_seconds is 0.5 in tests
    app = app_factory([FakeCheck("postgres", delay=2.0), FakeCheck("redis"), FakeCheck("qdrant")])
    async with client_for(app) as client:
        response = await client.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["checks"]["postgres"]["status"] == "down"
    assert response.json()["checks"]["postgres"]["latency_ms"] < 1500


async def test_required_check_not_registered_is_not_ready(app_factory: AppFactory) -> None:
    app = app_factory([FakeCheck("redis")], required={"postgres", "redis"})
    async with client_for(app) as client:
        response = await client.get("/health/ready")
    assert response.status_code == 503


async def test_metrics_exposes_prometheus_format(client: httpx.AsyncClient) -> None:
    await client.get("/health/ready")
    response = await client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    text = response.text
    assert "erp_http_requests_total" in text
    assert 'route="/health/ready"' in text
    assert "erp_dependency_up" in text


async def test_metrics_uses_route_templates_not_raw_paths(client: httpx.AsyncClient) -> None:
    await client.get("/api/v1/does-not-exist/12345")
    text = (await client.get("/metrics")).text
    assert "12345" not in text
    assert 'route="unmatched"' in text
