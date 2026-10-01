"""Tenant administration endpoints (``/api/v1/admin``, requires ``admin.manage``)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query

from app.dependencies import DbSessionDep, PrincipalDep, ResourcesDep
from app.schemas.admin import UserCreate, UserList, UserOut, UserUpdate
from app.services.admin_service import AdminService

router = APIRouter(prefix="/admin", tags=["admin"])


def _service(resources: ResourcesDep, session: DbSessionDep, ctx: PrincipalDep) -> AdminService:
    return AdminService(
        session=session,
        settings=resources.settings,
        ctx=ctx,
        passwords=resources.passwords,
        audit=resources.audit,
    )


@router.get("/users", response_model=UserList)
async def list_users(
    resources: ResourcesDep,
    session: DbSessionDep,
    ctx: PrincipalDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> UserList:
    items, total = await _service(resources, session, ctx).list_users(limit=limit, offset=offset)
    return UserList(items=items, total=total)


@router.post("/users", response_model=UserOut, status_code=201)
async def create_user(
    body: UserCreate, resources: ResourcesDep, session: DbSessionDep, ctx: PrincipalDep
) -> UserOut:
    return await _service(resources, session, ctx).create_user(body)


@router.patch("/users/{user_id}", response_model=UserOut)
async def update_user(
    user_id: uuid.UUID,
    body: UserUpdate,
    resources: ResourcesDep,
    session: DbSessionDep,
    ctx: PrincipalDep,
) -> UserOut:
    return await _service(resources, session, ctx).update_user(user_id, body)
