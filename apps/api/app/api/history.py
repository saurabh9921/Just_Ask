"""Query history (``/api/v1/history``): the signed-in user's own questions."""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.feedback import feedback_service
from app.core.tenant_context import TenantContext
from app.dependencies import AppResources, DbSessionDep, PrincipalDep, ResourcesDep
from app.schemas.history import (
    ConversationDetail,
    ConversationItem,
    ConversationPage,
    HistoryDetail,
    HistoryItem,
    HistoryPage,
    HistoryUpdate,
)
from app.services.history_service import HistoryService

router = APIRouter(prefix="/history", tags=["history"])

Status = Literal[
    "pending", "running", "succeeded", "planned", "clarification", "refused", "failed", "cancelled"
]


def history_service(
    resources: AppResources, session: AsyncSession, ctx: TenantContext
) -> HistoryService:
    return HistoryService(
        session=session,
        settings=resources.settings,
        ctx=ctx,
        redis=resources.redis,
        audit=resources.audit,
        feedback=feedback_service(resources, session, ctx),
    )


@router.get("", response_model=HistoryPage)
async def list_history(
    resources: ResourcesDep,
    session: DbSessionDep,
    ctx: PrincipalDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 20,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
    saved: bool | None = None,
    status: Status | None = None,
    connection_id: uuid.UUID | None = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
) -> HistoryPage:
    return await history_service(resources, session, ctx).list(
        limit=min(limit, resources.settings.history_page_max),
        offset=offset,
        saved=saved,
        status=status,
        connection_id=connection_id,
        search=q,
    )


# Conversations (chats) - declared before "/{query_id}" so the path is not read as an id.
@router.get("/conversations", response_model=ConversationPage)
async def list_conversations(
    resources: ResourcesDep,
    session: DbSessionDep,
    ctx: PrincipalDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 20,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
    saved: bool | None = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
) -> ConversationPage:
    """The user's chats, most recently active first (search matches any question)."""
    return await history_service(resources, session, ctx).list_conversations(
        limit=min(limit, resources.settings.history_page_max),
        offset=offset,
        saved=saved,
        search=q,
    )


@router.get("/conversations/{conversation_id}", response_model=ConversationDetail)
async def get_conversation(
    conversation_id: uuid.UUID, resources: ResourcesDep, session: DbSessionDep, ctx: PrincipalDep
) -> ConversationDetail:
    return await history_service(resources, session, ctx).get_conversation(conversation_id)


@router.patch("/conversations/{conversation_id}", response_model=ConversationItem)
async def update_conversation(
    conversation_id: uuid.UUID,
    body: HistoryUpdate,
    resources: ResourcesDep,
    session: DbSessionDep,
    ctx: PrincipalDep,
) -> ConversationItem:
    """Save (bookmark) or unsave a whole chat."""
    return await history_service(resources, session, ctx).set_conversation_saved(
        conversation_id, body.saved
    )


@router.delete("/conversations/{conversation_id}", status_code=204)
async def delete_conversation(
    conversation_id: uuid.UUID, resources: ResourcesDep, session: DbSessionDep, ctx: PrincipalDep
) -> Response:
    await history_service(resources, session, ctx).delete_conversation(conversation_id)
    return Response(status_code=204)


@router.get("/{query_id}", response_model=HistoryDetail)
async def get_history_item(
    query_id: uuid.UUID, resources: ResourcesDep, session: DbSessionDep, ctx: PrincipalDep
) -> HistoryDetail:
    return await history_service(resources, session, ctx).get(query_id)


@router.patch("/{query_id}", response_model=HistoryItem)
async def update_history_item(
    query_id: uuid.UUID,
    body: HistoryUpdate,
    resources: ResourcesDep,
    session: DbSessionDep,
    ctx: PrincipalDep,
) -> HistoryItem:
    """Save (bookmark) or unsave a question."""
    return await history_service(resources, session, ctx).set_saved(query_id, body.saved)


@router.delete("/{query_id}", status_code=204)
async def delete_history_item(
    query_id: uuid.UUID, resources: ResourcesDep, session: DbSessionDep, ctx: PrincipalDep
) -> Response:
    await history_service(resources, session, ctx).delete(query_id)
    return Response(status_code=204)
