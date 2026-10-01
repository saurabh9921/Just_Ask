"""Analytics endpoints (``/api/v1/analytics``).

* ``POST /query``              - ask a question; runs the governed agent synchronously
* ``POST /query/stream``       - same, as Server-Sent Events (progress, then result)
* ``POST /explain``            - plan, compile and validate only (no execution)
* ``POST /follow-up``          - refine an earlier question of the same user
* ``GET  /query/{id}``         - stored record + cached response (while it lives)
* ``POST /query/{id}/cancel``  - cancel a running query (cross-process)

All accept an optional ``Idempotency-Key`` header on POSTs that start a run.
Failed or cancelled runs return the standard error envelope with the failing
step's status code and ``details.query_id``.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse, Response, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.orchestrator import Emit
from app.agents.state import ProgressEvent
from app.core.exceptions import AppError, BadRequestError, error_response
from app.core.logging import get_logger
from app.core.tenant_context import TenantContext, bind_context
from app.db.models import QueryRecord
from app.dependencies import AppResources, DbSessionDep, PrincipalDep, ResourcesDep
from app.schemas.analytics import (
    CancelResponse,
    FollowUpRequest,
    QueryRecordOut,
    QueryRequest,
    QueryResponse,
    StreamRequest,
)
from app.services.analytics_service import (
    AnalyticsService,
    Mode,
    response_status,
    validate_idempotency_key,
)
from app.services.connection_service import ConnectionService

logger = get_logger(__name__)

router = APIRouter(prefix="/analytics", tags=["analytics"])

IdempotencyKey = Annotated[str | None, Header(alias="Idempotency-Key")]
SSE_HEARTBEAT_SECONDS = 15.0
_background: set[asyncio.Task[Any]] = set()


def analytics_service(
    resources: AppResources, session: AsyncSession, ctx: TenantContext
) -> AnalyticsService:
    connections = ConnectionService(
        session=session,
        settings=resources.settings,
        ctx=ctx,
        cipher=resources.cipher,
        adapters=resources.adapters,
        host_policy=resources.host_policy,
        audit=resources.audit,
    )
    return AnalyticsService(
        session=session,
        settings=resources.settings,
        ctx=ctx,
        connections=connections,
        registry=resources.semantic_registry,
        retriever=resources.retriever,
        llm=resources.llm,
        audit=resources.audit,
        redis=resources.redis,
        runs=resources.analytics_runs,
        rate_limiter=resources.rate_limiter,
        result_cache=resources.result_cache,
        catalog_cache=resources.catalog_cache,
    )


def _render(response: QueryResponse) -> Response:
    status = response_status(response)
    headers = {"X-Query-Id": str(response.query_id)}
    if response.replayed:
        headers["Idempotent-Replayed"] = "true"
    if status != 200:
        err = response.error
        return error_response(
            status,
            err.code if err else "QUERY_CANCELLED",
            err.message if err else "The query was cancelled.",
            details={**(err.details if err else {}), "query_id": str(response.query_id)},
            headers=headers,
        )
    return JSONResponse(response.model_dump(mode="json"), headers=headers)


async def _run(
    service: AnalyticsService,
    *,
    question: str,
    connection_id: uuid.UUID,
    idempotency_key: str | None,
    mode: Mode,
    previous_plan: dict[str, Any] | None = None,
    conversation_id: uuid.UUID | None = None,
    parent_query_id: uuid.UUID | None = None,
) -> Response:
    started = await service.start(
        question=question,
        connection_id=connection_id,
        idempotency_key=validate_idempotency_key(idempotency_key),
        conversation_id=conversation_id,
        parent_query_id=parent_query_id,
    )
    if isinstance(started, QueryResponse):
        return _render(started)
    return _render(await service.run(started, mode=mode, previous_plan=previous_plan))


@router.post("/query", response_model=QueryResponse)
async def query(
    body: QueryRequest,
    resources: ResourcesDep,
    session: DbSessionDep,
    ctx: PrincipalDep,
    idempotency_key: IdempotencyKey = None,
) -> Response:
    return await _run(
        analytics_service(resources, session, ctx),
        question=body.question,
        connection_id=body.connection_id,
        idempotency_key=idempotency_key,
        mode="execute",
        conversation_id=body.conversation_id,
    )


@router.post("/explain", response_model=QueryResponse)
async def explain(
    body: QueryRequest,
    resources: ResourcesDep,
    session: DbSessionDep,
    ctx: PrincipalDep,
    idempotency_key: IdempotencyKey = None,
) -> Response:
    """Plan, compile, validate and cost the question without running it."""
    return await _run(
        analytics_service(resources, session, ctx),
        question=body.question,
        connection_id=body.connection_id,
        idempotency_key=idempotency_key,
        mode="plan",
        conversation_id=body.conversation_id,
    )


@router.post("/follow-up", response_model=QueryResponse)
async def follow_up(
    body: FollowUpRequest,
    resources: ResourcesDep,
    session: DbSessionDep,
    ctx: PrincipalDep,
    idempotency_key: IdempotencyKey = None,
) -> Response:
    service = analytics_service(resources, session, ctx)
    previous_plan, parent_connection = await service.previous_plan(body.parent_query_id)
    return await _run(
        service,
        question=body.question,
        connection_id=body.connection_id or parent_connection,
        idempotency_key=idempotency_key,
        mode="execute",
        previous_plan=previous_plan,
        conversation_id=body.conversation_id,
        parent_query_id=body.parent_query_id,
    )


@router.get("/query/{query_id}", response_model=QueryRecordOut)
async def get_query(
    query_id: uuid.UUID, resources: ResourcesDep, session: DbSessionDep, ctx: PrincipalDep
) -> QueryRecordOut:
    return await analytics_service(resources, session, ctx).get(query_id)


@router.post("/query/{query_id}/cancel", response_model=CancelResponse)
async def cancel_query(
    query_id: uuid.UUID, resources: ResourcesDep, session: DbSessionDep, ctx: PrincipalDep
) -> CancelResponse:
    return await analytics_service(resources, session, ctx).cancel(query_id)


# ------------------------------------------------------------------ streaming
def _sse(event: str, data: str) -> bytes:
    lines = "".join(f"data: {line}\n" for line in data.splitlines() or [""])
    return f"event: {event}\n{lines}\n".encode()


async def start_run(
    service: AnalyticsService,
    *,
    question: str,
    connection_id: uuid.UUID | None,
    parent_query_id: uuid.UUID | None,
    idempotency_key: str | None,
    conversation_id: uuid.UUID | None = None,
) -> tuple[QueryRecord | QueryResponse, dict[str, Any] | None]:
    """Authorize and register a run (shared by SSE and WebSocket).

    Returns the new record - or a replayed response for a known ``Idempotency-Key`` -
    and the parent's plan for follow-ups.
    """
    previous_plan: dict[str, Any] | None = None
    if parent_query_id is not None:
        previous_plan, parent_connection = await service.previous_plan(parent_query_id)
        connection_id = connection_id or parent_connection
    if connection_id is None:
        raise BadRequestError("A connection or an earlier query is required.")
    started = await service.start(
        question=question,
        connection_id=connection_id,
        idempotency_key=validate_idempotency_key(idempotency_key),
        conversation_id=conversation_id,
        parent_query_id=parent_query_id,
    )
    return started, previous_plan


async def run_detached(
    resources: AppResources,
    ctx: TenantContext,
    query_id: uuid.UUID,
    *,
    mode: Mode,
    previous_plan: dict[str, Any] | None,
    emit: Emit | None,
) -> QueryResponse | AppError:
    """Run a registered query in its own DB session (outlives the starting request)."""
    try:
        with bind_context(ctx):
            async with resources.database.session_factory() as session:
                service = analytics_service(resources, session, ctx)
                record = await session.get(QueryRecord, query_id)
                if record is None or record.tenant_id != ctx.tenant_id:  # pragma: no cover
                    return AppError()
                return await service.run(record, mode=mode, previous_plan=previous_plan, emit=emit)
    except AppError as exc:
        return exc
    except Exception:
        logger.exception("detached analytics run crashed")
        return AppError()


async def _stream_run(
    resources: AppResources,
    ctx: TenantContext,
    query_id: uuid.UUID,
    previous_plan: dict[str, Any] | None,
    queue: asyncio.Queue[ProgressEvent | QueryResponse | AppError | None],
) -> None:
    try:
        await queue.put(
            await run_detached(
                resources,
                ctx,
                query_id,
                mode="execute",
                previous_plan=previous_plan,
                emit=queue.put,
            )
        )
    finally:
        await queue.put(None)


async def _events(
    queue: asyncio.Queue[ProgressEvent | QueryResponse | AppError | None],
    resources: AppResources,
    query_id: uuid.UUID,
) -> AsyncIterator[bytes]:
    yield _sse("accepted", json.dumps({"query_id": str(query_id)}))
    finished = False
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), SSE_HEARTBEAT_SECONDS)
            except TimeoutError:
                yield b": keep-alive\n\n"
                continue
            if item is None:
                finished = True
                return
            if isinstance(item, ProgressEvent):
                yield _sse("progress", item.model_dump_json())
            elif isinstance(item, QueryResponse):
                status = response_status(item)
                yield _sse("result" if status == 200 else "error", item.model_dump_json())
            else:
                body = {"code": item.code, "message": item.message, "query_id": str(query_id)}
                yield _sse("error", json.dumps(body))
    finally:
        if not finished:
            # Client went away: stop the run; it still records its audit event and history.
            run = resources.analytics_runs.get(query_id)
            if run is not None:
                run.cancel.set()


@router.post("/query/stream", response_class=StreamingResponse)
async def query_stream(
    body: StreamRequest,
    resources: ResourcesDep,
    session: DbSessionDep,
    ctx: PrincipalDep,
    idempotency_key: IdempotencyKey = None,
) -> Response:
    """Progress events (``event: progress``) followed by ``result`` or ``error``."""
    started, previous_plan = await start_run(
        analytics_service(resources, session, ctx),
        question=body.question,
        connection_id=body.connection_id,
        parent_query_id=body.parent_query_id,
        idempotency_key=idempotency_key,
        conversation_id=body.conversation_id,
    )
    queue: asyncio.Queue[ProgressEvent | QueryResponse | AppError | None] = asyncio.Queue()
    if isinstance(started, QueryResponse):
        await queue.put(started)
        await queue.put(None)
        task: asyncio.Task[None] = asyncio.create_task(asyncio.sleep(0))
        query_id = started.query_id
    else:
        query_id = started.id
        task = asyncio.create_task(_stream_run(resources, ctx, query_id, previous_plan, queue))
    _background.add(task)
    task.add_done_callback(_background.discard)
    return StreamingResponse(
        _events(queue, resources, query_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
            "X-Query-Id": str(query_id),
        },
    )
