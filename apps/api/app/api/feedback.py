"""Answer feedback (``/api/v1/feedback``)."""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import BadRequestError
from app.core.tenant_context import TenantContext
from app.dependencies import AppResources, DbSessionDep, PrincipalDep, ResourcesDep
from app.schemas.feedback import FeedbackCreate, FeedbackOut, FeedbackPage
from app.services.feedback_service import FeedbackService

router = APIRouter(prefix="/feedback", tags=["feedback"])


def feedback_service(
    resources: AppResources, session: AsyncSession, ctx: TenantContext
) -> FeedbackService:
    return FeedbackService(
        session=session,
        settings=resources.settings,
        ctx=ctx,
        audit=resources.audit,
        registry=resources.semantic_registry,
        ingestion=resources.ingestion,
        retriever=resources.retriever,
    )


@router.post("", response_model=FeedbackOut, status_code=201)
async def submit_feedback(
    body: FeedbackCreate, resources: ResourcesDep, session: DbSessionDep, ctx: PrincipalDep
) -> FeedbackOut:
    """Rate an answer (thumbs up/down, categories, optional comment). Re-rating updates it."""
    return await feedback_service(resources, session, ctx).submit(body)


@router.get("", response_model=FeedbackPage)
async def list_feedback(
    resources: ResourcesDep,
    session: DbSessionDep,
    ctx: PrincipalDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
    rating: Annotated[int | None, Query(ge=-1, le=1, description="-1 or 1")] = None,
    query_id: uuid.UUID | None = None,
    scope: Literal["mine", "tenant"] = "mine",
) -> FeedbackPage:
    """Own feedback; ``scope=tenant`` (``admin.manage``) lists the whole tenant's."""
    if rating == 0:
        raise BadRequestError("rating must be -1 or 1.")
    return await feedback_service(resources, session, ctx).list(
        limit=limit, offset=offset, rating=rating, query_id=query_id, scope=scope
    )
