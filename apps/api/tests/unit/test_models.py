from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import Base, DatabaseConnection, Feedback, QueryRecord, Tenant, User
from app.db.session import Database


@pytest.fixture
async def session(settings: Settings) -> AsyncIterator[AsyncSession]:
    db = Database(settings)
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with db.session_factory() as s:
        yield s
    await db.dispose()


async def _tenant_and_user(session: AsyncSession) -> tuple[Tenant, User]:
    tenant = Tenant(slug=f"t-{uuid.uuid4().hex[:8]}", name="Acme")
    session.add(tenant)
    await session.flush()
    user = User(tenant_id=tenant.id, email="a@acme.test", display_name="A")
    session.add(user)
    await session.flush()
    return tenant, user


async def test_defaults_are_secure_for_new_connection(session: AsyncSession) -> None:
    tenant, _ = await _tenant_and_user(session)
    conn = DatabaseConnection(
        tenant_id=tenant.id,
        name="erp",
        connector_type="postgresql",
        secret_ref="env://ERP_DB_PASSWORD",
    )
    session.add(conn)
    await session.flush()
    loaded = (await session.execute(select(DatabaseConnection))).scalar_one()
    assert loaded.read_only is True
    assert loaded.tls_mode == "verify-full"
    assert loaded.max_rows == 1000
    assert loaded.options == {}


async def test_writable_connection_is_rejected_by_database(session: AsyncSession) -> None:
    tenant, _ = await _tenant_and_user(session)
    session.add(
        DatabaseConnection(
            tenant_id=tenant.id, name="rw", connector_type="postgresql", read_only=False
        )
    )
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_unknown_connector_type_is_rejected(session: AsyncSession) -> None:
    tenant, _ = await _tenant_and_user(session)
    session.add(DatabaseConnection(tenant_id=tenant.id, name="x", connector_type="mongodb"))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_email_unique_per_tenant_not_globally(session: AsyncSession) -> None:
    tenant_a, _ = await _tenant_and_user(session)
    tenant_b, _ = await _tenant_and_user(session)
    assert tenant_a.id != tenant_b.id  # same email in two tenants is fine
    session.add(User(tenant_id=tenant_a.id, email="a@acme.test", display_name="dup"))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_feedback_rating_constrained(session: AsyncSession) -> None:
    tenant, user = await _tenant_and_user(session)
    record = QueryRecord(tenant_id=tenant.id, user_id=user.id, question="Total sales by region")
    session.add(record)
    await session.flush()
    session.add(Feedback(tenant_id=tenant.id, user_id=user.id, query_id=record.id, rating=5))
    with pytest.raises(IntegrityError):
        await session.flush()
