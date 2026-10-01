"""Query history of the signed-in user: list, detail, save and delete.

History is personal: every query filters on tenant *and* user, deleted rows are
hidden (soft delete keeps the audit trail consistent). Rows never contain result
data; the detail view adds the cached response while it is still in Redis.
Deleting a query also drops its cached response and its feedback-memory document.

Questions are grouped into conversations (one chat in the side panel); the
conversation views list chats, open a whole chat, and save or delete all of its
questions at once. Rows from before conversations existed form one-question chats.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import ColumnElement, Select, and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core.exceptions import NotFoundError
from app.core.logging import get_logger
from app.core.permissions import Permission
from app.core.security import utcnow
from app.core.tenant_context import TenantContext
from app.db.models import DatabaseConnection, Feedback, QueryRecord
from app.schemas.analytics import QueryResponse
from app.schemas.history import (
    ConversationDetail,
    ConversationItem,
    ConversationPage,
    HistoryDetail,
    HistoryItem,
    HistoryPage,
)
from app.services.analytics_service import ACTIVE_STATUSES, result_key
from app.services.audit_service import AuditOutcome, AuditService
from app.services.feedback_service import FeedbackService

logger = get_logger(__name__)

MAX_SEARCH_CHARS = 200

_Row = tuple[QueryRecord, str | None, int | None]
# Module-level: inside HistoryService the `list` method shadows the builtin.
_Rows = list[_Row]


def _like_pattern(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _conversation_key() -> ColumnElement[uuid.UUID]:
    """A question's conversation; older rows without one are their own conversation."""
    return func.coalesce(QueryRecord.conversation_id, QueryRecord.id)


@dataclass
class HistoryService:
    session: AsyncSession
    settings: Settings
    ctx: TenantContext
    redis: Redis
    audit: AuditService
    feedback: FeedbackService

    def _base(self) -> Select[Any]:
        return (
            select(QueryRecord, DatabaseConnection.name, Feedback.rating)
            .outerjoin(
                DatabaseConnection,
                and_(
                    DatabaseConnection.id == QueryRecord.connection_id,
                    DatabaseConnection.tenant_id == self.ctx.tenant_id,
                ),
            )
            .outerjoin(
                Feedback,
                and_(
                    Feedback.query_id == QueryRecord.id,
                    Feedback.user_id == self.ctx.user_id,
                    Feedback.tenant_id == self.ctx.tenant_id,
                ),
            )
            .where(
                QueryRecord.tenant_id == self.ctx.tenant_id,
                QueryRecord.user_id == self.ctx.user_id,
                QueryRecord.deleted_at.is_(None),
            )
        )

    @staticmethod
    def _item(record: QueryRecord, connection_name: str | None, rating: int | None) -> HistoryItem:
        return HistoryItem(
            query_id=record.id,
            conversation_id=record.conversation_id or record.id,
            question=record.question,
            status=record.status,
            intent=record.intent,
            connection_id=record.connection_id,
            connection_name=connection_name,
            row_count=record.row_count,
            total_ms=record.total_ms,
            error_code=record.error_code,
            saved=record.saved,
            feedback_rating=rating,
            created_at=record.created_at,
            completed_at=record.completed_at,
        )

    async def list(
        self,
        *,
        limit: int,
        offset: int,
        saved: bool | None = None,
        status: str | None = None,
        connection_id: uuid.UUID | None = None,
        search: str | None = None,
    ) -> HistoryPage:
        self.ctx.require(Permission.HISTORY_READ)
        stmt = self._base()
        if saved is not None:
            stmt = stmt.where(QueryRecord.saved.is_(saved))
        if status is not None:
            stmt = stmt.where(QueryRecord.status == status)
        if connection_id is not None:
            stmt = stmt.where(QueryRecord.connection_id == connection_id)
        if search:
            pattern = _like_pattern(search.strip()[:MAX_SEARCH_CHARS])
            stmt = stmt.where(QueryRecord.question.ilike(pattern, escape="\\"))
        total = await self.session.scalar(select(func.count()).select_from(stmt.subquery()))
        rows = await self.session.execute(
            stmt.order_by(QueryRecord.created_at.desc(), QueryRecord.id).limit(limit).offset(offset)
        )
        return HistoryPage(
            items=[self._item(r, name, rating) for r, name, rating in rows],
            total=int(total or 0),
            limit=limit,
            offset=offset,
        )

    async def _row(self, query_id: uuid.UUID) -> tuple[QueryRecord, str | None, int | None]:
        row = (await self.session.execute(self._base().where(QueryRecord.id == query_id))).first()
        if row is None:
            raise NotFoundError()
        record, name, rating = row
        return record, name, rating

    async def _cached(self, query_id: uuid.UUID) -> QueryResponse | None:
        try:
            raw = await self.redis.get(result_key(self.ctx.tenant_id, self.ctx.user_id, query_id))
        except (RedisError, OSError):
            logger.warning("result cache unavailable")
            return None
        return QueryResponse.model_validate_json(raw) if raw is not None else None

    async def get(self, query_id: uuid.UUID) -> HistoryDetail:
        self.ctx.require(Permission.HISTORY_READ)
        return await self._detail(*await self._row(query_id))

    async def _detail(
        self, record: QueryRecord, name: str | None, rating: int | None
    ) -> HistoryDetail:
        cached = await self._cached(record.id)
        return HistoryDetail(
            **self._item(record, name, rating).model_dump(),
            semantic_version=record.semantic_version,
            execution_ms=record.execution_ms,
            query_plan=record.query_plan,
            response=cached,
            response_expired=record.status not in ACTIVE_STATUSES and cached is None,
        )

    async def set_saved(self, query_id: uuid.UUID, saved: bool) -> HistoryItem:
        self.ctx.require(Permission.HISTORY_READ)
        record, name, rating = await self._row(query_id)
        record.saved = saved
        await self.session.commit()
        return self._item(record, name, rating)

    async def delete(self, query_id: uuid.UUID) -> None:
        self.ctx.require(Permission.HISTORY_READ)
        record, _, _ = await self._row(query_id)
        _mark_deleted(record)
        await self.session.commit()
        await self._forget(record)

    async def _forget(self, record: QueryRecord) -> None:
        """After a delete: drop the cached answer and feedback memory, audit it."""
        try:
            await self.redis.delete(result_key(self.ctx.tenant_id, self.ctx.user_id, record.id))
        except (RedisError, OSError):
            logger.warning("result cache unavailable; cached response expires by TTL")
        await self.feedback.forget(record)
        await self.audit.record(
            "history.delete",
            AuditOutcome.SUCCESS,
            tenant_id=self.ctx.tenant_id,
            actor_user_id=self.ctx.user_id,
            resource_type="query",
            resource_id=str(record.id),
        )

    # ------------------------------------------------------------ conversations
    async def list_conversations(
        self,
        *,
        limit: int,
        offset: int,
        saved: bool | None = None,
        search: str | None = None,
    ) -> ConversationPage:
        """Chats, most recently active first; search matches any of their questions."""
        self.ctx.require(Permission.HISTORY_READ)
        key = _conversation_key().label("conv_key")
        last = func.max(func.coalesce(QueryRecord.completed_at, QueryRecord.created_at)).label(
            "last_activity"
        )
        stmt = (
            select(key, last)
            .where(
                QueryRecord.tenant_id == self.ctx.tenant_id,
                QueryRecord.user_id == self.ctx.user_id,
                QueryRecord.deleted_at.is_(None),
            )
            .group_by(_conversation_key())
        )
        if saved is not None:
            any_saved = func.max(case((QueryRecord.saved.is_(True), 1), else_=0))
            stmt = stmt.having(any_saved == (1 if saved else 0))
        if search:
            pattern = _like_pattern(search.strip()[:MAX_SEARCH_CHARS])
            matches = case((QueryRecord.question.ilike(pattern, escape="\\"), 1), else_=0)
            stmt = stmt.having(func.max(matches) == 1)
        total = await self.session.scalar(select(func.count()).select_from(stmt.subquery()))
        page = (
            await self.session.execute(stmt.order_by(last.desc(), key).limit(limit).offset(offset))
        ).all()
        ids = [row.conv_key for row in page]
        grouped: dict[uuid.UUID, _Rows] = {}
        if ids:
            rows = await self.session.execute(
                self._base()
                .where(_conversation_key().in_(ids))
                .order_by(QueryRecord.created_at, QueryRecord.id)
            )
            for record, name, rating in rows:
                grouped.setdefault(record.conversation_id or record.id, []).append(
                    (record, name, rating)
                )
        return ConversationPage(
            items=[self._conversation(grouped[i]) for i in ids if i in grouped],
            total=int(total or 0),
            limit=limit,
            offset=offset,
        )

    async def get_conversation(self, conversation_id: uuid.UUID) -> ConversationDetail:
        """Every question of the chat with its answer while cached, oldest first."""
        self.ctx.require(Permission.HISTORY_READ)
        rows = await self._conversation_rows(conversation_id)
        return ConversationDetail(
            **self._conversation(rows).model_dump(),
            messages=[await self._detail(*row) for row in rows],
        )

    async def set_conversation_saved(
        self, conversation_id: uuid.UUID, saved: bool
    ) -> ConversationItem:
        self.ctx.require(Permission.HISTORY_READ)
        rows = await self._conversation_rows(conversation_id)
        for record, _, _ in rows:
            record.saved = saved
        await self.session.commit()
        return self._conversation(rows)

    async def delete_conversation(self, conversation_id: uuid.UUID) -> None:
        """Deletes every question of the chat exactly as deleting each one would."""
        self.ctx.require(Permission.HISTORY_READ)
        rows = await self._conversation_rows(conversation_id)
        for record, _, _ in rows:
            _mark_deleted(record)
        await self.session.commit()
        for record, _, _ in rows:
            await self._forget(record)

    async def _conversation_rows(self, conversation_id: uuid.UUID) -> _Rows:
        result = await self.session.execute(
            self._base()
            .where(_conversation_key() == conversation_id)
            .order_by(QueryRecord.created_at, QueryRecord.id)
        )
        rows = [(record, name, rating) for record, name, rating in result]
        if not rows:
            raise NotFoundError()
        return rows

    @staticmethod
    def _conversation(rows: _Rows) -> ConversationItem:
        """Summary of a chat from its questions (oldest first)."""
        first, last = rows[0][0], rows[-1][0]
        activity: datetime = max(r.completed_at or r.created_at for r, _, _ in rows)
        return ConversationItem(
            conversation_id=first.conversation_id or first.id,
            title=first.question,
            question_count=len(rows),
            last_question=last.question,
            last_status=last.status,
            connection_id=last.connection_id,
            connection_name=rows[-1][1],
            saved=any(r.saved for r, _, _ in rows),
            started_at=first.created_at,
            last_activity_at=activity,
        )


def _mark_deleted(record: QueryRecord) -> None:
    record.deleted_at = utcnow()
    record.saved = False
    record.idempotency_key = None  # a deleted query is never replayed
