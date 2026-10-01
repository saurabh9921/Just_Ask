"""Connection CRUD and connection tests (``/api/v1/connections``)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Response

from app.dependencies import DbSessionDep, PrincipalDep, ResourcesDep
from app.schemas.connections import (
    ConnectionCreate,
    ConnectionList,
    ConnectionOut,
    ConnectionTestOut,
    ConnectionUpdate,
)
from app.services.connection_service import ConnectionService

router = APIRouter(prefix="/connections", tags=["connections"])


def connection_service(
    resources: ResourcesDep, session: DbSessionDep, ctx: PrincipalDep
) -> ConnectionService:
    return ConnectionService(
        session=session,
        settings=resources.settings,
        ctx=ctx,
        cipher=resources.cipher,
        adapters=resources.adapters,
        host_policy=resources.host_policy,
        audit=resources.audit,
    )


async def _out(service: ConnectionService, conn_id: uuid.UUID) -> ConnectionOut:
    conn = await service.get(conn_id)
    roles = await service.granted_roles([conn.id])
    return ConnectionService.to_out(conn, roles[conn.id])


@router.post("", response_model=ConnectionOut, status_code=201)
async def create_connection(
    body: ConnectionCreate, resources: ResourcesDep, session: DbSessionDep, ctx: PrincipalDep
) -> ConnectionOut:
    service = connection_service(resources, session, ctx)
    conn = await service.create(body)
    return await _out(service, conn.id)


@router.get("", response_model=ConnectionList)
async def list_connections(
    resources: ResourcesDep, session: DbSessionDep, ctx: PrincipalDep
) -> ConnectionList:
    service = connection_service(resources, session, ctx)
    conns = await service.list_visible()
    roles = await service.granted_roles([c.id for c in conns])
    return ConnectionList(
        items=[ConnectionService.to_out(c, roles[c.id]) for c in conns], total=len(conns)
    )


@router.get("/{connection_id}", response_model=ConnectionOut)
async def get_connection(
    connection_id: uuid.UUID, resources: ResourcesDep, session: DbSessionDep, ctx: PrincipalDep
) -> ConnectionOut:
    return await _out(connection_service(resources, session, ctx), connection_id)


@router.post("/{connection_id}/test", response_model=ConnectionTestOut)
async def test_connection(
    connection_id: uuid.UUID, resources: ResourcesDep, session: DbSessionDep, ctx: PrincipalDep
) -> ConnectionTestOut:
    result = await connection_service(resources, session, ctx).test(connection_id)
    return ConnectionTestOut.model_validate(result.model_dump())


@router.patch("/{connection_id}", response_model=ConnectionOut)
async def update_connection(
    connection_id: uuid.UUID,
    body: ConnectionUpdate,
    resources: ResourcesDep,
    session: DbSessionDep,
    ctx: PrincipalDep,
) -> ConnectionOut:
    service = connection_service(resources, session, ctx)
    conn = await service.update(connection_id, body)
    return await _out(service, conn.id)


@router.delete("/{connection_id}", status_code=204, response_class=Response)
async def delete_connection(
    connection_id: uuid.UUID, resources: ResourcesDep, session: DbSessionDep, ctx: PrincipalDep
) -> Response:
    await connection_service(resources, session, ctx).delete(connection_id)
    return Response(status_code=204)
