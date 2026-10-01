"""Shared implementation for SQLAlchemy-backed adapters (PostgreSQL, SQLite, MySQL, ...).

Dialect adapters override a handful of hooks:

* ``_create_engine(cfg)``      - URL, pool and driver settings (read-only session!);
                                 called for the primary and, if configured, the read replica
* ``_begin_readonly``          - per-transaction read-only / timeout statements
* ``_verify_read_only``        - posture checks for ``test_connection``
* ``_server_version``          - server version string
* ``_row_estimates``           - cheap row-count estimates for metadata
* ``_interrupt``               - abort a running statement (drivers that need it)
* ``_explain_sql``             - EXPLAIN syntax
* ``system_schemas``           - schemas never exposed
* ``driver_errors``            - driver exceptions SQLAlchemy leaves untranslated
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any, ClassVar

from sqlalchemy import Select, inspect
from sqlalchemy.engine import Connection, Dialect, Inspector
from sqlalchemy.engine.reflection import ObjectKind as SAObjectKind
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.connectors.base import DatabaseAdapter
from app.connectors.common.errors import (
    ConnectorBusyError,
    ConnectorCircuitOpenError,
    ConnectorError,
    ConnectorErrorCode,
    ConnectorUnavailableError,
    QueryCancelledError,
    QueryTimeoutError,
    UnsafeStatementError,
)
from app.connectors.common.type_mapping import normalize_type
from app.connectors.common.types import (
    CanonicalType,
    CatalogSnapshot,
    ColumnInfo,
    ConnectionConfig,
    ConnectionTestResult,
    ExplainResult,
    ForeignKeyInfo,
    ObjectKind,
    ObjectMetadata,
    QueryResult,
    ResultColumn,
    TableInfo,
)
from app.core.logging import get_logger
from app.core.metrics import (
    CONNECTOR_ERRORS,
    CONNECTOR_OPERATION_DURATION,
    CONNECTOR_OPERATIONS,
)
from app.core.resilience import CircuitBreaker, CircuitOpenError
from app.query.compiler import CompiledQuery, compile_plan
from app.query.validator import ResolvedPlan

logger = get_logger(__name__)

# Exception class names (anywhere in the cause chain) -> error code. Name-based so
# optional drivers need not be importable.
_ERROR_NAME_MAP: dict[str, ConnectorErrorCode] = {
    "InvalidPasswordError": ConnectorErrorCode.AUTH_FAILED,
    "InvalidAuthorizationSpecificationError": ConnectorErrorCode.AUTH_FAILED,
    "InvalidCatalogNameError": ConnectorErrorCode.DATABASE_NOT_FOUND,
    "InsufficientPrivilegeError": ConnectorErrorCode.PERMISSION_DENIED,
    "ReadOnlySQLTransactionError": ConnectorErrorCode.NOT_READ_ONLY,
    "SSLError": ConnectorErrorCode.TLS_ERROR,
    "SSLCertVerificationError": ConnectorErrorCode.TLS_ERROR,
    "ConnectionRefusedError": ConnectorErrorCode.HOST_UNREACHABLE,
    "gaierror": ConnectorErrorCode.HOST_UNREACHABLE,
    "ConnectionDoesNotExistError": ConnectorErrorCode.HOST_UNREACHABLE,
    "TimeoutError": ConnectorErrorCode.TIMEOUT,
    "QueryCanceledError": ConnectorErrorCode.TIMEOUT,
}

_TEST_MESSAGES: dict[ConnectorErrorCode, str] = {
    ConnectorErrorCode.AUTH_FAILED: "Authentication failed. Check the username and secret.",
    ConnectorErrorCode.DATABASE_NOT_FOUND: "The database does not exist or is not accessible.",
    ConnectorErrorCode.PERMISSION_DENIED: "The database role lacks required read permissions.",
    ConnectorErrorCode.TLS_ERROR: (
        "TLS failed: the server may not support encryption, or its certificate is not "
        "trusted for this host name. Check the database's SSL setup or choose another "
        "encryption mode."
    ),
    ConnectorErrorCode.HOST_UNREACHABLE: "The database host could not be reached.",
    ConnectorErrorCode.TIMEOUT: "The connection attempt timed out.",
    ConnectorErrorCode.ROLE_TOO_PRIVILEGED: (
        "The database role is too privileged. Use a dedicated read-only role."
    ),
    ConnectorErrorCode.NOT_READ_ONLY: "The session could not be made read-only.",
    ConnectorErrorCode.UNKNOWN: "The connection test failed.",
}


# Driver error numbers (MySQL/MariaDB errno as ``args[0]``).
_MYSQL_ERRNO: dict[int, ConnectorErrorCode] = {
    1045: ConnectorErrorCode.AUTH_FAILED,
    1049: ConnectorErrorCode.DATABASE_NOT_FOUND,
    1044: ConnectorErrorCode.PERMISSION_DENIED,
    1142: ConnectorErrorCode.PERMISSION_DENIED,
    1143: ConnectorErrorCode.PERMISSION_DENIED,
    1792: ConnectorErrorCode.NOT_READ_ONLY,
    2003: ConnectorErrorCode.HOST_UNREACHABLE,
    2005: ConnectorErrorCode.HOST_UNREACHABLE,
    2013: ConnectorErrorCode.HOST_UNREACHABLE,
    1317: ConnectorErrorCode.TIMEOUT,
    3024: ConnectorErrorCode.TIMEOUT,
}
# ODBC SQLSTATEs (SQL Server; pyodbc puts the SQLSTATE in ``args[0]``).
_SQLSTATE: dict[str, ConnectorErrorCode] = {
    "28000": ConnectorErrorCode.AUTH_FAILED,
    "HYT00": ConnectorErrorCode.TIMEOUT,
    "HYT01": ConnectorErrorCode.TIMEOUT,
    "08001": ConnectorErrorCode.HOST_UNREACHABLE,
    "08S01": ConnectorErrorCode.HOST_UNREACHABLE,
    "IM002": ConnectorErrorCode.UNKNOWN,  # ODBC driver not installed
}
# Oracle ``ORA-nnnnn`` codes.
_ORACLE: dict[str, ConnectorErrorCode] = {
    "ORA-01017": ConnectorErrorCode.AUTH_FAILED,
    "ORA-12541": ConnectorErrorCode.HOST_UNREACHABLE,
    "ORA-12514": ConnectorErrorCode.DATABASE_NOT_FOUND,
    "ORA-01031": ConnectorErrorCode.PERMISSION_DENIED,
    "ORA-01013": ConnectorErrorCode.TIMEOUT,
    "DPY-4011": ConnectorErrorCode.HOST_UNREACHABLE,
}


def _driver_code(exc: BaseException) -> ConnectorErrorCode | None:
    first = exc.args[0] if exc.args else None
    if isinstance(first, int) and not isinstance(first, bool):
        return _MYSQL_ERRNO.get(first)
    if isinstance(first, str):
        if first in _SQLSTATE:
            return _SQLSTATE[first]
        for prefix, code in _ORACLE.items():
            if first.startswith(prefix):
                return code
    return None


# Drivers report a refused TLS negotiation as a plain connection error; it is not an
# unreachable host (and must not trip the circuit breaker as one).
_TLS_REFUSED_MARKERS = ("rejected ssl upgrade", "does not support ssl", "ssl is not enabled")


def classify_error(exc: BaseException) -> ConnectorErrorCode:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        code = _ERROR_NAME_MAP.get(type(current).__name__) or _driver_code(current)
        if code is not None:
            return code
        if any(marker in str(current).lower() for marker in _TLS_REFUSED_MARKERS):
            return ConnectorErrorCode.TLS_ERROR
        if isinstance(current, OSError) and not isinstance(current, TimeoutError):
            return ConnectorErrorCode.HOST_UNREACHABLE
        nxt = getattr(current, "orig", None)
        current = nxt if isinstance(nxt, BaseException) else current.__cause__
    return ConnectorErrorCode.UNKNOWN


class PostureError(Exception):
    """Raised by ``_verify_read_only`` when the connection must be refused."""

    def __init__(self, code: ConnectorErrorCode) -> None:
        super().__init__(code.value)
        self.code = code


class SqlAlchemyAdapter(DatabaseAdapter):
    system_schemas: ClassVar[frozenset[str]] = frozenset()
    explain_prefix: ClassVar[str] = "EXPLAIN "
    # Some async dialects translate driver errors for statements but not while opening a
    # connection (SQLAlchemy's asyncpg dialect raises asyncpg's own exceptions there).
    # Unlisted, a wrong password surfaced as an unhandled 500 instead of AUTH_FAILED.
    driver_errors: ClassVar[tuple[type[Exception], ...]] = ()

    def __init__(self, config: ConnectionConfig) -> None:
        super().__init__(config)
        self._engine: AsyncEngine | None = None
        self._replica_engine: AsyncEngine | None = None
        # Per-connection concurrency limit (per API/worker process): at most
        # pool_max_size statements run at once; others wait up to queue_timeout_seconds.
        self._semaphore = asyncio.Semaphore(max(1, config.pool_max_size))
        self._breaker = CircuitBreaker(
            name=f"connection:{config.connection_id}",
            failure_threshold=max(1, config.breaker_failure_threshold),
            reset_seconds=config.breaker_reset_seconds,
        )
        self._running: dict[str, tuple[asyncio.Task[Any], AsyncConnection]] = {}
        self._cancel_requested: set[str] = set()

    # ------------------------------------------------------------- hooks
    def _create_engine(self, cfg: ConnectionConfig) -> AsyncEngine:
        """Engine for ``cfg`` (primary or replica host). Network adapters override this."""
        raise NotImplementedError

    def _build_engine(self) -> AsyncEngine:
        return self._create_engine(self.config)

    def _build_replica_engine(self) -> AsyncEngine | None:
        cfg = self.config
        if not cfg.read_replica_host:
            return None
        return self._create_engine(
            replace(cfg, host=cfg.read_replica_host, port=cfg.read_replica_port or cfg.port)
        )

    async def _begin_readonly(self, conn: AsyncConnection, timeout_seconds: float) -> None:
        """Run inside the freshly begun transaction before the query."""

    async def _verify_read_only(self, conn: AsyncConnection) -> list[str]:
        """Return warnings; raise PostureError to refuse the connection."""
        return []

    async def _server_version(self, conn: AsyncConnection) -> str | None:
        return None

    async def _row_estimates(self, conn: AsyncConnection, schema: str) -> dict[str, int]:
        return {}

    async def _interrupt(self, conn: AsyncConnection) -> None:
        """Abort the statement running on ``conn``. Default relies on task cancellation."""

    # ------------------------------------------------------------ engine
    @property
    def engine(self) -> AsyncEngine:
        if self._engine is None:
            self._engine = self._build_engine()
        return self._engine

    @property
    def query_engine(self) -> AsyncEngine:
        """Engine for analytical statements: the read replica when configured.

        Metadata discovery and connection tests use the primary (:attr:`engine`), whose
        catalog is authoritative; analytical load goes to the replica.
        """
        if self.config.read_replica_host:
            if self._replica_engine is None:
                self._replica_engine = self._build_replica_engine()
            if self._replica_engine is not None:
                return self._replica_engine
        return self.engine

    @property
    def _connector_failures(self) -> tuple[type[Exception], ...]:
        """Exceptions that mean the database (or the path to it) failed."""
        return (SQLAlchemyError, OSError, *self.driver_errors)

    @property
    def breaker(self) -> CircuitBreaker:
        return self._breaker

    async def close(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
        if self._replica_engine is not None:
            await self._replica_engine.dispose()
            self._replica_engine = None

    @asynccontextmanager
    async def _measure(self, operation: str) -> AsyncIterator[None]:
        started = time.perf_counter()
        outcome = "success"
        try:
            yield
        except BaseException:
            outcome = "error"
            raise
        finally:
            connector = self.connector_type.value
            CONNECTOR_OPERATIONS.labels(
                connector=connector, operation=operation, outcome=outcome
            ).inc()
            CONNECTOR_OPERATION_DURATION.labels(connector=connector, operation=operation).observe(
                time.perf_counter() - started
            )

    def _wrap(self, exc: BaseException) -> ConnectorError:
        code = classify_error(exc)
        CONNECTOR_ERRORS.labels(connector=self.connector_type.value, code=code.value).inc()
        logger.warning(
            "connector operation failed",
            extra={
                "connector": self.connector_type.value,
                "connection_id": str(self.config.connection_id),
                "error_code": code.value,
                "error_type": type(exc).__name__,
            },
        )
        if code in {ConnectorErrorCode.HOST_UNREACHABLE, ConnectorErrorCode.TIMEOUT}:
            return ConnectorUnavailableError(details={"error_code": code.value})
        return ConnectorError(details={"error_code": code.value})

    # ------------------------------------------------------ test connection
    async def test_connection(self) -> ConnectionTestResult:
        started = time.perf_counter()
        async with self._measure("test_connection"):
            try:
                async with asyncio.timeout(self.config.connect_timeout_seconds + 5):
                    async with self.engine.connect() as conn:
                        version = await self._server_version(conn)
                        warnings = await self._verify_read_only(conn)
                        await conn.rollback()
            except PostureError as exc:
                return ConnectionTestResult(
                    ok=False, error_code=exc.code.value, message=_TEST_MESSAGES[exc.code]
                )
            except self._connector_failures as exc:  # TimeoutError is an OSError
                code = classify_error(exc)
                logger.info(
                    "connection test failed",
                    extra={
                        "connection_id": str(self.config.connection_id),
                        "error_code": code.value,
                        "error_type": type(exc).__name__,
                    },
                )
                return ConnectionTestResult(
                    ok=False, error_code=code.value, message=_TEST_MESSAGES[code]
                )
        return ConnectionTestResult(
            ok=True,
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
            server_version=version,
            warnings=warnings,
        )

    # ------------------------------------------------------------ metadata
    async def _inspect[T](self, fn: Callable[[Inspector], T]) -> T:
        def run(sync_conn: Connection) -> T:
            return fn(inspect(sync_conn))

        try:
            async with self.engine.connect() as conn:
                return await conn.run_sync(run)
        except self._connector_failures as exc:
            raise self._wrap(exc) from exc

    def _visible(self, schema: str) -> bool:
        return schema.lower() not in self.system_schemas

    @staticmethod
    def _visible_object(name: str) -> bool:
        return True

    async def get_schemas(self) -> list[str]:
        async with self._measure("get_schemas"):
            names = await self._inspect(lambda i: i.get_schema_names())
        return sorted(n for n in names if self._visible(n))

    async def get_tables(self, schema: str) -> list[TableInfo]:
        async with self._measure("get_tables"):
            names = await self._inspect(lambda i: i.get_table_names(schema=schema))
        return [
            TableInfo(schema_name=schema, name=n, kind=ObjectKind.TABLE)
            for n in sorted(names)
            if self._visible_object(n)
        ]

    async def get_views(self, schema: str) -> list[TableInfo]:
        async with self._measure("get_views"):
            names = await self._inspect(lambda i: i.get_view_names(schema=schema))
        return [
            TableInfo(schema_name=schema, name=n, kind=ObjectKind.VIEW)
            for n in sorted(names)
            if self._visible_object(n)
        ]

    def _column_info(self, raw: dict[str, Any], ordinal: int) -> ColumnInfo:
        data_type = str(raw["type"])
        return ColumnInfo(
            name=str(raw["name"]),
            data_type=data_type,
            canonical_type=self.normalize_type(data_type),
            nullable=bool(raw.get("nullable", True)),
            ordinal=ordinal,
            comment=raw.get("comment"),
        )

    async def get_columns(self, schema: str, table: str) -> list[ColumnInfo]:
        async with self._measure("get_columns"):
            cols = await self._inspect(lambda i: i.get_columns(table, schema=schema))
        return [self._column_info(dict(c), idx) for idx, c in enumerate(cols, start=1)]

    async def get_primary_keys(self, schema: str, table: str) -> list[str]:
        async with self._measure("get_primary_keys"):
            pk = await self._inspect(lambda i: i.get_pk_constraint(table, schema=schema))
        return [str(c) for c in pk.get("constrained_columns") or []]

    @staticmethod
    def _fk_info(raw: dict[str, Any]) -> ForeignKeyInfo:
        return ForeignKeyInfo(
            name=raw.get("name"),
            columns=[str(c) for c in raw.get("constrained_columns", [])],
            referred_schema=raw.get("referred_schema"),
            referred_table=str(raw["referred_table"]),
            referred_columns=[str(c) for c in raw.get("referred_columns", [])],
        )

    async def get_foreign_keys(self, schema: str, table: str) -> list[ForeignKeyInfo]:
        async with self._measure("get_foreign_keys"):
            fks = await self._inspect(lambda i: i.get_foreign_keys(table, schema=schema))
        return [self._fk_info(dict(fk)) for fk in fks]

    async def get_catalog(self, schemas: list[str] | None = None) -> CatalogSnapshot:
        """Bulk reflection (one round of queries per schema, not per table)."""
        async with self._measure("get_catalog"):
            target = schemas if schemas is not None else await self.get_schemas()
            objects: list[ObjectMetadata] = []
            for schema in target:
                if not self._visible(schema):
                    continue
                objects.extend(await self._catalog_for_schema(schema))
            version: str | None = None
            try:
                async with self.engine.connect() as conn:
                    version = await self._server_version(conn)
            except self._connector_failures as exc:
                raise self._wrap(exc) from exc
        return CatalogSnapshot(
            database=self.config.database, server_version=version, objects=objects
        )

    async def _catalog_for_schema(self, schema: str) -> list[ObjectMetadata]:
        def reflect(sync_conn: Connection) -> dict[str, Any]:
            insp = inspect(sync_conn)
            kinds: dict[str, ObjectKind] = {}
            for name in insp.get_table_names(schema=schema):
                kinds[name] = ObjectKind.TABLE
            for name in insp.get_view_names(schema=schema):
                kinds[name] = ObjectKind.VIEW
            try:
                for name in insp.get_materialized_view_names(schema=schema):
                    kinds[name] = ObjectKind.MATERIALIZED_VIEW
            except NotImplementedError:
                pass
            if not kinds:
                return {"kinds": {}}
            any_kind = SAObjectKind.ANY
            return {
                "kinds": kinds,
                "columns": insp.get_multi_columns(schema=schema, kind=any_kind),
                "pks": insp.get_multi_pk_constraint(schema=schema, kind=any_kind),
                "fks": insp.get_multi_foreign_keys(schema=schema, kind=any_kind),
                "comments": _optional(
                    lambda: insp.get_multi_table_comment(schema=schema, kind=any_kind)
                ),
            }

        try:
            async with self.engine.connect() as conn:
                data = await conn.run_sync(reflect)
                estimates = await self._row_estimates(conn, schema) if data["kinds"] else {}
        except self._connector_failures as exc:
            raise self._wrap(exc) from exc

        results: list[ObjectMetadata] = []
        keys_by_name = _keys_by_name(data.get("columns", {}), schema)
        for name, kind in sorted(data["kinds"].items()):
            if not self._visible_object(name):
                continue
            key = keys_by_name.get(name)
            columns = data["columns"].get(key, []) if key is not None else []
            pk = data["pks"].get(key, {}) if key is not None else {}
            fks = data["fks"].get(key, []) if key is not None else []
            comment = data["comments"].get(key, {}) if key is not None else {}
            results.append(
                ObjectMetadata(
                    schema_name=schema,
                    name=name,
                    kind=kind,
                    columns=[self._column_info(dict(c), i) for i, c in enumerate(columns, 1)],
                    primary_key=[str(c) for c in (pk or {}).get("constrained_columns") or []],
                    foreign_keys=[self._fk_info(dict(fk)) for fk in fks],
                    row_estimate=estimates.get(name),
                    comment=(comment or {}).get("text"),
                )
            )
        return results

    # ----------------------------------------------------------- execution
    def _guard(self, query: Select[Any]) -> None:
        if not isinstance(query, Select):
            raise UnsafeStatementError(details={"reason": "ONLY_SELECT_ALLOWED"})
        if query._for_update_arg is not None:
            raise UnsafeStatementError(details={"reason": "LOCKING_READ_NOT_ALLOWED"})

    def _cap(self, max_rows: int | None) -> int:
        cap = self.config.max_rows
        return max(1, min(max_rows, cap)) if max_rows is not None else cap

    def _timeout(self, timeout_seconds: float | None) -> float:
        ceiling = float(self.config.query_timeout_seconds)
        return max(0.1, min(timeout_seconds, ceiling)) if timeout_seconds is not None else ceiling

    @staticmethod
    def _limited(query: Select[Any], cap: int) -> Select[Any]:
        existing = query._limit  # int | None
        if existing is not None and existing <= cap:
            return query
        return query.limit(cap + 1)  # one extra row detects truncation

    @asynccontextmanager
    async def _readonly_connection(
        self, timeout_s: float, query_id: str | None
    ) -> AsyncIterator[AsyncConnection]:
        try:
            await asyncio.wait_for(self._semaphore.acquire(), self.config.queue_timeout_seconds)
        except TimeoutError as exc:
            CONNECTOR_OPERATIONS.labels(
                connector=self.connector_type.value, operation="queue", outcome="busy"
            ).inc()
            raise ConnectorBusyError() from exc
        try:
            async with self.query_engine.connect() as conn:
                task = asyncio.current_task()
                if query_id is not None and task is not None:
                    self._running[query_id] = (task, conn)
                loop = asyncio.get_running_loop()
                watchdog = loop.call_later(
                    timeout_s, lambda: loop.create_task(self._interrupt(conn))
                )
                try:
                    await self._begin_readonly(conn, timeout_s)
                    yield conn
                finally:
                    watchdog.cancel()
                    if query_id is not None:
                        self._running.pop(query_id, None)
                    if conn.in_transaction():
                        await conn.rollback()
        finally:
            self._semaphore.release()

    async def _run[T](
        self,
        operation: str,
        query_id: str | None,
        timeout_s: float,
        coro_fn: Callable[[], Awaitable[T]],
    ) -> T:
        self._before_statement()
        try:
            async with self._measure(operation), asyncio.timeout(timeout_s + 1.0):
                result: T = await coro_fn()
        except TimeoutError as exc:
            self._breaker.record_failure()
            raise QueryTimeoutError() from exc
        except asyncio.CancelledError:
            if query_id is not None and query_id in self._cancel_requested:
                self._cancel_requested.discard(query_id)
                task = asyncio.current_task()
                if task is not None:
                    task.uncancel()
                raise QueryCancelledError() from None
            raise
        except self._connector_failures as exc:
            code = classify_error(exc)
            if code is ConnectorErrorCode.TIMEOUT or _is_interrupt(exc):
                if query_id is not None and query_id in self._cancel_requested:
                    self._cancel_requested.discard(query_id)
                    raise QueryCancelledError() from exc
                self._breaker.record_failure()
                raise QueryTimeoutError() from exc
            self._record_outcome(code)
            raise self._wrap(exc) from exc
        else:
            self._breaker.record_success()
            return result

    def _before_statement(self) -> None:
        try:
            self._breaker.before_call()
        except CircuitOpenError as exc:
            CONNECTOR_OPERATIONS.labels(
                connector=self.connector_type.value, operation="breaker", outcome="open"
            ).inc()
            raise ConnectorCircuitOpenError() from exc

    def _record_outcome(self, code: ConnectorErrorCode) -> None:
        """Unreachable databases open the breaker; any other error means the database
        answered (e.g. a permission error), so it counts as healthy."""
        if code in {ConnectorErrorCode.HOST_UNREACHABLE, ConnectorErrorCode.TIMEOUT}:
            self._breaker.record_failure()
        else:
            self._breaker.record_success()

    async def execute_readonly(
        self,
        query: Select[Any],
        *,
        max_rows: int | None = None,
        timeout_seconds: float | None = None,
        query_id: str | None = None,
    ) -> QueryResult:
        self._guard(query)
        cap = self._cap(max_rows)
        timeout = self._timeout(timeout_seconds)
        statement = self._limited(query, cap)
        started = time.perf_counter()

        async def work() -> tuple[list[str], list[tuple[object, ...]]]:
            rows: list[tuple[object, ...]] = []
            async with self._readonly_connection(timeout, query_id) as conn:
                result = await conn.stream(statement)
                try:
                    keys = list(result.keys())
                    async for partition in result.partitions(min(500, cap + 1)):
                        rows.extend(tuple(r) for r in partition)
                        if len(rows) > cap:
                            break
                finally:
                    await result.close()
            return keys, rows

        keys, rows = await self._run("execute_readonly", query_id, timeout, work)
        truncated = len(rows) > cap
        rows = rows[:cap]
        return QueryResult(
            columns=[ResultColumn(name=k) for k in keys],
            rows=rows,
            row_count=len(rows),
            truncated=truncated,
            elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
        )

    async def stream_readonly(
        self,
        query: Select[Any],
        *,
        batch_size: int = 500,
        max_rows: int | None = None,
        timeout_seconds: float | None = None,
        query_id: str | None = None,
        ceiling: int | None = None,
    ) -> AsyncIterator[list[tuple[object, ...]]]:
        """Row batches; ``ceiling`` replaces the connection's ``max_rows`` as the hard
        limit (background exports - the caller enforces its own platform maximum)."""
        self._guard(query)
        limit = max(1, ceiling) if ceiling is not None else self.config.max_rows
        cap = max(1, min(max_rows, limit)) if max_rows is not None else limit
        timeout = self._timeout(timeout_seconds)
        statement = query.limit(cap) if query._limit is None or query._limit > cap else query
        emitted = 0
        self._before_statement()
        try:
            async with (
                asyncio.timeout(timeout + 1.0),
                self._readonly_connection(timeout, query_id) as conn,
            ):
                result = await conn.stream(statement)
                try:
                    async for partition in result.partitions(max(1, batch_size)):
                        batch = [tuple(r) for r in partition][: cap - emitted]
                        emitted += len(batch)
                        if batch:
                            yield batch
                        if emitted >= cap:
                            break
                finally:
                    await result.close()
        except TimeoutError as exc:
            self._breaker.record_failure()
            raise QueryTimeoutError() from exc
        except self._connector_failures as exc:
            code = classify_error(exc)
            if _is_interrupt(exc) or code is ConnectorErrorCode.TIMEOUT:
                self._breaker.record_failure()
                raise QueryTimeoutError() from exc
            self._record_outcome(code)
            raise self._wrap(exc) from exc
        else:
            self._breaker.record_success()

    async def explain_query(self, query: Select[Any]) -> ExplainResult:
        self._guard(query)
        timeout = self._timeout(None)

        async def work() -> list[tuple[object, ...]]:
            async with self._readonly_connection(timeout, None) as conn:
                sql, args = _driver_statement(query, conn.dialect)
                result = await conn.exec_driver_sql(self.explain_prefix + sql, args)
                return [tuple(r) for r in result.fetchall()]

        rows = await self._run("explain_query", None, timeout, work)
        return self._parse_explain(rows)

    def _parse_explain(self, rows: list[tuple[object, ...]]) -> ExplainResult:
        return ExplainResult(plan=[list(r) for r in rows])

    async def cancel_query(self, query_id: str) -> bool:
        running = self._running.get(query_id)
        if running is None:
            return False
        task, conn = running
        self._cancel_requested.add(query_id)
        await self._interrupt(conn)
        task.cancel()
        return True

    # ------------------------------------------------------------- dialect
    def normalize_type(self, raw_type: str) -> CanonicalType:
        return normalize_type(raw_type)

    @property
    def dialect(self) -> Dialect:
        return self.engine.dialect

    def compile_query_plan(self, plan: ResolvedPlan) -> CompiledQuery:
        return compile_plan(plan, self.engine.dialect.name)

    def quote_identifier(self, identifier: str) -> str:
        if not identifier or "\x00" in identifier or len(identifier) > 128:
            raise UnsafeStatementError(details={"reason": "INVALID_IDENTIFIER"})
        return str(self.engine.dialect.identifier_preparer.quote_identifier(identifier))


def _driver_statement(
    query: Select[Any], dialect: Dialect
) -> tuple[str, tuple[Any, ...] | dict[str, Any]]:
    """Driver-level SQL + arguments for ``query`` (used to prefix EXPLAIN).

    Expanding (IN) parameters are rendered, and each value goes through its
    type's bind processor exactly as a normal execution would (e.g. dates to ISO
    strings on SQLite). Values stay bound - never rendered into the SQL text.
    """
    compiled = query.compile(dialect=dialect, compile_kwargs={"render_postcompile": True})
    params = compiled.construct_params()

    def value(name: str) -> Any:
        bind = compiled.binds.get(name)
        if bind is None:  # an expanded IN element: "<name>_<n>"
            bind = compiled.binds.get(re.sub(r"_\d+$", "", name))
        raw = params[name]
        if bind is None:
            return raw
        processor = bind.type.dialect_impl(dialect).bind_processor(dialect)
        return processor(raw) if processor is not None else raw

    if compiled.positional:
        return compiled.string, tuple(value(k) for k in compiled.positiontup or [])
    return compiled.string, {k: value(k) for k in params}


def _keys_by_name(
    mapping: Iterable[tuple[str | None, str]], schema: str
) -> dict[str, tuple[str | None, str]]:
    return {key[1]: key for key in mapping if key[0] is None or key[0] == schema}


def _optional(fn: Callable[[], dict[Any, Any]]) -> dict[Any, Any]:
    """Reflection features some dialects lack (e.g. table comments on SQLite)."""
    try:
        return fn()
    except NotImplementedError:
        return {}


_INTERRUPT_MARKERS = (
    "interrupted",  # SQLite, MySQL 1317
    "canceling statement",  # PostgreSQL
    "max_execution_time",  # MySQL 3024
    "operation canceled",  # SQL Server (ODBC)
    "ora-01013",  # Oracle user requested cancel
)


def _is_interrupt(exc: BaseException) -> bool:
    text = str(getattr(exc, "orig", exc)).lower()
    return any(marker in text for marker in _INTERRUPT_MARKERS)
