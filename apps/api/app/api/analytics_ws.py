"""Analytics over WebSocket (``/api/v1/analytics/ws``): multiplexed progress streaming.

One socket per extension background worker carries any number of questions
(bounded by ``WS_MAX_CONCURRENT_QUERIES``), each identified by a client-chosen
``client_ref``. JSON text frames only.

Client -> server::

    {"type": "auth", "access_token": "..."}                  first frame, within WS_AUTH_TIMEOUT
    {"type": "query", "client_ref": "r1", "question": "...", "connection_id": "..." |
     "parent_query_id": "...", "conversation_id": "...", "mode": "execute"|"plan",
     "idempotency_key": "..."}
    {"type": "cancel", "query_id": "..."}
    {"type": "ping"}

Server -> client::

    {"type": "ready", "user_id", "tenant", "heartbeat_seconds"}
    {"type": "accepted", "client_ref", "query_id"}
    {"type": "progress", "client_ref", "query_id", "event": ProgressEvent}
    {"type": "result", "client_ref", "query_id", "response": QueryResponse}
    {"type": "error", "client_ref"?, "query_id"?, "error": {code, message, status, details},
     "response"?: QueryResponse}
    {"type": "cancelled", "query_id", "status", "cancel_requested"}
    {"type": "ping"} (heartbeat) / {"type": "pong"}

Security:

* The token travels in the first frame, never in the URL (URLs end up in logs).
* ``Origin`` - when present - must be an allowed extension origin (the browser does
  not apply CORS to WebSockets; this blocks cross-site WebSocket hijacking).
* The token is re-validated (expiry) and the user/tenant/session state re-loaded
  from the database before every query and cancel, exactly like an HTTP request;
  an expired token yields ``TOKEN_EXPIRED`` and the client re-authenticates on the
  same socket with a refreshed token.
* Frames are size-limited and schema-validated (unknown fields rejected); invalid
  frames are answered with an error that never echoes the input.
* Closing the socket cancels its running queries (they still write history/audit).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import Field, StringConstraints, TypeAdapter, ValidationError

from app.agents.state import ProgressEvent
from app.api.analytics import analytics_service, run_detached, start_run
from app.core.exceptions import AppError, AuthenticationError
from app.core.logging import get_logger, request_id_var
from app.core.metrics import WS_CONNECTIONS, WS_MESSAGES
from app.core.permissions import Permission
from app.core.security import AccessTokenClaims
from app.core.tenant_context import TenantContext, bind_context
from app.dependencies import AppResources, load_context
from app.schemas.analytics import QueryResponse, Question
from app.schemas.common import StrictModel
from app.services.analytics_service import response_status

logger = get_logger(__name__)

router = APIRouter(prefix="/analytics", tags=["analytics"])

CLOSE_POLICY = 1008
CLOSE_TOO_BIG = 1009
CLOSE_UNAUTHENTICATED = 4401
SHUTDOWN_GRACE_SECONDS = 10.0

ClientRef = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,64}$")]


class WsAuth(StrictModel):
    type: Literal["auth"]
    access_token: Annotated[str, StringConstraints(min_length=1, max_length=8192)]


class WsQuery(StrictModel):
    type: Literal["query"]
    client_ref: ClientRef
    question: Question
    connection_id: uuid.UUID | None = None
    parent_query_id: uuid.UUID | None = None
    conversation_id: uuid.UUID | None = None
    mode: Literal["execute", "plan"] = "execute"
    idempotency_key: Annotated[str, StringConstraints(max_length=128)] | None = None


class WsCancel(StrictModel):
    type: Literal["cancel"]
    query_id: uuid.UUID


class WsPing(StrictModel):
    type: Literal["ping"]


ClientMessage = Annotated[WsAuth | WsQuery | WsCancel | WsPing, Field(discriminator="type")]
_MESSAGES: TypeAdapter[WsAuth | WsQuery | WsCancel | WsPing] = TypeAdapter(ClientMessage)


def _error(
    code: str,
    message: str,
    status: int,
    *,
    client_ref: str | None = None,
    query_id: uuid.UUID | str | None = None,
    details: dict[str, Any] | None = None,
    response: QueryResponse | None = None,
) -> dict[str, Any]:
    frame: dict[str, Any] = {
        "type": "error",
        "error": {"code": code, "message": message, "status": status, "details": details or {}},
    }
    if client_ref is not None:
        frame["client_ref"] = client_ref
    if query_id is not None:
        frame["query_id"] = str(query_id)
    if response is not None:
        frame["response"] = response.model_dump(mode="json")
    return frame


def _app_error(exc: AppError, **kw: Any) -> dict[str, Any]:
    return _error(exc.code, exc.message, exc.status_code, details=dict(exc.details), **kw)


class _Connection:
    def __init__(self, websocket: WebSocket, resources: AppResources) -> None:
        self.ws = websocket
        self.resources = resources
        self.settings = resources.settings
        self.token: str | None = None
        self.claims: AccessTokenClaims | None = None
        self.queries: dict[str, asyncio.Task[None]] = {}
        self.query_ids: dict[str, uuid.UUID] = {}
        self._send_lock = asyncio.Lock()
        self._closed = False

    # ------------------------------------------------------------ transport
    async def send(self, frame: dict[str, Any]) -> None:
        if self._closed:
            return
        async with self._send_lock:
            try:
                await self.ws.send_text(json.dumps(frame, default=str))
            except (WebSocketDisconnect, RuntimeError, OSError):
                self._closed = True

    async def receive(self) -> str | None:
        """Next text frame; ``None`` when the frame is too large (connection stays open)."""
        raw = await self.ws.receive_text()
        if len(raw.encode("utf-8")) > self.settings.ws_max_message_bytes:
            WS_MESSAGES.labels("unknown", "too_large").inc()
            await self.send(_error("MESSAGE_TOO_LARGE", "The message is too large.", 413))
            return None
        return raw

    def parse(self, raw: str) -> WsAuth | WsQuery | WsCancel | WsPing | None:
        try:
            return _MESSAGES.validate_json(raw)
        except ValidationError:
            return None

    # ------------------------------------------------------------ auth
    async def authenticate(self, token: str) -> TenantContext:
        claims = self.resources.tokens.decode_access_token(token)
        async with self.resources.database.session_factory() as session:
            ctx = await load_context(
                session,
                user_id=claims.user_id,
                tenant_id=claims.tenant_id,
                session_id=claims.session_id,
            )
        if self.claims is not None and (
            claims.user_id != self.claims.user_id or claims.tenant_id != self.claims.tenant_id
        ):
            # A socket belongs to one principal for its whole life.
            raise AuthenticationError("This connection belongs to another user.")
        self.token, self.claims = token, claims
        return ctx

    async def context(self) -> TenantContext:
        """Re-validate token expiry and user/tenant/session state (per query, like HTTP)."""
        if self.token is None:
            raise AuthenticationError()
        return await self.authenticate(self.token)

    async def ready(self, ctx: TenantContext) -> None:
        await self.send(
            {
                "type": "ready",
                "user_id": str(ctx.user_id),
                "tenant": ctx.tenant_slug,
                "heartbeat_seconds": self.settings.ws_heartbeat_seconds,
            }
        )

    # ------------------------------------------------------------ lifecycle
    async def serve(self) -> None:
        try:
            raw = await asyncio.wait_for(self.receive(), self.settings.ws_auth_timeout_seconds)
        except TimeoutError:
            await self.ws.close(CLOSE_UNAUTHENTICATED, "authentication required")
            return
        message = self.parse(raw) if raw is not None else None
        if not isinstance(message, WsAuth):
            WS_MESSAGES.labels("auth", "rejected").inc()
            await self.ws.close(CLOSE_UNAUTHENTICATED, "authentication required")
            return
        try:
            ctx = await self.authenticate(message.access_token)
        except AppError as exc:
            WS_MESSAGES.labels("auth", "rejected").inc()
            await self.send(_app_error(exc))
            await self.ws.close(CLOSE_UNAUTHENTICATED, "authentication failed")
            return
        WS_MESSAGES.labels("auth", "ok").inc()
        await self.ready(ctx)
        heartbeat = asyncio.create_task(self._heartbeat())
        try:
            while True:
                raw = await self.receive()
                if raw is None:
                    continue
                await self.dispatch(self.parse(raw))
        except WebSocketDisconnect:
            pass
        finally:
            self._closed = True
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat

    async def _heartbeat(self) -> None:
        while not self._closed:
            await asyncio.sleep(self.settings.ws_heartbeat_seconds)
            await self.send({"type": "ping"})

    async def dispatch(self, message: WsAuth | WsQuery | WsCancel | WsPing | None) -> None:
        if message is None:
            WS_MESSAGES.labels("unknown", "invalid").inc()
            await self.send(_error("INVALID_MESSAGE", "The message is not valid.", 400))
        elif isinstance(message, WsPing):
            await self.send({"type": "pong"})
        elif isinstance(message, WsAuth):
            try:
                ctx = await self.authenticate(message.access_token)
            except AppError as exc:
                WS_MESSAGES.labels("auth", "rejected").inc()
                await self.send(_app_error(exc))
                return
            WS_MESSAGES.labels("auth", "ok").inc()
            await self.ready(ctx)
        elif isinstance(message, WsCancel):
            await self.cancel(message.query_id)
        else:
            await self.submit(message)

    async def submit(self, message: WsQuery) -> None:
        ref = message.client_ref
        if ref in self.queries:
            await self.send(
                _error("DUPLICATE_CLIENT_REF", "This client_ref is in use.", 409, client_ref=ref)
            )
            return
        if len(self.queries) >= self.settings.ws_max_concurrent_queries:
            WS_MESSAGES.labels("query", "too_many").inc()
            await self.send(
                _error(
                    "TOO_MANY_QUERIES",
                    "Too many questions are running on this connection.",
                    429,
                    client_ref=ref,
                )
            )
            return
        task = asyncio.create_task(self._run_query(message))
        self.queries[ref] = task
        task.add_done_callback(lambda _: self._forget(ref))

    def _forget(self, ref: str) -> None:
        self.queries.pop(ref, None)
        self.query_ids.pop(ref, None)

    async def _run_query(self, message: WsQuery) -> None:
        ref = message.client_ref
        request_id_var.set(f"ws_{secrets.token_hex(12)}")
        try:
            ctx = await self.context()
            ctx.require(Permission.ANALYTICS_QUERY)
            async with self.resources.database.session_factory() as session:
                with bind_context(ctx):
                    started, previous_plan = await start_run(
                        analytics_service(self.resources, session, ctx),
                        question=message.question,
                        connection_id=message.connection_id,
                        parent_query_id=message.parent_query_id,
                        conversation_id=message.conversation_id,
                        idempotency_key=message.idempotency_key,
                    )
        except AppError as exc:
            WS_MESSAGES.labels("query", "rejected").inc()
            await self.send(_app_error(exc, client_ref=ref))
            return
        WS_MESSAGES.labels("query", "accepted").inc()
        if isinstance(started, QueryResponse):  # idempotent replay
            await self.send(
                {"type": "accepted", "client_ref": ref, "query_id": str(started.query_id)}
            )
            await self._final(ref, started)
            return
        query_id = started.id
        self.query_ids[ref] = query_id
        await self.send({"type": "accepted", "client_ref": ref, "query_id": str(query_id)})

        async def emit(event: ProgressEvent) -> None:
            await self.send(
                {
                    "type": "progress",
                    "client_ref": ref,
                    "query_id": str(query_id),
                    "event": event.model_dump(mode="json"),
                }
            )

        result = await run_detached(
            self.resources,
            ctx,
            query_id,
            mode=message.mode,
            previous_plan=previous_plan,
            emit=emit,
        )
        if isinstance(result, AppError):
            await self.send(_app_error(result, client_ref=ref, query_id=query_id))
        else:
            await self._final(ref, result)

    async def _final(self, ref: str, response: QueryResponse) -> None:
        status = response_status(response)
        if status == 200:
            await self.send(
                {
                    "type": "result",
                    "client_ref": ref,
                    "query_id": str(response.query_id),
                    "response": response.model_dump(mode="json"),
                }
            )
            return
        err = response.error
        await self.send(
            _error(
                err.code if err else "QUERY_CANCELLED",
                err.message if err else "The query was cancelled.",
                status,
                client_ref=ref,
                query_id=response.query_id,
                details=err.details if err else None,
                response=response,
            )
        )

    async def cancel(self, query_id: uuid.UUID) -> None:
        try:
            ctx = await self.context()
            async with self.resources.database.session_factory() as session:
                with bind_context(ctx):
                    out = await analytics_service(self.resources, session, ctx).cancel(query_id)
        except AppError as exc:
            await self.send(_app_error(exc, query_id=query_id))
            return
        WS_MESSAGES.labels("cancel", "ok").inc()
        await self.send({"type": "cancelled", **out.model_dump(mode="json")})

    async def shutdown(self) -> None:
        """Cancel this socket's running queries; let them finish history + audit."""
        for query_id in list(self.query_ids.values()):
            run = self.resources.analytics_runs.get(query_id)
            if run is not None:
                run.cancel.set()
        tasks = list(self.queries.values())
        if not tasks:
            return
        _, pending = await asyncio.wait(tasks, timeout=SHUTDOWN_GRACE_SECONDS)
        for task in pending:
            task.cancel()


@router.websocket("/ws")
async def analytics_socket(websocket: WebSocket) -> None:
    resources: AppResources = websocket.app.state.resources
    origin = websocket.headers.get("origin")
    if origin is not None and origin not in resources.settings.cors_allowed_origins:
        WS_MESSAGES.labels("connect", "origin_rejected").inc()
        await websocket.close(CLOSE_POLICY)  # before accept: rejected with HTTP 403
        return
    await websocket.accept()
    WS_CONNECTIONS.inc()
    connection = _Connection(websocket, resources)
    try:
        await connection.serve()
    finally:
        WS_CONNECTIONS.dec()
        await connection.shutdown()
