"""Async engine and session management for the *application metadata* database.

This module is only for ERP Insight AI's own PostgreSQL database (tenants, users,
connections, audit, history). Customer ERP databases are accessed exclusively
through ``app.connectors`` adapters with read-only credentials.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import Settings


def build_engine(settings: Settings) -> AsyncEngine:
    url = make_url(settings.app_database_url.get_secret_value())
    kwargs: dict[str, Any] = {"pool_pre_ping": True}

    if url.get_backend_name() == "sqlite":
        # SQLite is used for unit tests only; no pool sizing applies. Request, background
        # run and audit sessions write concurrently: WAL lets readers and a committing
        # writer proceed together, and the busy timeout makes a second writer wait
        # instead of failing with "database is locked".
        engine = create_async_engine(url, connect_args={"timeout": 30}, **kwargs)

        @event.listens_for(engine.sync_engine, "connect")
        def _configure_sqlite(dbapi_connection: Any, _: Any) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

        return engine

    kwargs.update(
        pool_size=settings.app_db_pool_size,
        max_overflow=settings.app_db_max_overflow,
        pool_timeout=settings.app_db_pool_timeout_seconds,
        pool_recycle=settings.app_db_pool_recycle_seconds,
    )
    if url.get_backend_name() == "postgresql" and url.get_driver_name() == "asyncpg":
        kwargs["connect_args"] = {
            "server_settings": {
                "application_name": settings.app_name,
                "statement_timeout": str(settings.app_db_statement_timeout_ms),
                "idle_in_transaction_session_timeout": "60000",
            },
            "command_timeout": settings.app_db_statement_timeout_ms / 1000,
        }
    return create_async_engine(url, **kwargs)


class Database:
    """Owns the engine and session factory for the application lifetime."""

    def __init__(self, settings: Settings) -> None:
        self.engine = build_engine(settings)
        self.session_factory = async_sessionmaker(
            self.engine, expire_on_commit=False, autoflush=False
        )

    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.session_factory() as session:
            try:
                yield session
            except BaseException:
                await session.rollback()
                raise

    async def dispose(self) -> None:
        await self.engine.dispose()
