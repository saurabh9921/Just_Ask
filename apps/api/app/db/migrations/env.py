"""Alembic environment (async).

The URL comes from ``APP_DATABASE_URL`` (via Settings) unless a caller sets
``sqlalchemy.url`` on the Alembic config programmatically (used by tests).
"""

from __future__ import annotations

import asyncio

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import get_settings
from app.db.models import Base

config = context.config
target_metadata = Base.metadata


def _database_url() -> str:
    configured = config.get_main_option("sqlalchemy.url")
    if configured:
        return configured
    return get_settings().app_database_url.get_secret_value()


def _configure(connection: Connection | None = None, url: str | None = None) -> None:
    context.configure(
        connection=connection,
        url=url,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=False,
        render_as_batch=(url or "").startswith("sqlite")
        or (connection is not None and connection.dialect.name == "sqlite"),
        literal_binds=url is not None,
        dialect_opts={"paramstyle": "named"} if url is not None else {},
    )


def run_migrations_offline() -> None:
    """Emit SQL to stdout (``alembic upgrade head --sql``) for DBA review."""
    _configure(url=_database_url())
    with context.begin_transaction():
        context.run_migrations()


def _run_sync(connection: Connection) -> None:
    _configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(_database_url(), poolclass=pool.NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(_run_sync)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    existing = config.attributes.get("connection")
    if existing is not None:
        _run_sync(existing)
    else:
        asyncio.run(run_migrations_online())
