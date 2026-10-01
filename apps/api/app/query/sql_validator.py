"""AST-level safety validation of compiled SQL (defence in depth).

The compiler builds SQL from a validated plan, so everything here *should* already
hold. This validator re-checks the rendered statement independently with sqlglot,
so a compiler bug, a malicious semantic-layer edit or a future code path that
builds SQL differently still cannot reach the database with:

* more than one statement, comments, or non-ASCII outside string literals;
* anything other than one plain ``SELECT`` (no UNION/CTE/INTO/subqueries);
* writes, DDL, DCL, procedure calls, COPY/PRAGMA/ATTACH or other commands;
* ``*`` projections, locking reads, system catalogs, unapproved tables/columns;
* functions outside a small allowlist (``pg_sleep``, ``load_extension``, ...);
* cross/natural/comma joins, too many joins, a missing or excessive LIMIT;
* a missing mandatory equality predicate (e.g. ``tenant_code = <parameter>``)
  at the top level of WHERE (an ``OR`` around it does not count).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Final

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from app.core.exceptions import AppError

# SQLAlchemy dialect name -> sqlglot dialect. SAP HANA has no sqlglot dialect; the
# compiler's HANA output is ANSI-style, so the generic parser is used (foundation only).
DIALECTS: Final[dict[str, str]] = {
    "postgresql": "postgres",
    "sqlite": "sqlite",
    "mysql": "mysql",
    "mariadb": "mysql",
    "mssql": "tsql",
    "sqlserver": "tsql",
    "oracle": "oracle",
    "hana": "",
}

SYSTEM_SCHEMAS: Final = frozenset(
    {"pg_catalog", "information_schema", "pg_toast", "sys", "mysql", "performance_schema"}
)
SYSTEM_TABLE_PREFIXES: Final = ("pg_", "sqlite_")

# sqlglot node classes a compiled analytical query may contain as functions.
ALLOWED_FUNCTIONS: Final[tuple[type[exp.Expression], ...]] = (
    exp.Sum,
    exp.Count,
    exp.Avg,
    exp.Min,
    exp.Max,
    exp.Coalesce,
    exp.Nullif,
    exp.Cast,
    exp.Lower,
    exp.Upper,
    exp.Abs,
    exp.Round,
    exp.Floor,  # integer bucketing (SQLite quarter)
    exp.DateTrunc,
    exp.TimestampTrunc,
    exp.TimeToStr,  # SQLite strftime(...)
    exp.TsOrDsToTimestamp,  # implicit argument wrapper of SQLite strftime
    exp.Date,  # SQLite date(x, modifiers...)
    # Phase 6 time buckets (MySQL, SQL Server, Oracle, SAP HANA) - all pure date/string
    # arithmetic on the bucketed column and constants chosen by the compiler.
    exp.DateFromParts,
    exp.DateAdd,
    exp.DateSub,
    exp.DateDiff,
    exp.Year,
    exp.Month,
    exp.Quarter,
    exp.DayOfWeek,
    exp.Extract,  # SQL Server DATEPART(quarter, x)
    exp.Concat,
    exp.Pad,
    exp.StrToDate,
    exp.TsOrDsToDate,
    exp.TimeStrToTime,  # SQL Server string date constants (week anchor)
    exp.And,  # boolean connectors are Func subclasses in sqlglot
    exp.Or,
)
# Pure date/format functions sqlglot does not model (parsed as Anonymous). ``printf``
# is SQLite's side-effect-free string formatter (quarter buckets); the rest are the
# MySQL / SQL Server / SAP HANA bucket functions used by ``query/compiler.py``.
ALLOWED_ANONYMOUS: Final = frozenset(
    {
        "date",
        "datetime",
        "strftime",
        "date_trunc",
        "printf",
        "subdate",
        "weekday",
        "datepart",
        "datefromparts",
        "to_date",
        "to_varchar",
        "trunc",
    }
)

_WRITE_CLASS_NAMES: Final = (
    "Insert",
    "Update",
    "Delete",
    "Merge",
    "Drop",
    "Create",
    "Alter",
    "TruncateTable",
    "Grant",
    "Revoke",
    "Command",
    "Set",
    "Transaction",
    "Commit",
    "Rollback",
    "Copy",
    "Execute",
    "Pragma",
    "Attach",
    "Detach",
    "Use",
    "Describe",
    "Show",
    "Kill",
    "Analyze",
    "Cache",
    "Uncache",
    "Refresh",
    "LoadData",
    "Summarize",
    "Comment",
)
WRITE_CLASSES: Final[tuple[type[exp.Expression], ...]] = tuple(
    cls for name in _WRITE_CLASS_NAMES if isinstance(cls := getattr(exp, name, None), type)
)
PLACEHOLDER_CLASSES: Final = (exp.Placeholder, exp.Parameter)


class SqlSafetyError(AppError):
    status_code = 403
    code = "QUERY_NOT_ALLOWED"
    default_message = "The requested operation is not permitted."

    def __init__(self, reason: str, **details: Any) -> None:
        super().__init__(details={"reason": reason, **details})
        self.reason = reason


@dataclass(frozen=True)
class SqlPolicy:
    dialect: str
    allowed_tables: frozenset[str]
    allowed_columns: frozenset[str]
    required_equality_columns: frozenset[str] = frozenset()
    max_joins: int = 3
    max_limit: int = 1000


@dataclass(frozen=True)
class ValidatedSql:
    tables: frozenset[str]
    columns: frozenset[str]
    functions: frozenset[str]
    has_limit: bool
    statement_hash: str
    normalized_sql: str = field(repr=False)


# ------------------------------------------------------------------ lexical
_PLACEHOLDER_NAMED: Final = re.compile(r"%\([A-Za-z_][A-Za-z0-9_]*\)s")


def _literal_end(sql: str, start: int) -> int:
    """Index just past the quoted literal/identifier starting at ``start``."""
    close = "]" if sql[start] == "[" else sql[start]
    j, n = start + 1, len(sql)
    while j < n:
        if sql[j] == "\\":
            # Backslash escapes (MySQL literals, PostgreSQL E'...') let the database
            # split a literal differently from this lexer; compiled SQL never has them.
            raise SqlSafetyError("PARSE_ERROR", detail="backslash in literal")
        if sql[j] == close:
            if j + 1 < n and sql[j + 1] == close:  # doubled quote escape
                j += 2
                continue
            return j + 1
        j += 1
    raise SqlSafetyError("PARSE_ERROR", detail="unterminated literal")


def _lexical_scan(sql: str) -> str:
    """Reject comments, extra statements and non-ASCII outside literals; rewrite
    ``%s`` / ``%(name)s`` placeholders (outside literals) to ``?``."""
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch in "'\"`[":
            end = _literal_end(sql, i)
            out.append(sql[i:end])
            i = end
            continue
        two = sql[i : i + 2]
        if two in {"--", "/*", "*/"} or ch == "#":
            raise SqlSafetyError("COMMENT")
        if ch == ";":
            raise SqlSafetyError("MULTIPLE_STATEMENTS")
        if ch == "\\":
            raise SqlSafetyError("PARSE_ERROR", detail="backslash outside literal")
        if ord(ch) > 126 or (ord(ch) < 32 and ch not in "\t\r\n"):
            raise SqlSafetyError("PARSE_ERROR", detail="invalid character outside literal")
        if ch == "%":
            named = _PLACEHOLDER_NAMED.match(sql, i)
            if named:
                out.append("?")
                i = named.end()
                continue
            if two == "%s":
                out.append("?")
                i += 2
                continue
        out.append(ch)
        i += 1
    return "".join(out)


# --------------------------------------------------------------------- AST
def _arg(node: exp.Expression, name: str) -> Any:
    return node.args.get(name) if name in node.args else node.args.get(f"{name}_")


def _is_placeholder(node: exp.Expression) -> bool:
    while isinstance(node, exp.Cast | exp.Paren):
        node = node.this
    return isinstance(node, PLACEHOLDER_CLASSES)


def _conjuncts(node: exp.Expression | None) -> list[exp.Expression]:
    if node is None:
        return []
    while isinstance(node, exp.Paren):
        node = node.this
    if isinstance(node, exp.And):
        return _conjuncts(node.this) + _conjuncts(node.expression)
    return [node]


def _table_key(table: exp.Table) -> tuple[str, str]:
    return (table.db or "").lower(), (table.name or "").lower()


def _table_allowed(schema: str, name: str, allowed: frozenset[str]) -> bool:
    """``schema.table`` entries match exactly; a bare entry matches any non-system
    schema. An *unqualified* reference (resolved via the connection's pinned default
    schema) matches an entry whose table part is the same name."""
    if schema:
        return f"{schema}.{name}" in allowed or name in allowed
    return name in allowed or any(entry.rsplit(".", 1)[-1] == name for entry in allowed)


def _check_tables(root: exp.Select, policy: SqlPolicy) -> frozenset[str]:
    names: set[str] = set()
    for table in root.find_all(exp.Table):
        if not isinstance(table.this, exp.Identifier):
            raise SqlSafetyError("FUNCTION_NOT_ALLOWED", detail="table function")
        schema, name = _table_key(table)
        if (table.catalog or "").lower() in SYSTEM_SCHEMAS:
            raise SqlSafetyError("SYSTEM_CATALOG", table=name)
        if (
            schema in SYSTEM_SCHEMAS
            or name in SYSTEM_SCHEMAS
            or name.startswith(SYSTEM_TABLE_PREFIXES)
        ):
            raise SqlSafetyError("SYSTEM_CATALOG", table=name)
        qualified = f"{schema}.{name}" if schema else name
        if not _table_allowed(schema, name, policy.allowed_tables):
            raise SqlSafetyError("TABLE_NOT_ALLOWED", table=qualified)
        names.add(qualified)
    if not names:
        raise SqlSafetyError("TABLE_NOT_ALLOWED", detail="no table")
    return frozenset(names)


def _check_joins(root: exp.Select, policy: SqlPolicy) -> None:
    joins: list[exp.Join] = list(_arg(root, "joins") or [])
    from_ = _arg(root, "from")
    if from_ is not None and len(from_.expressions or []) > 0:
        raise SqlSafetyError("CROSS_JOIN", detail="comma join")
    for join in joins:
        kind = (join.args.get("kind") or "").upper()
        method = (join.args.get("method") or "").upper()
        if kind == "CROSS" or method == "NATURAL":
            raise SqlSafetyError("CROSS_JOIN")
        if join.args.get("on") is None and not join.args.get("using"):
            raise SqlSafetyError("CROSS_JOIN", detail="join without condition")
    if len(joins) > policy.max_joins:
        raise SqlSafetyError("TOO_MANY_JOINS", joins=len(joins))


def _check_functions(root: exp.Select) -> frozenset[str]:
    names: set[str] = set()
    for func in root.find_all(exp.Func):
        if isinstance(func, exp.Anonymous):
            fname = str(func.name).lower()
            if fname not in ALLOWED_ANONYMOUS:
                raise SqlSafetyError("FUNCTION_NOT_ALLOWED", function=fname[:64])
            names.add(fname)
            continue
        if not isinstance(func, ALLOWED_FUNCTIONS):
            raise SqlSafetyError("FUNCTION_NOT_ALLOWED", function=type(func).__name__)
        if type(func).__name__ not in {"And", "Or"}:
            names.add(type(func).__name__.lower())
    return frozenset(names)


def _check_columns(root: exp.Select, policy: SqlPolicy) -> frozenset[str]:
    aliases = {a.alias.lower() for a in root.expressions if isinstance(a, exp.Alias)}
    used: set[str] = set()
    for column in root.find_all(exp.Column):
        name = column.name.lower()
        if name in policy.allowed_columns:
            used.add(name)
            continue
        in_order_or_group = column.find_ancestor(exp.Order, exp.Group) is not None
        if not column.table and name in aliases and in_order_or_group:
            continue
        raise SqlSafetyError("COLUMN_NOT_ALLOWED", column=name[:128])
    return frozenset(used)


def _check_limit(root: exp.Select, policy: SqlPolicy) -> None:
    limit = _arg(root, "limit")
    if limit is None:
        raise SqlSafetyError("MISSING_LIMIT")
    value = limit.args.get("expression") or limit.args.get("count") or limit.this
    if isinstance(value, exp.Literal) and not value.is_string:
        try:
            if int(value.this) > policy.max_limit:
                raise SqlSafetyError("LIMIT_TOO_HIGH", limit=int(value.this))
        except ValueError as exc:
            raise SqlSafetyError("PARSE_ERROR", detail="non-integer limit") from exc
    elif value is None or not _is_placeholder(value):
        raise SqlSafetyError("PARSE_ERROR", detail="unsupported limit expression")


def _check_required_filters(root: exp.Select, policy: SqlPolicy) -> None:
    if not policy.required_equality_columns:
        return
    where = _arg(root, "where")
    satisfied: set[str] = set()
    for term in _conjuncts(where.this if where is not None else None):
        if not isinstance(term, exp.EQ):
            continue
        left, right = term.this, term.expression
        for col, other in ((left, right), (right, left)):
            if isinstance(col, exp.Column) and _is_placeholder(other):
                satisfied.add(col.name.lower())
    missing = sorted(policy.required_equality_columns - satisfied)
    if missing:
        raise SqlSafetyError("MISSING_REQUIRED_FILTER", columns=missing)


def validate_sql(sql: str, policy: SqlPolicy) -> ValidatedSql:
    """Validate one compiled statement. Raises :class:`SqlSafetyError`."""
    dialect = DIALECTS.get(policy.dialect)
    if dialect is None:
        raise SqlSafetyError("PARSE_ERROR", detail="unknown dialect")
    prepared = _lexical_scan(sql)
    try:
        statements = [s for s in sqlglot.parse(prepared, read=dialect) if s is not None]
    except SqlglotError as exc:
        raise SqlSafetyError("PARSE_ERROR") from exc
    if len(statements) != 1:
        raise SqlSafetyError("MULTIPLE_STATEMENTS" if statements else "PARSE_ERROR")
    root = statements[0]

    if isinstance(root, WRITE_CLASSES) or any(
        isinstance(node, WRITE_CLASSES) for node in root.walk()
    ):
        raise SqlSafetyError("WRITE_OR_DDL")
    if (
        not isinstance(root, exp.Select)
        or _arg(root, "with") is not None
        or _arg(root, "into") is not None
    ):
        raise SqlSafetyError("NOT_SELECT")
    if _arg(root, "locks"):
        raise SqlSafetyError("LOCKING")
    if any(
        isinstance(node, exp.Select | exp.Subquery | exp.Union | exp.Lateral | exp.Unnest)
        for node in root.walk()
        if node is not root
    ):
        raise SqlSafetyError("SUBQUERY")
    if any(isinstance(node, exp.Star) for node in root.walk()):
        raise SqlSafetyError("STAR")

    tables = _check_tables(root, policy)
    _check_joins(root, policy)
    functions = _check_functions(root)
    columns = _check_columns(root, policy)
    _check_limit(root, policy)
    _check_required_filters(root, policy)

    normalized = root.sql(dialect=dialect, normalize=True, pretty=False)
    return ValidatedSql(
        tables=tables,
        columns=columns,
        functions=functions,
        has_limit=True,
        statement_hash=hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
        normalized_sql=normalized,
    )
