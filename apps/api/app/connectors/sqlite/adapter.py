"""SQLite adapter (aiosqlite) for development, demos and tests.

A SQLite "connection" is a file on the API server, so it is sandboxed:
* disabled in production unless ``SQLITE_CONNECTIONS_ENABLED=true``;
* the path must resolve (after following symlinks) inside ``SQLITE_DATA_DIR``,
  be an existing regular file and have a .db/.sqlite/.sqlite3 extension;
* the file is opened with ``mode=ro`` (URI) and ``PRAGMA query_only = ON``;
* ATTACH is impossible for the reader (query_only + our SELECT-only execution).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import quote

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from app.connectors.common.errors import ConnectionConfigError, ConnectorErrorCode
from app.connectors.common.sqlalchemy_adapter import PostureError, SqlAlchemyAdapter
from app.connectors.common.types import ConnectionConfig, ConnectorType

ALLOWED_SUFFIXES = frozenset({".db", ".sqlite", ".sqlite3"})


def resolve_sqlite_path(raw: str | None, data_dir: str) -> Path:
    if not raw:
        raise ConnectionConfigError(
            "A database file is required.", details={"reason": "MISSING_FIELDS"}
        )
    base = Path(data_dir).resolve()
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = base / candidate
    resolved = candidate.resolve()
    if not resolved.is_relative_to(base):
        raise ConnectionConfigError(
            "The database file must be inside the configured SQLite data directory.",
            details={"reason": "PATH_NOT_ALLOWED"},
        )
    if resolved.suffix.lower() not in ALLOWED_SUFFIXES:
        raise ConnectionConfigError(
            "Unsupported SQLite file type.", details={"reason": "PATH_NOT_ALLOWED"}
        )
    if not resolved.is_file():
        raise ConnectionConfigError(
            "The database file does not exist.", details={"reason": "FILE_NOT_FOUND"}
        )
    return resolved


class SQLiteAdapter(SqlAlchemyAdapter):
    connector_type = ConnectorType.SQLITE
    system_schemas: ClassVar[frozenset[str]] = frozenset({"temp"})
    explain_prefix: ClassVar[str] = "EXPLAIN QUERY PLAN "

    def __init__(self, config: ConnectionConfig, *, data_dir: str) -> None:
        super().__init__(config)
        self._path = resolve_sqlite_path(config.database, data_dir)

    def _build_engine(self) -> AsyncEngine:
        # ``uri=true`` must be in the URL query for SQLAlchemy to open in URI mode;
        # ``mode=ro`` then makes the file read-only at the SQLite level.
        uri = f"file:{quote(self._path.as_posix(), safe='/:')}?mode=ro&uri=true"
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{uri}",
            connect_args={"timeout": self.config.connect_timeout_seconds},
            poolclass=NullPool,
        )

        @event.listens_for(engine.sync_engine, "connect")
        def _harden(dbapi_connection: Any, _: Any) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA query_only = ON")
            cursor.execute("PRAGMA trusted_schema = OFF")
            cursor.close()

        return engine

    @staticmethod
    def _visible_object(name: str) -> bool:
        return not name.startswith("sqlite_")

    async def _server_version(self, conn: AsyncConnection) -> str | None:
        value = (await conn.execute(text("SELECT sqlite_version()"))).scalar()
        return f"SQLite {value}" if value is not None else None

    async def _verify_read_only(self, conn: AsyncConnection) -> list[str]:
        value = (await conn.execute(text("PRAGMA query_only"))).scalar()
        if int(value or 0) != 1:
            raise PostureError(ConnectorErrorCode.NOT_READ_ONLY)
        return []

    async def _interrupt(self, conn: AsyncConnection) -> None:
        raw = await conn.get_raw_connection()
        driver = raw.driver_connection
        interrupt = getattr(driver, "interrupt", None)
        if interrupt is not None:
            await interrupt()
