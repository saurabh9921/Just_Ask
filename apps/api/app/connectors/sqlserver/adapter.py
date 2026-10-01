"""Microsoft SQL Server / Azure SQL adapter (aioodbc + Microsoft ODBC Driver 17/18).

SQL Server has no session-level read-only switch, so the posture check is stricter
than for PostgreSQL/MySQL: ``test_connection`` *refuses* logins that are
``sysadmin``, ``db_owner``, ``db_datawriter``, ``db_ddladmin`` or hold
INSERT/UPDATE/DELETE on the database; customers grant ``db_datareader`` (or SELECT
on reporting views) only. In addition:
* connections use ``ApplicationIntent=ReadOnly`` (routes to readable secondaries in an
  availability group and documents intent);
* every analytical transaction is rolled back (never committed);
* the ODBC query timeout and ``LOCK_TIMEOUT`` bound every statement.

Estimated plans (SHOWPLAN) need a separate batch and elevated permission, so
``explain_query`` is not supported: the cost estimator falls back to metadata row
estimates (``sys.partitions``).
"""

from __future__ import annotations

import re
from typing import Any, ClassVar

from sqlalchemy import URL, event, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from app.connectors.common.errors import (
    ConnectionConfigError,
    ConnectorErrorCode,
    UnsupportedConnectorError,
)
from app.connectors.common.sqlalchemy_adapter import PostureError, SqlAlchemyAdapter
from app.connectors.common.types import ConnectionConfig, ConnectorType, ExplainResult, TlsMode

DEFAULT_PORT = 1433
DEFAULT_ODBC_DRIVER = "ODBC Driver 18 for SQL Server"
_UNSAFE_ODBC = re.compile(r"[;{}=\x00]")

_ROLE_SCHEMAS = frozenset(
    {
        "sys",
        "information_schema",
        "guest",
        "db_owner",
        "db_accessadmin",
        "db_securityadmin",
        "db_ddladmin",
        "db_backupoperator",
        "db_datareader",
        "db_datawriter",
        "db_denydatareader",
        "db_denydatawriter",
    }
)


def odbc_value(value: str) -> str:
    """Brace-quote an ODBC attribute value (``}`` is escaped by doubling)."""
    if "\x00" in value:
        raise ConnectionConfigError(
            "Connection settings contain invalid characters.",
            details={"reason": "INVALID_CHARACTERS"},
        )
    return "{" + value.replace("}", "}}") + "}"


def tls_attributes(tls_mode: TlsMode) -> str:
    if tls_mode is TlsMode.DISABLE:
        return "Encrypt=no"
    if tls_mode is TlsMode.PREFER:
        return "Encrypt=optional"
    if tls_mode is TlsMode.REQUIRE:
        return "Encrypt=yes;TrustServerCertificate=yes"
    # VERIFY_CA / VERIFY_FULL: the certificate chain is validated against the OS trust
    # store (install a private CA there); the driver checks the host name.
    return "Encrypt=yes;TrustServerCertificate=no"


def build_odbc_connect(cfg: ConnectionConfig, driver: str) -> str:
    """ODBC connection string. Host/port/driver never contain ODBC metacharacters."""
    host = cfg.host or ""
    if not host or _UNSAFE_ODBC.search(host) or _UNSAFE_ODBC.search(driver):
        raise ConnectionConfigError(
            "Connection settings contain invalid characters.",
            details={"reason": "INVALID_CHARACTERS"},
        )
    parts = [
        f"DRIVER={odbc_value(driver)}",
        f"SERVER={host},{int(cfg.port or DEFAULT_PORT)}",
        f"DATABASE={odbc_value(cfg.database or '')}",
        f"UID={odbc_value(cfg.username or '')}",
        f"PWD={odbc_value(cfg.password.get_secret_value() if cfg.password else '')}",
        tls_attributes(cfg.tls_mode),
        "ApplicationIntent=ReadOnly",
        "MultipleActiveResultSets=no",
        f"Connection Timeout={max(1, int(cfg.connect_timeout_seconds))}",
        "APP=erp-insight-ai",
    ]
    return ";".join(parts)


class SQLServerAdapter(SqlAlchemyAdapter):
    connector_type = ConnectorType.SQLSERVER
    system_schemas: ClassVar[frozenset[str]] = _ROLE_SCHEMAS

    def __init__(self, config: ConnectionConfig, *, odbc_driver: str = DEFAULT_ODBC_DRIVER) -> None:
        super().__init__(config)
        self._odbc_driver = odbc_driver

    def _create_engine(self, cfg: ConnectionConfig) -> AsyncEngine:
        if not cfg.host or not cfg.database or not cfg.username:
            raise ConnectionConfigError(
                "Host, database and username are required.",
                details={"reason": "MISSING_FIELDS"},
            )
        url = URL.create(
            "mssql+aioodbc", query={"odbc_connect": build_odbc_connect(cfg, self._odbc_driver)}
        )
        engine = create_async_engine(
            url,
            pool_size=max(1, cfg.pool_max_size),
            max_overflow=0,
            pool_timeout=cfg.connect_timeout_seconds,
            pool_recycle=1800,
            pool_pre_ping=True,
        )
        timeout = int(cfg.query_timeout_seconds) + 5

        @event.listens_for(engine.sync_engine, "connect")
        def _set_query_timeout(dbapi_connection: Any, _record: Any) -> None:
            # aioodbc wraps a pyodbc connection; its ``timeout`` is the per-statement limit.
            raw = getattr(getattr(dbapi_connection, "driver_connection", None), "_conn", None)
            if raw is not None:
                raw.timeout = timeout

        return engine

    def _visible(self, schema: str) -> bool:
        return schema.lower() not in self.system_schemas

    async def get_schemas(self) -> list[str]:
        if self.config.schema:
            return [self.config.schema]
        return await super().get_schemas()

    async def _begin_readonly(self, conn: AsyncConnection, timeout_seconds: float) -> None:
        await conn.execute(text("SET LOCK_TIMEOUT 5000"))

    async def _server_version(self, conn: AsyncConnection) -> str | None:
        value = (
            await conn.execute(
                text("SELECT CAST(SERVERPROPERTY('ProductVersion') AS NVARCHAR(64))")
            )
        ).scalar()
        return str(value) if value is not None else None

    async def _verify_read_only(self, conn: AsyncConnection) -> list[str]:
        row = (
            await conn.execute(
                text(
                    "SELECT IS_SRVROLEMEMBER('sysadmin'), IS_ROLEMEMBER('db_owner'), "
                    "IS_ROLEMEMBER('db_datawriter'), IS_ROLEMEMBER('db_ddladmin'), "
                    "HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'INSERT'), "
                    "HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'UPDATE'), "
                    "HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'DELETE'), "
                    "HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'CREATE TABLE')"
                )
            )
        ).one()
        sysadmin, owner, writer, ddl, can_insert, can_update, can_delete, can_create = (
            int(v or 0) for v in row
        )
        if sysadmin or owner or writer or ddl or can_insert or can_update or can_delete:
            raise PostureError(ConnectorErrorCode.ROLE_TOO_PRIVILEGED)
        if can_create:
            return [
                "The login can create tables in this database; db_datareader only is recommended."
            ]
        return []

    async def _row_estimates(self, conn: AsyncConnection, schema: str) -> dict[str, int]:
        rows = await conn.execute(
            text(
                "SELECT o.name, SUM(p.rows) FROM sys.objects o "
                "JOIN sys.schemas s ON s.schema_id = o.schema_id "
                "JOIN sys.partitions p ON p.object_id = o.object_id AND p.index_id IN (0, 1) "
                "WHERE s.name = :schema AND o.type = 'U' GROUP BY o.name"
            ),
            {"schema": schema},
        )
        return {str(name): int(est) for name, est in rows if est is not None}

    async def explain_query(self, query: Any) -> ExplainResult:
        self._guard(query)
        raise UnsupportedConnectorError(
            "Estimated plans are not collected for SQL Server.",
            details={"connector_type": self.connector_type.value},
        )
