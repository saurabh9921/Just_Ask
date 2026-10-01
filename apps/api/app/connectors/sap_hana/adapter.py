"""SAP HANA adapter - FOUNDATION (compile only).

What exists: the connector type, type normalisation, identifier quoting and SQL
compilation through the ``sqlalchemy-hana`` dialect (so plans can be compiled,
validated and reviewed with ``/analytics/explain``-style tooling and tests).

What does not exist yet: execution and metadata discovery. SAP's ``hdbcli`` driver is
synchronous and SQLAlchemy has no asyncio HANA dialect; running it in worker threads
needs its own pooling, timeout (``SET 'STATEMENT_TIMEOUT'``/``query_timeout``) and
cancellation design (``ALTER SYSTEM CANCEL SESSION`` needs a privilege a read-only
user must not hold). Every I/O method therefore raises ``UnsupportedConnectorError``.
The connector type is disabled unless ``ENABLE_FOUNDATION_CONNECTORS=true``.

Planned read-only posture: a technical user with ``SELECT`` on calculation/reporting
views only, ``SET TRANSACTION READ ONLY`` per transaction, TLS (``encrypt=true``,
``sslValidateCertificate=true``). See docs/connector-development.md.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy import Select
from sqlalchemy.engine import Dialect

from app.connectors.base import DatabaseAdapter
from app.connectors.common.errors import UnsafeStatementError, UnsupportedConnectorError
from app.connectors.common.type_mapping import normalize_type
from app.connectors.common.types import (
    CanonicalType,
    CatalogSnapshot,
    ColumnInfo,
    ConnectionTestResult,
    ConnectorType,
    ExplainResult,
    ForeignKeyInfo,
    QueryResult,
    TableInfo,
)
from app.query.compiler import CompiledQuery, compile_plan
from app.query.validator import ResolvedPlan

DEFAULT_PORT = 39015


def _not_available() -> UnsupportedConnectorError:
    return UnsupportedConnectorError(
        "SAP HANA execution is not available yet (connector foundation).",
        details={"connector_type": ConnectorType.SAP_HANA.value},
    )


class SapHanaAdapter(DatabaseAdapter):
    connector_type = ConnectorType.SAP_HANA

    @property
    def dialect(self) -> Dialect:
        try:
            from sqlalchemy_hana.dialect import HANAHDBCLIDialect  # noqa: PLC0415 - optional
        except ImportError as exc:  # the "hana" extra is not installed
            raise _not_available() from exc
        return HANAHDBCLIDialect()

    def compile_query_plan(self, plan: ResolvedPlan) -> CompiledQuery:
        return compile_plan(plan, "hana")

    def normalize_type(self, raw_type: str) -> CanonicalType:
        name = raw_type.strip().lower()
        if name in {"seconddate"}:
            return CanonicalType.TIMESTAMP
        if name in {"alphanum", "shorttext", "st_geometry"}:
            return CanonicalType.STRING if name != "st_geometry" else CanonicalType.OTHER
        return normalize_type(raw_type)

    def quote_identifier(self, identifier: str) -> str:
        if not identifier or "\x00" in identifier or len(identifier) > 127:
            raise UnsafeStatementError(details={"reason": "INVALID_IDENTIFIER"})
        return '"' + identifier.replace('"', '""') + '"'

    async def close(self) -> None:
        return None

    async def test_connection(self) -> ConnectionTestResult:
        return ConnectionTestResult(
            ok=False,
            error_code="CONNECTOR_NOT_AVAILABLE",
            message="SAP HANA is a connector foundation; execution is not available yet.",
        )

    async def get_catalog(self, schemas: list[str] | None = None) -> CatalogSnapshot:
        raise _not_available()

    async def get_schemas(self) -> list[str]:
        raise _not_available()

    async def get_tables(self, schema: str) -> list[TableInfo]:
        raise _not_available()

    async def get_views(self, schema: str) -> list[TableInfo]:
        raise _not_available()

    async def get_columns(self, schema: str, table: str) -> list[ColumnInfo]:
        raise _not_available()

    async def get_primary_keys(self, schema: str, table: str) -> list[str]:
        raise _not_available()

    async def get_foreign_keys(self, schema: str, table: str) -> list[ForeignKeyInfo]:
        raise _not_available()

    async def execute_readonly(
        self,
        query: Select[Any],
        *,
        max_rows: int | None = None,
        timeout_seconds: float | None = None,
        query_id: str | None = None,
    ) -> QueryResult:
        raise _not_available()

    def stream_readonly(
        self,
        query: Select[Any],
        *,
        batch_size: int = 500,
        max_rows: int | None = None,
        timeout_seconds: float | None = None,
        query_id: str | None = None,
        ceiling: int | None = None,
    ) -> AsyncIterator[list[tuple[object, ...]]]:
        raise _not_available()

    async def explain_query(self, query: Select[Any]) -> ExplainResult:
        raise _not_available()

    async def cancel_query(self, query_id: str) -> bool:
        return False
