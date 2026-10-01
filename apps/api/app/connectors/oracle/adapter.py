"""Oracle Database adapter - FOUNDATION (python-oracledb, thin mode, async).

Status: implemented against the shared SQLAlchemy adapter and covered by compile/
validation tests, but not yet verified against a live Oracle instance, so the
connector type is disabled unless ``ENABLE_FOUNDATION_CONNECTORS=true``.

Read-only posture:
* every analytical transaction starts with ``SET TRANSACTION READ ONLY``;
* ``call_timeout`` bounds every round trip;
* ``test_connection`` refuses sessions with DBA/export roles or ``* ANY *`` write
  privileges and warns when the user can create objects.

``EXPLAIN PLAN`` writes into ``PLAN_TABLE`` (not allowed in a read-only transaction), so
cost estimation uses ``ALL_TABLES.NUM_ROWS`` statistics instead.
"""

from __future__ import annotations

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

DEFAULT_PORT = 1521

ORACLE_SYSTEM_SCHEMAS = frozenset(
    {
        "sys",
        "system",
        "outln",
        "xdb",
        "mdsys",
        "ctxsys",
        "ordsys",
        "ordplugins",
        "dbsnmp",
        "appqossys",
        "audsys",
        "gsmadmin_internal",
        "lbacsys",
        "ojvmsys",
        "wmsys",
        "dvsys",
        "dvf",
        "olapsys",
        "anonymous",
        "xs$null",
    }
)


class OracleAdapter(SqlAlchemyAdapter):
    connector_type = ConnectorType.ORACLE
    system_schemas: ClassVar[frozenset[str]] = ORACLE_SYSTEM_SCHEMAS

    def _create_engine(self, cfg: ConnectionConfig) -> AsyncEngine:
        if not cfg.host or not cfg.database or not cfg.username:
            raise ConnectionConfigError(
                "Host, service name (database_name) and username are required.",
                details={"reason": "MISSING_FIELDS"},
            )
        url = URL.create(
            "oracle+oracledb",
            username=cfg.username,
            password=cfg.password.get_secret_value() if cfg.password else None,
            host=cfg.host,
            port=cfg.port or DEFAULT_PORT,
            query={"service_name": cfg.database},
        )
        connect_args: dict[str, Any] = {
            "tcp_connect_timeout": cfg.connect_timeout_seconds,
            "program": "erp-insight-ai",
        }
        if cfg.tls_mode is not TlsMode.DISABLE:
            connect_args["protocol"] = "tcps"
            connect_args["ssl_server_dn_match"] = cfg.tls_mode is TlsMode.VERIFY_FULL
        engine = create_async_engine(
            url,
            connect_args=connect_args,
            pool_size=max(1, cfg.pool_max_size),
            max_overflow=0,
            pool_timeout=cfg.connect_timeout_seconds,
            pool_recycle=1800,
            pool_pre_ping=True,
        )
        call_timeout_ms = int((cfg.query_timeout_seconds + 5) * 1000)

        @event.listens_for(engine.sync_engine, "connect")
        def _set_call_timeout(dbapi_connection: Any, _record: Any) -> None:
            raw = getattr(dbapi_connection, "driver_connection", None)
            if raw is not None and hasattr(raw, "call_timeout"):
                raw.call_timeout = call_timeout_ms

        return engine

    def _visible(self, schema: str) -> bool:
        return schema.lower() not in self.system_schemas and not schema.lower().startswith("apex_")

    async def get_schemas(self) -> list[str]:
        if self.config.schema:
            return [self.config.schema]
        return await super().get_schemas()

    async def _begin_readonly(self, conn: AsyncConnection, timeout_seconds: float) -> None:
        await conn.execute(text("SET TRANSACTION READ ONLY"))

    async def _server_version(self, conn: AsyncConnection) -> str | None:
        value = (
            await conn.execute(text("SELECT version_full FROM product_component_version"))
        ).scalar()
        return str(value) if value is not None else None

    async def _verify_read_only(self, conn: AsyncConnection) -> list[str]:
        roles = {
            str(r[0]).upper() for r in await conn.execute(text("SELECT role FROM session_roles"))
        }
        if roles & {"DBA", "IMP_FULL_DATABASE", "EXP_FULL_DATABASE", "DATAPUMP_EXP_FULL_DATABASE"}:
            raise PostureError(ConnectorErrorCode.ROLE_TOO_PRIVILEGED)
        privileges = {
            str(r[0]).upper()
            for r in await conn.execute(text("SELECT privilege FROM session_privs"))
        }
        dangerous = {
            p
            for p in privileges
            if " ANY " in f" {p} " and not p.startswith(("SELECT ANY", "READ ANY"))
        }
        if dangerous or "SYSDBA" in privileges:
            raise PostureError(ConnectorErrorCode.ROLE_TOO_PRIVILEGED)
        if privileges & {"CREATE TABLE", "CREATE VIEW", "CREATE PROCEDURE"}:
            return ["The user can create objects; a read-only user is recommended."]
        return []

    async def _row_estimates(self, conn: AsyncConnection, schema: str) -> dict[str, int]:
        rows = await conn.execute(
            text(
                "SELECT table_name, num_rows FROM all_tables "
                "WHERE owner = UPPER(:schema) AND num_rows IS NOT NULL"
            ),
            {"schema": schema},
        )
        return {str(name).lower(): int(est) for name, est in rows if est is not None}

    async def explain_query(self, query: Any) -> ExplainResult:
        self._guard(query)
        raise UnsupportedConnectorError(
            "EXPLAIN PLAN needs a writable PLAN_TABLE; row statistics are used instead.",
            details={"connector_type": self.connector_type.value},
        )
