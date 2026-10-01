"""Semantic index endpoints (``/api/v1/connections/{id}/semantic/...``)."""

from __future__ import annotations

import uuid
from dataclasses import asdict

from fastapi import APIRouter, Response

from app.dependencies import DbSessionDep, PrincipalDep, ResourcesDep
from app.schemas.semantic import (
    ResolveRequest,
    ResolveResponse,
    SearchHit,
    SearchRequest,
    SearchResponse,
    SemanticStatusOut,
    SemanticVersionOut,
)
from app.services.semantic_service import SemanticService, to_version_out

from .connections import connection_service

router = APIRouter(prefix="/connections", tags=["semantic"])


def _service(resources: ResourcesDep, session: DbSessionDep, ctx: PrincipalDep) -> SemanticService:
    return SemanticService(
        session=session,
        ctx=ctx,
        connections=connection_service(resources, session, ctx),
        audit=resources.audit,
        retriever=resources.retriever,
        registry=resources.semantic_registry,
        llm=resources.llm,
        top_k=resources.settings.rag_top_k,
    )


@router.post("/{connection_id}/semantic/ingest", response_model=SemanticVersionOut, status_code=202)
async def request_ingest(
    connection_id: uuid.UUID,
    response: Response,
    resources: ResourcesDep,
    session: DbSessionDep,
    ctx: PrincipalDep,
) -> SemanticVersionOut:
    row, created = await _service(resources, session, ctx).request_ingest(connection_id)
    if created:
        await resources.semantic_jobs.submit(row.id)
    response.headers["Location"] = f"/api/v1/connections/{connection_id}/semantic/status"
    return to_version_out(row)


@router.get("/{connection_id}/semantic/status", response_model=SemanticStatusOut)
async def semantic_status(
    connection_id: uuid.UUID, resources: ResourcesDep, session: DbSessionDep, ctx: PrincipalDep
) -> SemanticStatusOut:
    return await _service(resources, session, ctx).status(connection_id)


@router.post("/{connection_id}/semantic/search", response_model=SearchResponse)
async def semantic_search(
    connection_id: uuid.UUID,
    body: SearchRequest,
    resources: ResourcesDep,
    session: DbSessionDep,
    ctx: PrincipalDep,
) -> SearchResponse:
    version, docs = await _service(resources, session, ctx).search(
        connection_id, body.query, body.collections, body.top_k
    )
    return SearchResponse(version=version, hits=[SearchHit(**asdict(d)) for d in docs])


@router.post("/{connection_id}/semantic/resolve", response_model=ResolveResponse)
async def semantic_resolve(
    connection_id: uuid.UUID,
    body: ResolveRequest,
    resources: ResourcesDep,
    session: DbSessionDep,
    ctx: PrincipalDep,
) -> ResolveResponse:
    result = await _service(resources, session, ctx).resolve(connection_id, body.question)
    return ResolveResponse.model_validate(result)
