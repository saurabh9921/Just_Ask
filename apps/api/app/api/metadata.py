"""Metadata discovery endpoints (``/api/v1/connections/{id}/...``)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Path, Query, Response

from app.dependencies import DbSessionDep, PrincipalDep, ResourcesDep
from app.schemas.metadata import ConnectionSyncStatus, EntityDetail, EntityList, SyncStatusOut
from app.services.metadata_service import MetadataService, to_status

from .connections import connection_service

router = APIRouter(prefix="/connections", tags=["metadata"])


def _service(resources: ResourcesDep, session: DbSessionDep, ctx: PrincipalDep) -> MetadataService:
    return MetadataService(
        session=session,
        ctx=ctx,
        connections=connection_service(resources, session, ctx),
        audit=resources.audit,
    )


@router.post("/{connection_id}/metadata/sync", response_model=SyncStatusOut, status_code=202)
async def request_sync(
    connection_id: uuid.UUID,
    response: Response,
    resources: ResourcesDep,
    session: DbSessionDep,
    ctx: PrincipalDep,
) -> SyncStatusOut:
    snapshot, created = await _service(resources, session, ctx).request_sync(connection_id)
    if created:
        await resources.metadata_jobs.submit(snapshot.id)
    response.headers["Location"] = f"/api/v1/connections/{connection_id}/sync-status"
    return to_status(snapshot)


@router.get("/{connection_id}/sync-status", response_model=ConnectionSyncStatus)
async def sync_status(
    connection_id: uuid.UUID, resources: ResourcesDep, session: DbSessionDep, ctx: PrincipalDep
) -> ConnectionSyncStatus:
    return await _service(resources, session, ctx).sync_status(connection_id)


@router.get("/{connection_id}/entities", response_model=EntityList)
async def list_entities(
    connection_id: uuid.UUID,
    resources: ResourcesDep,
    session: DbSessionDep,
    ctx: PrincipalDep,
    search: Annotated[str | None, Query(max_length=100)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
) -> EntityList:
    return await _service(resources, session, ctx).list_entities(
        connection_id, search=search, limit=limit, offset=offset
    )


@router.get("/{connection_id}/entities/{entity_name}", response_model=EntityDetail)
async def get_entity(
    connection_id: uuid.UUID,
    entity_name: Annotated[str, Path(min_length=1, max_length=511)],
    resources: ResourcesDep,
    session: DbSessionDep,
    ctx: PrincipalDep,
) -> EntityDetail:
    return await _service(resources, session, ctx).get_entity(connection_id, entity_name)
