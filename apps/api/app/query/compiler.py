"""Compile a :class:`ResolvedPlan` into a dialect-specific SQLAlchemy ``Select``.

Guarantees (asserted again by ``sql_validator`` on the rendered SQL):
* every value (filters, periods, tenant row filters) is a bound parameter;
* identifiers come only from the semantic layer / bindings and are quoted by the
  dialect's identifier preparer (``table``/``column`` constructs);
* explicit projections only (never ``*``); joins only on approved keys (ON clause);
* tenant row filters are always present; a LIMIT is always present;
* the only literal SQL fragments are constants chosen from fixed tables below
  (time-bucket formats), never derived from user or model input.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Select, and_, distinct, func, literal_column, select
from sqlalchemy.dialects.mssql import DATE as MSSQL_DATE
from sqlalchemy.engine import Dialect
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.selectable import FromClause, NamedFromClause

from app.connectors.common.errors import UnsupportedConnectorError
from app.connectors.common.types import CanonicalType
from app.query.normalizer import Window
from app.query.plans import FilterOp
from app.query.validator import (
    ResolvedDimension,
    ResolvedFilter,
    ResolvedMeasure,
    ResolvedPlan,
)
from app.semantic.models import TimeGrain, ValueFormat

SA_TYPES: dict[CanonicalType, Any] = {
    CanonicalType.INTEGER: sa.Integer(),
    CanonicalType.DECIMAL: sa.Numeric(),
    CanonicalType.FLOAT: sa.Float(),
    CanonicalType.DATE: sa.Date(),
    CanonicalType.TIMESTAMP: sa.DateTime(),
    CanonicalType.BOOLEAN: sa.Boolean(),
}

# Fixed, enum-keyed SQL fragments (never user input). Constants are literal columns, not
# bound parameters: the bucket expression is repeated in GROUP BY, and SQL Server (server-
# side parameters) only matches identical expressions.
_PG_GRAIN: dict[TimeGrain, ColumnElement[Any]] = {
    g: literal_column(f"'{g.value}'") for g in TimeGrain
}
_SQLITE_FORMAT: dict[TimeGrain, ColumnElement[Any]] = {
    TimeGrain.MONTH: literal_column("'%Y-%m-01'"),
    TimeGrain.YEAR: literal_column("'%Y-01-01'"),
}
_MYSQL_FORMAT: dict[TimeGrain, ColumnElement[Any]] = {
    TimeGrain.MONTH: literal_column("'%Y-%m-01'"),
    TimeGrain.YEAR: literal_column("'%Y-01-01'"),
}
_ORACLE_FORMAT: dict[TimeGrain, ColumnElement[Any]] = {
    TimeGrain.WEEK: literal_column("'IW'"),
    TimeGrain.MONTH: literal_column("'MM'"),
    TimeGrain.QUARTER: literal_column("'Q'"),
    TimeGrain.YEAR: literal_column("'YYYY'"),
}


def _n(value: int) -> ColumnElement[Any]:
    return literal_column(str(int(value)))


def _sqlite_bucket(col: ColumnElement[Any], grain: TimeGrain) -> ColumnElement[Any]:
    if grain is TimeGrain.DAY:
        return func.date(col)
    if grain is TimeGrain.WEEK:  # Monday of the ISO week
        return func.date(col, literal_column("'weekday 0'"), literal_column("'-6 days'"))
    if grain is TimeGrain.QUARTER:
        month = sa.cast(func.strftime(literal_column("'%m'"), col), sa.Integer())
        return func.printf(
            literal_column("'%s-%02d-01'"),
            func.strftime(literal_column("'%Y'"), col),
            # ``//`` renders integer division (``/`` is true division in SQLAlchemy 2).
            (month - _n(1)) // _n(3) * _n(3) + _n(1),
        )
    return func.strftime(_SQLITE_FORMAT[grain], col)


def _mysql_bucket(col: ColumnElement[Any], grain: TimeGrain) -> ColumnElement[Any]:
    if grain is TimeGrain.DAY:
        return func.date(col)
    if grain is TimeGrain.WEEK:  # WEEKDAY(): Monday = 0
        return func.subdate(func.date(col), func.weekday(col))
    if grain is TimeGrain.QUARTER:
        month = (func.quarter(col) - _n(1)) * _n(3) + _n(1)
        return sa.cast(
            func.concat(
                func.year(col),
                literal_column("'-'"),
                func.lpad(month, _n(2), literal_column("'0'")),
                literal_column("'-01'"),
            ),
            sa.Date(),
        )
    return sa.cast(func.date_format(col, _MYSQL_FORMAT[grain]), sa.Date())


def _mssql_bucket(col: ColumnElement[Any], grain: TimeGrain) -> ColumnElement[Any]:
    """Works on every supported SQL Server version (no DATETRUNC, which is 2022+)."""
    # mssql.DATE, not sa.Date: before the server version is known SQLAlchemy renders
    # sa.Date as DATETIME on SQL Server, which would keep the time of day.
    if grain is TimeGrain.DAY:
        return sa.cast(col, MSSQL_DATE())
    if grain is TimeGrain.WEEK:  # 1900-01-01 was a Monday
        day: ColumnElement[Any] = literal_column("day")
        offset = func.datediff(day, literal_column("'19000101'"), col) % _n(7)
        return func.dateadd(day, -offset, sa.cast(col, MSSQL_DATE()))
    if grain is TimeGrain.QUARTER:
        month = (func.datepart(literal_column("quarter"), col) - _n(1)) * _n(3) + _n(1)
        return func.datefromparts(func.year(col), month, _n(1))
    if grain is TimeGrain.MONTH:
        return func.datefromparts(func.year(col), func.month(col), _n(1))
    return func.datefromparts(func.year(col), _n(1), _n(1))


def _oracle_bucket(col: ColumnElement[Any], grain: TimeGrain) -> ColumnElement[Any]:
    if grain is TimeGrain.DAY:
        return func.trunc(col)
    return func.trunc(col, _ORACLE_FORMAT[grain])


def _hana_bucket(col: ColumnElement[Any], grain: TimeGrain) -> ColumnElement[Any]:
    """SAP HANA foundation: day, month and year (week/quarter need a verified test)."""
    if grain is TimeGrain.DAY:
        return func.to_date(col)
    if grain in {TimeGrain.MONTH, TimeGrain.YEAR}:
        pattern = "'YYYY-MM'" if grain is TimeGrain.MONTH else "'YYYY'"
        suffix = "'-01'" if grain is TimeGrain.MONTH else "'-01-01'"
        return func.to_date(
            func.concat(func.to_varchar(col, literal_column(pattern)), literal_column(suffix))
        )
    raise UnsupportedConnectorError(
        "This time grain is not available for SAP HANA yet.",
        details={"dialect": "hana", "grain": grain.value},
    )


def time_bucket(dialect_name: str, col: ColumnElement[Any], grain: TimeGrain) -> ColumnElement[Any]:
    """The first day of ``col``'s day/week (Monday)/month/quarter/year bucket, as a date."""
    if dialect_name == "postgresql":
        return sa.cast(func.date_trunc(_PG_GRAIN[grain], col), sa.Date())
    if dialect_name == "sqlite":
        return _sqlite_bucket(col, grain)
    if dialect_name in {"mysql", "mariadb"}:
        return _mysql_bucket(col, grain)
    if dialect_name == "mssql":
        return _mssql_bucket(col, grain)
    if dialect_name == "oracle":
        return _oracle_bucket(col, grain)
    if dialect_name == "hana":
        return _hana_bucket(col, grain)
    raise UnsupportedConnectorError(
        "Time bucketing is not implemented for this database yet.",
        details={"dialect": dialect_name},
    )


@dataclass(frozen=True, slots=True)
class OutputColumn:
    name: str
    ref: str
    kind: str  # dimension | time | measure
    type: CanonicalType
    format: ValueFormat | None = None
    time_grain: TimeGrain | None = None
    role: str | None = None  # dimensions: category | name | identifier | free_text | time
    aggregation: str | None = None  # measures


@dataclass
class CompiledQuery:
    statement: Select[Any]
    previous_statement: Select[Any] | None
    output: list[OutputColumn]
    dialect_name: str
    tables: set[str] = field(default_factory=set)
    columns: set[str] = field(default_factory=set)
    row_filter_columns: set[str] = field(default_factory=set)

    def render(self, dialect: Dialect, *, previous: bool = False) -> str:
        stmt = self.previous_statement if previous else self.statement
        assert stmt is not None  # noqa: S101 - caller checks
        return str(stmt.compile(dialect=dialect, compile_kwargs={"render_postcompile": True}))

    def parameters(self, dialect: Dialect, *, previous: bool = False) -> dict[str, Any]:
        stmt = self.previous_statement if previous else self.statement
        assert stmt is not None  # noqa: S101
        return dict(stmt.compile(dialect=dialect).params)


class _Builder:
    def __init__(self, resolved: ResolvedPlan, dialect_name: str) -> None:
        self.resolved = resolved
        self.dialect_name = dialect_name
        self.aliases: dict[str, NamedFromClause] = {}
        self.tables: set[str] = set()
        self.columns: set[str] = set()
        needed: dict[str, set[str]] = {s.alias: set() for s in resolved.sources}
        for d in resolved.dimensions:
            needed[d.source].add(d.field)
        for f in resolved.filters:
            needed[f.source].add(f.field)
        for rf in resolved.row_filters:
            needed[rf.source].add(rf.field)
        if resolved.time_filter:
            needed[resolved.time_filter.source].add(resolved.time_filter.field)
        for m in self._leaf_measures(resolved.measures):
            if m.source and m.field:
                needed[m.source].add(m.field)
        main_alias = resolved.sources[0].alias
        for source in resolved.sources[1:]:
            for left, right in source.join_keys:
                needed[main_alias].add(left)
                needed[source.alias].add(right)
        for source in resolved.sources:
            schema = None if source.schema_name in (None, "", "main") else source.schema_name
            cols = [
                sa.column(
                    name,
                    SA_TYPES.get(source.column_types.get(name, CanonicalType.STRING), sa.String()),
                )
                for name in sorted(needed[source.alias])
            ]
            self.aliases[source.alias] = sa.table(source.object_name, *cols, schema=schema).alias(
                source.alias
            )
            self.tables.add(
                f"{schema}.{source.object_name}".lower() if schema else source.object_name.lower()
            )
            self.columns.update(n.lower() for n in needed[source.alias])

    @staticmethod
    def _leaf_measures(measures: list[ResolvedMeasure]) -> list[ResolvedMeasure]:
        leaves: list[ResolvedMeasure] = []
        for m in measures:
            if m.op is None:
                leaves.append(m)
            else:
                leaves.extend(_Builder._leaf_measures([x for x in (m.left, m.right) if x]))
        return leaves

    def col(self, source: str, name: str) -> ColumnElement[Any]:
        return self.aliases[source].c[name]

    def from_clause(self) -> FromClause:
        sources = self.resolved.sources
        clause: FromClause = self.aliases[sources[0].alias]
        for source in sources[1:]:
            on = and_(
                *[
                    self.col(sources[0].alias, left) == self.col(source.alias, right)
                    for left, right in source.join_keys
                ]
            )
            clause = clause.join(self.aliases[source.alias], on)
        return clause

    def dimension(self, d: ResolvedDimension) -> ColumnElement[Any]:
        expr = self.col(d.source, d.field)
        if d.time_grain is not None:
            expr = time_bucket(self.dialect_name, expr, d.time_grain)
        return expr

    def measure(self, m: ResolvedMeasure) -> ColumnElement[Any]:
        if m.op is None:
            col = self.col(m.source or "", m.field or "")
            match m.aggregation:
                case "sum":
                    return func.sum(col)
                case "count":
                    # A joined row can repeat the counted record (bad keys on the "one" side,
                    # or a name-based join): count each key once.
                    if len(self.resolved.sources) > 1 and _is_key(m.field or ""):
                        return func.count(distinct(col))
                    return func.count(col)
                case "count_distinct":
                    return func.count(distinct(col))
                case "avg":
                    return func.avg(col)
                case "min":
                    return func.min(col)
                case "max":
                    return func.max(col)
            raise ValueError(f"unknown aggregation {m.aggregation}")
        assert m.left is not None and m.right is not None  # noqa: S101
        left, right = self.measure(m.left), self.measure(m.right)
        match m.op:
            case "add":
                return left + right
            case "subtract":
                return left - right
            case "multiply":
                return left * right
            case "divide":
                return self.as_float(left) / func.nullif(right, 0)
            case "percent_of":
                return literal_column("100.0") * self.as_float(left) / func.nullif(right, 0)
        raise ValueError(f"unknown metric op {m.op}")

    def as_float(self, expr: ColumnElement[Any]) -> ColumnElement[Any]:
        """Avoid integer division. MySQL/MariaDB cannot CAST to FLOAT portably (SQLAlchemy
        drops the cast), and their ``/`` already returns a decimal."""
        if self.dialect_name in {"mysql", "mariadb"}:
            return expr
        return sa.cast(expr, sa.Float())

    def filter(self, f: ResolvedFilter) -> ColumnElement[Any]:
        col = self.col(f.source, f.field)
        v = f.value
        expr: ColumnElement[Any]
        match f.op:
            case FilterOp.EQ:
                expr = col == v
            case FilterOp.NE:
                expr = col != v
            case FilterOp.GT:
                expr = col > v
            case FilterOp.GTE:
                expr = col >= v
            case FilterOp.LT:
                expr = col < v
            case FilterOp.LTE:
                expr = col <= v
            case FilterOp.IN:
                expr = col.in_(v)
            case FilterOp.NOT_IN:
                expr = col.not_in(v)
            case FilterOp.BETWEEN:
                expr = col.between(v[0], v[1])
            case FilterOp.CONTAINS:
                expr = func.lower(col).contains(str(v).lower(), autoescape=True)
            case FilterOp.STARTS_WITH:
                expr = func.lower(col).startswith(str(v).lower(), autoescape=True)
            case FilterOp.IS_NULL:
                expr = col.is_(None)
            case FilterOp.IS_NOT_NULL:
                expr = col.is_not(None)
        return expr

    def build(self, window: Window | None) -> Select[Any]:
        r = self.resolved
        dim_exprs = [self.dimension(d).label(d.label) for d in r.dimensions]
        measure_exprs = [self.measure(m).label(m.label) for m in r.measures]
        conditions: list[ColumnElement[Any]] = [
            self.col(rf.source, rf.field) == rf.value for rf in r.row_filters
        ]
        if r.time_filter is not None and window is not None:
            tcol = self.col(r.time_filter.source, r.time_filter.field)
            conditions += [tcol >= window.start, tcol < window.end]
        conditions += [self.filter(f) for f in r.filters]
        stmt = select(*dim_exprs, *measure_exprs).select_from(self.from_clause())
        if conditions:  # tables without tenant row filters may have none
            stmt = stmt.where(and_(*conditions))
        if dim_exprs:
            stmt = stmt.group_by(*[expr.element for expr in dim_exprs])
        labels = {c.name: c for c in [*dim_exprs, *measure_exprs]}
        order = [
            labels[name].desc() if direction == "desc" else labels[name].asc()
            for name, direction in r.sort
        ]
        tie = [labels[d.label].asc() for d in r.dimensions if d.label not in {n for n, _ in r.sort}]
        return stmt.order_by(*order, *tie).limit(r.limit)


def _is_key(field: str) -> bool:
    name = field.lower()
    return name == "id" or name.endswith("_id")


def compile_plan(resolved: ResolvedPlan, dialect_name: str) -> CompiledQuery:
    builder = _Builder(resolved, dialect_name)
    tf = resolved.time_filter
    statement = builder.build(tf.window if tf else None)
    previous = builder.build(tf.previous) if tf is not None and tf.previous is not None else None
    output = [
        OutputColumn(
            name=d.label,
            ref=d.ref,
            kind="time" if d.type in {CanonicalType.DATE, CanonicalType.TIMESTAMP} else "dimension",
            type=CanonicalType.DATE if d.time_grain else d.type,
            time_grain=d.time_grain,
            role=d.role,
        )
        for d in resolved.dimensions
    ] + [
        OutputColumn(
            name=m.label,
            ref=m.ref,
            kind="measure",
            type=m.type,
            format=m.format,
            aggregation=m.aggregation,
        )
        for m in resolved.measures
    ]
    return CompiledQuery(
        statement=statement,
        previous_statement=previous,
        output=output,
        dialect_name=dialect_name,
        tables=builder.tables,
        columns=builder.columns,
        row_filter_columns={rf.field.lower() for rf in resolved.row_filters},
    )
