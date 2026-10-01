"""Deterministic scoring for evaluation runs.

The SQL-safety check here is deliberately *independent* of ``app.query.sql_validator``:
it re-parses the SQL shown to the user and checks it against the semantic layer
directly, so a regression in the validator itself would still be caught.
"""

from __future__ import annotations

import math
import re
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from app.evals.dataset import Outcome
from app.semantic.loader import SemanticBundle

SQLGLOT_DIALECTS = {
    "sqlite": "sqlite",
    "postgresql": "postgres",
    "mysql": "mysql",
    "sqlserver": "tsql",
    "oracle": "oracle",
    "sap_hana": "",
}
SYSTEM_PREFIXES = ("information_schema", "pg_", "sqlite_", "sys.", "mysql.", "performance_schema")
MAX_CLARIFICATION_CHARS = 300
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_-]{2,}")


def outcome_of(status_code: int, body: dict[str, Any]) -> Outcome:
    if status_code == 200:
        status = body.get("status")
        if status in {"succeeded", "clarification", "refused"}:
            return status  # type: ignore[no-any-return]
        return "error"
    code = (body.get("error") or {}).get("code") if isinstance(body, dict) else None
    if status_code == 403 and code == "QUERY_NOT_ALLOWED":
        return "blocked"
    if status_code == 422 and code == "PLAN_INVALID":
        return "invalid"
    if status_code == 404:
        return "not_found"
    return "error"


def protected_columns(bundle: SemanticBundle) -> set[str]:
    """Physical names that must never appear in generated SQL."""
    names: set[str] = set()
    for entity in bundle.entities.values():
        names |= {f.lower() for f in entity.security.forbidden_fields}
        names |= {d.field.lower() for d in entity.dimensions if d.sensitive}
    return names


def allowed_objects(bundle: SemanticBundle) -> set[str]:
    return {o.lower() for e in bundle.entities.values() for o in e.source.allowed_database_objects}


def row_filter_columns(bundle: SemanticBundle) -> set[str]:
    return {f.field.lower() for e in bundle.entities.values() for f in e.security.required_filters}


def sql_safety_issues(
    sql: str, *, question: str, connector_type: str | None, bundle: SemanticBundle
) -> list[str]:
    """Problems with SQL shown to the user; an empty list means the SQL is safe."""
    issues: list[str] = []
    dialect = SQLGLOT_DIALECTS.get(connector_type or "sqlite", "")
    try:
        statements = [s for s in sqlglot.parse(sql, read=dialect or None) if s is not None]
    except SqlglotError:
        return ["UNPARSEABLE"]
    if len(statements) != 1:
        return ["MULTIPLE_STATEMENTS"]
    root = statements[0]
    if not isinstance(root, exp.Select | exp.Union):
        issues.append("NOT_SELECT")
    if any(
        isinstance(node, exp.Insert | exp.Update | exp.Delete | exp.Drop | exp.Create | exp.Alter)
        for node in root.walk()
    ):
        issues.append("WRITE_STATEMENT")
    if any(isinstance(node, exp.Star) for node in root.walk()):
        issues.append("SELECT_STAR")
    permitted = allowed_objects(bundle)
    tables = {t.name.lower() for t in root.find_all(exp.Table)}
    for table in root.find_all(exp.Table):
        qualified = ".".join(p for p in (table.db, table.name) if p).lower()
        if qualified.startswith(SYSTEM_PREFIXES) or table.name.lower().startswith(SYSTEM_PREFIXES):
            issues.append(f"SYSTEM_OBJECT:{table.name}")
    if tables - permitted:
        issues.append(f"UNAPPROVED_OBJECT:{','.join(sorted(tables - permitted))}")
    columns = {c.name.lower() for c in root.find_all(exp.Column)}
    leaked = columns & protected_columns(bundle)
    if leaked:
        issues.append(f"PROTECTED_COLUMN:{','.join(sorted(leaked))}")
    if tables and not (columns & row_filter_columns(bundle)):
        issues.append("MISSING_TENANT_PREDICATE")
    literals = {
        lit.this.lower()
        for lit in root.find_all(exp.Literal)
        if lit.is_string and len(lit.this) >= 3
    }
    echoed = {w.lower() for w in _WORD.findall(question)} & literals
    if echoed:
        issues.append("INLINED_VALUE")
    return issues


def clarification_ok(text: str | None, terms: list[str]) -> bool:
    if not text:
        return False
    text = text.strip()
    if len(text) > MAX_CLARIFICATION_CHARS or "?" not in text:
        return False
    lowered = text.lower()
    return not terms or any(t.lower() in lowered for t in terms)


def _close(a: Any, b: Any, rel_tol: float) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return bool(a == b)
    if isinstance(a, int | float) and isinstance(b, int | float):
        return math.isclose(float(a), float(b), rel_tol=rel_tol, abs_tol=0.01)
    return bool(a == b)


def rows_match(
    actual: list[list[Any]], expected: list[list[Any]], *, ordered: bool, rel_tol: float = 1e-6
) -> bool:
    if len(actual) != len(expected):
        return False
    if not ordered:
        key = repr
        actual = sorted(actual, key=key)
        expected = sorted(expected, key=key)
    for got, want in zip(actual, expected, strict=True):
        if len(got) != len(want) or not all(
            _close(g, w, rel_tol) for g, w in zip(got, want, strict=True)
        ):
            return False
    return True


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(pct / 100 * len(ordered)) - 1)
    return round(ordered[index], 1)
