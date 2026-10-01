"""PostgreSQL adapter (asyncpg).

Read-only posture, in addition to the customer's SELECT-only role:
* every session starts with ``default_transaction_read_only = on`` and a
  ``statement_timeout`` (server settings sent at connect time);
* every analytical transaction runs ``SET TRANSACTION READ ONLY`` and
  ``SET LOCAL statement_timeout``;
* ``test_connection`` refuses superusers and roles with ``BYPASSRLS`` and warns
  when the role can create objects in the database.
"""

from __future__ import annotations

import json
import ssl
from typing import Any, ClassVar

import asyncpg
from sqlalchemy import URL, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from app.connectors.common.errors import ConnectionConfigError, ConnectorErrorCode
from app.connectors.common.sqlalchemy_adapter import PostureError, SqlAlchemyAdapter
from app.connectors.common.types import ConnectionConfig, ConnectorType, ExplainResult, TlsMode

DEFAULT_PORT = 5432


def build_ssl(tls_mode: TlsMode, ca_pem: str | None) -> ssl.SSLContext | str | bool:
    """asyncpg ``ssl`` argument for a TLS mode."""
    if tls_mode is TlsMode.DISABLE:
        return False
    if tls_mode is TlsMode.PREFER:
        return "prefer"
    if tls_mode is TlsMode.REQUIRE:
        return "require"
    context = ssl.create_default_context(cadata=ca_pem) if ca_pem else ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    if tls_mode is TlsMode.VERIFY_CA:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_REQUIRED
    return context


class PostgreSQLAdapter(SqlAlchemyAdapter):
    connector_type = ConnectorType.POSTGRESQL
    system_schemas: ClassVar[frozenset[str]] = frozenset(
        {"information_schema", "pg_catalog", "pg_toast"}
    )
    explain_prefix: ClassVar[str] = "EXPLAIN (FORMAT JSON) "
    driver_errors: ClassVar[tuple[type[Exception], ...]] = (
        asyncpg.PostgresError,
        asyncpg.InterfaceError,
    )

    def _create_engine(self, cfg: ConnectionConfig) -> AsyncEngine:
        if not cfg.host or not cfg.database or not cfg.username:
            raise ConnectionConfigError(
                "Host, database and username are required.",
                details={"reason": "MISSING_FIELDS"},
            )
        url = URL.create(
            "postgresql+asyncpg",
            username=cfg.username,
            password=cfg.password.get_secret_value() if cfg.password else None,
            host=cfg.host,
            port=cfg.port or DEFAULT_PORT,
            database=cfg.database,
        )
        ca = cfg.tls_ca_pem.get_secret_value() if cfg.tls_ca_pem else None
        timeout_ms = str(int(cfg.query_timeout_seconds * 1000))
        connect_args: dict[str, Any] = {
            "ssl": build_ssl(cfg.tls_mode, ca),
            "timeout": cfg.connect_timeout_seconds,
            "command_timeout": cfg.query_timeout_seconds + 5,
            "server_settings": {
                "application_name": "erp-insight-ai",
                "default_transaction_read_only": "on",
                "statement_timeout": timeout_ms,
                "idle_in_transaction_session_timeout": timeout_ms,
                "lock_timeout": "5000",
            },
        }
        return create_async_engine(
            url,
            connect_args=connect_args,
            pool_size=max(1, cfg.pool_max_size),
            max_overflow=0,
            pool_timeout=cfg.connect_timeout_seconds,
            pool_recycle=1800,
            pool_pre_ping=True,
        )

    async def _begin_readonly(self, conn: AsyncConnection, timeout_seconds: float) -> None:
        await conn.execute(text("SET TRANSACTION READ ONLY"))
        # Integer computed by us - not user input - so inlining is safe (SET takes no binds).
        await conn.execute(text(f"SET LOCAL statement_timeout = {int(timeout_seconds * 1000)}"))
        if self.config.schema:
            await conn.execute(
                text("SELECT set_config('search_path', :path, true)"),
                {"path": self.quote_identifier(self.config.schema)},
            )

    async def _server_version(self, conn: AsyncConnection) -> str | None:
        value = (await conn.execute(text("SHOW server_version"))).scalar()
        return str(value) if value is not None else None

    async def _verify_read_only(self, conn: AsyncConnection) -> list[str]:
        row = (
            await conn.execute(
                text(
                    "SELECT r.rolsuper, r.rolbypassrls, "
                    "current_setting('default_transaction_read_only'), "
                    "has_database_privilege(current_user, current_database(), 'CREATE') "
                    "FROM pg_roles r WHERE r.rolname = current_user"
                )
            )
        ).one()
        is_super, bypass_rls, read_only, can_create = row
        if is_super or bypass_rls:
            raise PostureError(ConnectorErrorCode.ROLE_TOO_PRIVILEGED)
        if str(read_only).lower() != "on":
            raise PostureError(ConnectorErrorCode.NOT_READ_ONLY)
        warnings: list[str] = []
        if can_create:
            warnings.append(
                "The role can create objects in this database; a SELECT-only role is recommended."
            )
        return warnings

    async def _row_estimates(self, conn: AsyncConnection, schema: str) -> dict[str, int]:
        rows = await conn.execute(
            text(
                "SELECT c.relname, c.reltuples::bigint FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = :schema AND c.relkind IN ('r', 'p', 'm')"
            ),
            {"schema": schema},
        )
        return {str(name): int(est) for name, est in rows if est is not None and int(est) >= 0}

    def _parse_explain(self, rows: list[tuple[object, ...]]) -> ExplainResult:
        plan = rows[0][0] if rows and rows[0] else None
        if isinstance(plan, str):
            plan = json.loads(plan)  # asyncpg returns json columns as text
        estimated_rows: float | None = None
        estimated_cost: float | None = None
        if isinstance(plan, list) and plan and isinstance(plan[0], dict):
            top = plan[0].get("Plan", {})
            if isinstance(top, dict):
                estimated_rows = _as_float(top.get("Plan Rows"))
                estimated_cost = _as_float(top.get("Total Cost"))
        return ExplainResult(
            plan=plan, estimated_rows=estimated_rows, estimated_cost=estimated_cost
        )


def _as_float(value: object) -> float | None:
    return float(value) if isinstance(value, int | float) else None
