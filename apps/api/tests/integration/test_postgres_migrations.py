"""Run the real migrations against PostgreSQL in a throwaway database."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from tests.integration.conftest import require_env
from tests.sync import run_sync
from tests.unit.test_migrations import alembic_config

pytestmark = pytest.mark.integration


async def _admin(sql: str, admin_url: str) -> None:
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    async with engine.connect() as conn:
        await conn.execute(text(sql))
    await engine.dispose()


@pytest.fixture
def pg_url() -> Iterator[str]:
    admin_url = require_env("TEST_POSTGRES_ADMIN_URL")
    name = f"erp_test_{uuid.uuid4().hex[:10]}"
    run_sync(_admin(f'CREATE DATABASE "{name}"', admin_url))
    try:
        yield make_url(admin_url).set(database=name).render_as_string(hide_password=False)
    finally:
        run_sync(_admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)', admin_url))


async def _scalar(url: str, sql: str) -> object:
    engine = create_async_engine(url)
    async with engine.connect() as conn:
        value = (await conn.execute(text(sql))).scalar()
    await engine.dispose()
    return value


async def _exec(url: str, sql: str, params: dict[str, object] | None = None) -> None:
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.execute(text(sql), params or {})
    await engine.dispose()


def test_upgrade_downgrade_roundtrip_on_postgres(pg_url: str) -> None:
    cfg = alembic_config(pg_url)
    command.upgrade(cfg, "head")
    assert run_sync(_scalar(pg_url, "SELECT count(*) FROM roles")) == 5
    assert (
        run_sync(
            _scalar(
                pg_url,
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_name='tenants' AND column_name='settings'",
            )
        )
        == "jsonb"
    )
    command.downgrade(cfg, "base")
    assert (
        run_sync(
            _scalar(
                pg_url,
                "SELECT count(*) FROM pg_tables WHERE schemaname='public' "
                "AND tablename <> 'alembic_version'",
            )
        )
        == 0
    )


def test_audit_events_are_append_only_on_postgres(pg_url: str) -> None:
    command.upgrade(alembic_config(pg_url), "head")
    run_sync(
        _exec(
            pg_url,
            "INSERT INTO audit_events (event_type, outcome, details) "
            "VALUES ('test.event', 'success', '{}'::jsonb)",
        )
    )
    for statement in (
        "UPDATE audit_events SET outcome = 'error'",
        "DELETE FROM audit_events",
        "TRUNCATE audit_events",
    ):
        with pytest.raises(DBAPIError, match="append-only"):
            run_sync(_exec(pg_url, statement))
    assert run_sync(_scalar(pg_url, "SELECT count(*) FROM audit_events")) == 1


def test_read_only_constraint_enforced_on_postgres(pg_url: str) -> None:
    command.upgrade(alembic_config(pg_url), "head")
    tenant_id = uuid.uuid4()
    run_sync(
        _exec(
            pg_url,
            "INSERT INTO tenants (id, slug, name, status, settings) "
            "VALUES (:id, 't1', 'T', 'active', '{}'::jsonb)",
            {"id": tenant_id},
        )
    )
    with pytest.raises(DBAPIError, match="read_only_enforced"):
        run_sync(
            _exec(
                pg_url,
                "INSERT INTO db_connections (id, tenant_id, name, connector_type, tls_mode, "
                "pool_min_size, pool_max_size, read_only, query_timeout_seconds, max_rows, "
                "options, status) VALUES (:id, :tenant_id, 'rw', 'postgresql', 'require', "
                "1, 5, false, 30, 1000, '{}'::jsonb, 'untested')",
                {"id": uuid.uuid4(), "tenant_id": tenant_id},
            )
        )
