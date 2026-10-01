"""Background exports (``/api/v1/exports``)."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Query, Response
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenant_context import TenantContext
from app.dependencies import AppResources, DbSessionDep, PrincipalDep, ResourcesDep
from app.schemas.exports import DownloadTokenOut, ExportCreate, ExportOut, ExportPage
from app.services.export_service import (
    ExportService,
    mark_downloaded,
    ready_file,
    redeem_download_token,
)

router = APIRouter(prefix="/exports", tags=["exports"])

MEDIA_TYPES = {"csv": "text/csv; charset=utf-8", "json": "application/json"}


def export_service(
    resources: AppResources, session: AsyncSession, ctx: TenantContext
) -> ExportService:
    return ExportService(
        session=session,
        settings=resources.settings,
        ctx=ctx,
        audit=resources.audit,
        redis=resources.redis,
        jobs=resources.export_jobs,
    )


def _file_response(path: Path, file_name: str | None, fmt: str) -> FileResponse:
    return FileResponse(
        path,
        media_type=MEDIA_TYPES.get(fmt, "application/octet-stream"),
        filename=file_name or f"export.{fmt}",
        headers={"Cache-Control": "no-store"},
    )


@router.post("", response_model=ExportOut, status_code=202)
async def create_export(
    body: ExportCreate, resources: ResourcesDep, session: DbSessionDep, ctx: PrincipalDep
) -> ExportOut:
    """Queue an export of a succeeded query (``analytics.export``; ``confirm`` required)."""
    return await export_service(resources, session, ctx).create(body)


@router.get("", response_model=ExportPage)
async def list_exports(
    resources: ResourcesDep,
    session: DbSessionDep,
    ctx: PrincipalDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> ExportPage:
    return await export_service(resources, session, ctx).list(limit)


@router.get("/download/{token}")
async def download_with_token(
    token: str, resources: ResourcesDep, session: DbSessionDep
) -> Response:
    """Redeem a one-time download token (no bearer header: browsers' download managers
    cannot send one). The token expires quickly and is deleted on first use."""
    job = await redeem_download_token(session, resources.redis, token)
    path = ready_file(resources.settings, job)
    await mark_downloaded(session, resources.audit, job, via="token")
    return _file_response(path, job.file_name, job.format)


@router.get("/{export_id}", response_model=ExportOut)
async def get_export(
    export_id: uuid.UUID, resources: ResourcesDep, session: DbSessionDep, ctx: PrincipalDep
) -> ExportOut:
    return await export_service(resources, session, ctx).get(export_id)


@router.get("/{export_id}/file")
async def download_export(
    export_id: uuid.UUID, resources: ResourcesDep, session: DbSessionDep, ctx: PrincipalDep
) -> Response:
    """Stream the file with bearer authentication (API clients)."""
    job = await export_service(resources, session, ctx).owned(export_id)
    path = ready_file(resources.settings, job)
    await mark_downloaded(session, resources.audit, job, via="api")
    return _file_response(path, job.file_name, job.format)


@router.post("/{export_id}/download-token", response_model=DownloadTokenOut)
async def create_download_token(
    export_id: uuid.UUID, resources: ResourcesDep, session: DbSessionDep, ctx: PrincipalDep
) -> DownloadTokenOut:
    return await export_service(resources, session, ctx).download_token(export_id)


@router.delete("/{export_id}", status_code=204)
async def delete_export(
    export_id: uuid.UUID, resources: ResourcesDep, session: DbSessionDep, ctx: PrincipalDep
) -> Response:
    await export_service(resources, session, ctx).delete(export_id)
    return Response(status_code=204)
