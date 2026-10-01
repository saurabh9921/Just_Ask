"""Liveness, readiness and metrics endpoints (unversioned, operator-facing).

* ``/health/live``  - process is running; never touches dependencies.
* ``/health/ready`` - required dependencies reachable; 503 otherwise.
* ``/metrics``      - Prometheus exposition. nginx blocks this path from the public
  listener; Prometheus scrapes the API container directly on the internal network.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Response
from pydantic import BaseModel

from app.core.exceptions import NotFoundError
from app.core.metrics import render_latest
from app.dependencies import HealthServiceDep, SettingsDep
from app.services.health_service import CheckStatus

router = APIRouter(tags=["health"])


class LiveResponse(BaseModel):
    status: Literal["ok"] = "ok"
    version: str


class DependencyStatus(BaseModel):
    status: CheckStatus
    latency_ms: float
    required: bool


class ReadyResponse(BaseModel):
    status: Literal["ready", "not_ready"]
    checks: dict[str, DependencyStatus]


@router.get("/health/live", response_model=LiveResponse)
async def live(settings: SettingsDep) -> LiveResponse:
    return LiveResponse(version=settings.app_version)


@router.get(
    "/health/ready",
    response_model=ReadyResponse,
    responses={
        503: {"model": ReadyResponse, "description": "One or more required dependencies are down"}
    },
)
async def ready(health: HealthServiceDep, response: Response) -> ReadyResponse:
    report = await health.readiness()
    if not report.ready:
        response.status_code = 503
    return ReadyResponse(
        status="ready" if report.ready else "not_ready",
        checks={
            r.name: DependencyStatus(status=r.status, latency_ms=r.latency_ms, required=r.required)
            for r in report.checks
        },
    )


@router.get("/metrics", include_in_schema=False)
async def metrics(settings: SettingsDep) -> Response:
    if not settings.metrics_enabled:
        raise NotFoundError()
    payload, content_type = render_latest()
    return Response(content=payload, media_type=content_type)
