"""MySQL / MariaDB adapter (aiomysql).

Read-only posture, in addition to the customer's SELECT-only account:
* every session starts with ``SET SESSION TRANSACTION READ ONLY`` (``init_command``)
  and every analytical transaction runs ``SET TRANSACTION READ ONLY`` plus a statement
  time limit (``max_execution_time`` on MySQL, ``max_statement_time`` on MariaDB);
* ``test_connection`` refuses accounts with global write/admin privileges
  (``ALL PRIVILEGES``, ``SUPER``, write/DDL grants on ``*.*``) and warns about
  write grants on the target database;
* a timed-out or cancelled statement is stopped server-side with ``KILL QUERY`` on the
  statement's own connection id (an account may always kill its own queries).

In MySQL a *schema* is a *database*: metadata discovery is limited to the configured
database (``schema_name`` if set, else ``database_name``).
"""

from __future__ import annotations

import json
import re
import ssl
from typing import Any, ClassVar

from sqlalchemy import URL, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from app.connectors.common.errors import ConnectionConfigError, ConnectorErrorCode
from app.connectors.common.sqlalchemy_adapter import PostureError, SqlAlchemyAdapter
from app.connectors.common.types import (
    ConnectionConfig,
    ConnectorType,
    ExplainResult,
    TlsMode,
)

DEFAULT_PORT = 3306

_WRITE_PRIVILEGES = re.compile(
    r"\b(ALL PRIVILEGES|SUPER|INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|INDEX|GRANT OPTION"
    r"|FILE|SHUTDOWN|PROCESS|RELOAD|EXECUTE|TRIGGER|EVENT)\b"
)
_GRANT_TARGET = re.compile(r"\bON\s+(\S+)\s+TO\b", re.IGNORECASE)


def build_mysql_ssl(tls_mode: TlsMode, ca_pem: str | None) -> ssl.SSLContext | None:
    """aiomysql ``ssl`` argument (``None`` = plaintext; PREFER cannot be expressed)."""
    if tls_mode in {TlsMode.DISABLE, TlsMode.PREFER}:
        return None
    context = ssl.create_default_context(cadata=ca_pem) if ca_pem else ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    if tls_mode is TlsMode.REQUIRE:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    elif tls_mode is TlsMode.VERIFY_CA:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_REQUIRED
    return context


def assess_grants(grants: list[str], database: str | None) -> list[str]:
    """Raise for global write/admin grants; return warnings for database-level ones."""
    warnings: list[str] = []
    for grant in grants:
        upper = grant.upper()
        target_match = _GRANT_TARGET.search(grant)
        target = target_match.group(1).replace("`", "").upper() if target_match else ""
        privileges = upper.split(" ON ", 1)[0]
        if not _WRITE_PRIVILEGES.search(privileges):
            continue
        if target in {"*.*", "*"}:
            raise PostureError(ConnectorErrorCode.ROLE_TOO_PRIVILEGED)
        db = (database or "").upper()
        if db and target.startswith(f"{db}."):
            warnings.append(
                "The account can modify objects in this database; a SELECT-only account "
                "is recommended."
            )
    return sorted(set(warnings))


def _is_mariadb(conn: AsyncConnection) -> bool:
    return bool(getattr(conn.dialect, "is_mariadb", False))


class MySQLAdapter(SqlAlchemyAdapter):
    connector_type = ConnectorType.MYSQL
    system_schemas: ClassVar[frozenset[str]] = frozenset(
        {"information_schema", "mysql", "performance_schema", "sys"}
    )
    explain_prefix: ClassVar[str] = "EXPLAIN FORMAT=JSON "

    def __init__(self, config: ConnectionConfig) -> None:
        super().__init__(config)
        self._thread_ids: dict[int, int] = {}

    def _create_engine(self, cfg: ConnectionConfig) -> AsyncEngine:
        if not cfg.host or not cfg.database or not cfg.username:
            raise ConnectionConfigError(
                "Host, database and username are required.",
                details={"reason": "MISSING_FIELDS"},
            )
        url = URL.create(
            "mysql+aiomysql",
            username=cfg.username,
            password=cfg.password.get_secret_value() if cfg.password else None,
            host=cfg.host,
            port=cfg.port or DEFAULT_PORT,
            database=cfg.database,
            query={"charset": "utf8mb4"},
        )
        ca = cfg.tls_ca_pem.get_secret_value() if cfg.tls_ca_pem else None
        connect_args: dict[str, Any] = {
            "connect_timeout": cfg.connect_timeout_seconds,
            "init_command": "SET SESSION TRANSACTION READ ONLY",
            "program_name": "erp-insight-ai",
        }
        ssl_context = build_mysql_ssl(cfg.tls_mode, ca)
        if ssl_context is not None:
            connect_args["ssl"] = ssl_context
        return create_async_engine(
            url,
            connect_args=connect_args,
            pool_size=max(1, cfg.pool_max_size),
            max_overflow=1,  # one spare connection for KILL QUERY
            pool_timeout=cfg.connect_timeout_seconds,
            pool_recycle=1800,
            pool_pre_ping=True,
        )

    @property
    def _database(self) -> str | None:
        return self.config.schema or self.config.database

    async def get_schemas(self) -> list[str]:
        database = self._database
        return [database] if database else await super().get_schemas()

    async def _begin_readonly(self, conn: AsyncConnection, timeout_seconds: float) -> None:
        await conn.execute(text("SET TRANSACTION READ ONLY"))
        # Integers computed by us - not user input - so inlining is safe.
        if _is_mariadb(conn):
            await conn.execute(text(f"SET SESSION max_statement_time = {float(timeout_seconds)}"))
        else:
            await conn.execute(
                text(f"SET SESSION max_execution_time = {int(timeout_seconds * 1000)}")
            )
        thread_id = (await conn.execute(text("SELECT CONNECTION_ID()"))).scalar()
        if thread_id is not None:
            self._thread_ids[id(conn)] = int(thread_id)

    async def _interrupt(self, conn: AsyncConnection) -> None:
        thread_id = self._thread_ids.pop(id(conn), None)
        if thread_id is None:
            return
        async with self.query_engine.connect() as killer:
            await killer.execute(text(f"KILL QUERY {int(thread_id)}"))

    async def _server_version(self, conn: AsyncConnection) -> str | None:
        value = (await conn.execute(text("SELECT VERSION()"))).scalar()
        return str(value) if value is not None else None

    async def _verify_read_only(self, conn: AsyncConnection) -> list[str]:
        variable = (
            "@@session.tx_read_only" if _is_mariadb(conn) else "@@session.transaction_read_only"
        )
        read_only = (await conn.execute(text(f"SELECT {variable}"))).scalar()
        if int(read_only or 0) != 1:
            raise PostureError(ConnectorErrorCode.NOT_READ_ONLY)
        grants = [str(row[0]) for row in await conn.execute(text("SHOW GRANTS"))]
        return assess_grants(grants, self._database)

    async def _row_estimates(self, conn: AsyncConnection, schema: str) -> dict[str, int]:
        rows = await conn.execute(
            text(
                "SELECT TABLE_NAME, TABLE_ROWS FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = :schema AND TABLE_ROWS IS NOT NULL"
            ),
            {"schema": schema},
        )
        return {str(name): int(est) for name, est in rows if est is not None and int(est) >= 0}

    def _parse_explain(self, rows: list[tuple[object, ...]]) -> ExplainResult:
        raw = rows[0][0] if rows and rows[0] else None
        plan = json.loads(raw) if isinstance(raw, str) else raw
        cost: float | None = None
        if isinstance(plan, dict):
            block = plan.get("query_block", {})
            info = block.get("cost_info", {}) if isinstance(block, dict) else {}
            value = info.get("query_cost") if isinstance(info, dict) else None
            try:
                cost = float(value) if value is not None else None
            except (TypeError, ValueError):
                cost = None
        return ExplainResult(plan=plan, estimated_rows=None, estimated_cost=cost)
