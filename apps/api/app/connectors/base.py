"""The ``DatabaseAdapter`` contract every connector implements.

Rules every adapter must uphold (verified by tests/contract):

* Read-only at the session level wherever the engine supports it, in addition to
  the read-only database role the customer provisions.
* ``execute_readonly`` accepts only a SQLAlchemy ``Select`` built by our compiler -
  never a raw SQL string - rejects ``SELECT ... FOR UPDATE``, applies the row cap,
  enforces the timeout and never materialises more than ``max_rows + 1`` rows.
* Metadata methods return structure only, never data values.
* Errors are mapped to ``app.connectors.common.errors`` with generic messages.
* One connection pool per adapter instance; ``close()`` disposes it.

* ``compile_query_plan`` turns a validated, resolved plan into a dialect-specific
  ``Select`` (bound parameters only, tenant row filters always applied).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any, ClassVar

from sqlalchemy import Select
from sqlalchemy.engine import Dialect

from app.connectors.common.types import (
    CanonicalType,
    CatalogSnapshot,
    ColumnInfo,
    ConnectionConfig,
    ConnectionTestResult,
    ConnectorType,
    ExplainResult,
    ForeignKeyInfo,
    QueryResult,
    TableInfo,
)
from app.query.compiler import CompiledQuery
from app.query.validator import ResolvedPlan


class DatabaseAdapter(ABC):
    connector_type: ClassVar[ConnectorType]

    def __init__(self, config: ConnectionConfig) -> None:
        self.config = config

    # ------------------------------------------------------------ lifecycle
    @abstractmethod
    async def test_connection(self) -> ConnectionTestResult:
        """Connect, verify read-only posture, report server version. Never raises for
        database-side failures; returns ``ok=False`` with an ``error_code``."""

    @abstractmethod
    async def close(self) -> None:
        """Dispose the connection pool."""

    # ------------------------------------------------------------- metadata
    @abstractmethod
    async def get_catalog(self, schemas: list[str] | None = None) -> CatalogSnapshot: ...

    @abstractmethod
    async def get_schemas(self) -> list[str]: ...

    @abstractmethod
    async def get_tables(self, schema: str) -> list[TableInfo]: ...

    @abstractmethod
    async def get_views(self, schema: str) -> list[TableInfo]: ...

    @abstractmethod
    async def get_columns(self, schema: str, table: str) -> list[ColumnInfo]: ...

    @abstractmethod
    async def get_primary_keys(self, schema: str, table: str) -> list[str]: ...

    @abstractmethod
    async def get_foreign_keys(self, schema: str, table: str) -> list[ForeignKeyInfo]: ...

    # ------------------------------------------------------------ execution
    @abstractmethod
    async def execute_readonly(
        self,
        query: Select[Any],
        *,
        max_rows: int | None = None,
        timeout_seconds: float | None = None,
        query_id: str | None = None,
    ) -> QueryResult: ...

    @abstractmethod
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
        """Yield row batches without holding the full result in memory. ``ceiling``
        overrides the connection's ``max_rows`` hard limit (background exports)."""

    @abstractmethod
    async def explain_query(self, query: Select[Any]) -> ExplainResult: ...

    @abstractmethod
    async def cancel_query(self, query_id: str) -> bool:
        """Cancel a running query started with ``query_id``. Returns False if unknown."""

    # -------------------------------------------------------------- dialect
    @abstractmethod
    def normalize_type(self, raw_type: str) -> CanonicalType: ...

    @abstractmethod
    def quote_identifier(self, identifier: str) -> str: ...

    @abstractmethod
    def compile_query_plan(self, plan: ResolvedPlan) -> CompiledQuery:
        """Compile a resolved plan for this dialect (see ``app.query.compiler``)."""

    @property
    @abstractmethod
    def dialect(self) -> Dialect:
        """SQLAlchemy dialect used to render statements (display, SQL validation)."""
