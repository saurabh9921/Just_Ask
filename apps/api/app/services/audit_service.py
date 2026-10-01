"""Audit trail writer.

Audit events are written in their **own session and transaction**, so a denied or
failed operation is recorded even when the business transaction rolls back
(e.g. a failed login). ``details`` are redacted before storage and must contain
identifiers and categories only - never secrets, SQL values or result rows.
"""

from __future__ import annotations

import uuid
from enum import StrEnum
from typing import Any

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import get_logger, redact, request_id_var
from app.db.models import AuditEvent

logger = get_logger(__name__)


class AuditOutcome(StrEnum):
    SUCCESS = "success"
    DENIED = "denied"
    ERROR = "error"


class AuditService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def record(
        self,
        event_type: str,
        outcome: AuditOutcome,
        *,
        tenant_id: uuid.UUID | None = None,
        actor_user_id: uuid.UUID | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        event = AuditEvent(
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            request_id=request_id_var.get(),
            event_type=event_type,
            resource_type=resource_type,
            resource_id=resource_id,
            outcome=outcome.value,
            details=redact(details or {}),
        )
        try:
            async with self._session_factory() as session:
                session.add(event)
                await session.commit()
        except SQLAlchemyError:
            # Never lose the fact that auditing failed; surface it loudly to operators.
            logger.exception(
                "AUDIT WRITE FAILED", extra={"event_type": event_type, "outcome": outcome.value}
            )
            raise
