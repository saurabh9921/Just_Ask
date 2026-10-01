"""Answer feedback: one rating per user and query, plus query feedback memory.

Ratings are stored in ``feedback`` (comments never leave the database: they are not
logged, audited or sent to a model). A positive rating on a succeeded query adds its
question + plan structure to the ``query_feedback`` retrieval collection; a negative
rating removes it (see ``app/rag/feedback_memory.py``). Memory updates are best
effort: a vector-store failure never loses the rating itself.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core.exceptions import ConflictError, NotFoundError
from app.core.logging import get_logger
from app.core.metrics import FEEDBACK_SUBMISSIONS
from app.core.permissions import Permission
from app.core.security import utcnow
from app.core.tenant_context import TenantContext
from app.db.models import Feedback, QueryRecord
from app.rag.collections import Collection
from app.rag.feedback_memory import active_version, doc_key, feedback_document, record_update
from app.rag.hybrid_retriever import HybridRetriever
from app.rag.ingestion import IngestionDeps, upsert_documents
from app.schemas.feedback import FeedbackCreate, FeedbackOut, FeedbackPage
from app.semantic.registry import SemanticLayerRegistry
from app.services.audit_service import AuditOutcome, AuditService

logger = get_logger(__name__)

RATEABLE_STATUSES = frozenset({"succeeded", "planned", "clarification", "refused", "failed"})


class QueryNotRateableError(ConflictError):
    code = "QUERY_NOT_RATEABLE"
    default_message = "Feedback can be given once the query has finished."


def to_out(row: Feedback, question: str | None = None, *, remembered: bool = False) -> FeedbackOut:
    return FeedbackOut(
        id=row.id,
        query_id=row.query_id,
        user_id=row.user_id,
        question=question,
        rating=row.rating,
        categories=list(row.categories),
        comment=row.comment,
        created_at=row.created_at,
        updated_at=row.updated_at,
        remembered=remembered,
    )


@dataclass
class FeedbackService:
    session: AsyncSession
    settings: Settings
    ctx: TenantContext
    audit: AuditService
    registry: SemanticLayerRegistry
    ingestion: IngestionDeps
    retriever: HybridRetriever

    async def _own_query(self, query_id: uuid.UUID) -> QueryRecord:
        record = await self.session.scalar(
            select(QueryRecord).where(
                QueryRecord.id == query_id,
                QueryRecord.tenant_id == self.ctx.tenant_id,
                QueryRecord.user_id == self.ctx.user_id,
                QueryRecord.deleted_at.is_(None),
            )
        )
        if record is None:
            raise NotFoundError()
        return record

    async def submit(self, body: FeedbackCreate) -> FeedbackOut:
        self.ctx.require(Permission.FEEDBACK_WRITE)
        record = await self._own_query(body.query_id)
        if record.status not in RATEABLE_STATUSES:
            raise QueryNotRateableError()
        row = await self.session.scalar(
            select(Feedback).where(
                Feedback.tenant_id == self.ctx.tenant_id,
                Feedback.user_id == self.ctx.user_id,
                Feedback.query_id == record.id,
            )
        )
        if row is None:
            row = Feedback(
                tenant_id=self.ctx.tenant_id,
                user_id=self.ctx.user_id,
                query_id=record.id,
                rating=body.rating,
                categories=list(body.categories),
                comment=body.comment,
                created_at=utcnow(),
            )
            self.session.add(row)
        else:
            row.rating = body.rating
            row.categories = list(body.categories)
            row.comment = body.comment
            row.updated_at = utcnow()
        await self.session.commit()
        await self.session.refresh(row)
        FEEDBACK_SUBMISSIONS.labels("positive" if body.rating == 1 else "negative").inc()

        remembered = False
        if body.rating == 1 and record.status == "succeeded":
            remembered = await self.remember(record)
        else:
            await self.forget(record)
        await self.audit.record(
            "feedback.submit",
            AuditOutcome.SUCCESS,
            tenant_id=self.ctx.tenant_id,
            actor_user_id=self.ctx.user_id,
            resource_type="query",
            resource_id=str(record.id),
            details={
                "rating": body.rating,
                "categories": list(body.categories),
                "has_comment": body.comment is not None,
                "remembered": remembered,
            },
        )
        return to_out(row, record.question, remembered=remembered)

    async def remember(self, record: QueryRecord) -> bool:
        """Add a confirmed answer to the connection's active retrieval index."""
        if not self.settings.feedback_memory_enabled or record.connection_id is None:
            return False
        try:
            version = await active_version(self.session, self.ctx.tenant_id, record.connection_id)
            if version is None:
                record_update("add", "skipped")
                return False
            bound = {name for name, b in (version.entity_bindings or {}).items() if b.get("bound")}
            doc = feedback_document(
                self.registry.get(),
                bound,
                query_id=record.id,
                question=record.question,
                intent=record.intent,
                plan=record.query_plan,
            )
            if doc is None:
                record_update("add", "skipped")
                return False
            await upsert_documents(
                self.ingestion,
                [doc],
                tenant_id=self.ctx.tenant_id,
                connection_id=record.connection_id,
                version=version.version,
            )
        except Exception:  # noqa: BLE001 - memory is best effort; the rating is stored
            logger.warning("feedback memory update failed", extra={"query_id": str(record.id)})
            record_update("add", "error")
            return False
        await self.retriever.invalidate_connection(self.ctx.tenant_id, record.connection_id)
        record_update("add", "ok")
        return True

    async def forget(self, record: QueryRecord) -> None:
        if record.connection_id is None:
            return
        try:
            await self.ingestion.store.delete_document(
                Collection.QUERY_FEEDBACK,
                tenant_id=self.ctx.tenant_id,
                connection_id=record.connection_id,
                doc_key=doc_key(record.id),
            )
        except Exception:  # noqa: BLE001 - collection may not exist yet
            logger.info("no feedback memory to remove", extra={"query_id": str(record.id)})
            record_update("remove", "skipped")
            return
        await self.retriever.invalidate_connection(self.ctx.tenant_id, record.connection_id)
        record_update("remove", "ok")

    async def list(
        self,
        *,
        limit: int,
        offset: int,
        rating: int | None = None,
        query_id: uuid.UUID | None = None,
        scope: str = "mine",
    ) -> FeedbackPage:
        """Own feedback, or the whole tenant's with ``scope=tenant`` (``admin.manage``)."""
        self.ctx.require(Permission.FEEDBACK_WRITE)
        if scope == "tenant":
            self.ctx.require(Permission.ADMIN_MANAGE)
        conditions = [Feedback.tenant_id == self.ctx.tenant_id]
        if scope != "tenant":
            conditions.append(Feedback.user_id == self.ctx.user_id)
        if rating is not None:
            conditions.append(Feedback.rating == rating)
        if query_id is not None:
            conditions.append(Feedback.query_id == query_id)
        total = await self.session.scalar(
            select(func.count())
            .select_from(Feedback)
            .join(QueryRecord, QueryRecord.id == Feedback.query_id)
            .where(*conditions, QueryRecord.deleted_at.is_(None))
        )
        rows = await self.session.execute(
            select(Feedback, QueryRecord.question)
            .join(QueryRecord, QueryRecord.id == Feedback.query_id)
            .where(*conditions, QueryRecord.deleted_at.is_(None))
            .order_by(Feedback.created_at.desc(), Feedback.id)
            .limit(limit)
            .offset(offset)
        )
        return FeedbackPage(
            items=[to_out(row, question) for row, question in rows],
            total=int(total or 0),
            limit=limit,
            offset=offset,
        )
