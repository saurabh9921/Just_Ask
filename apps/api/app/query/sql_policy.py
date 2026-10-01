"""Build the SQL safety policy for a resolved plan - independently of the compiler.

Shared by the interactive agent and background exports so both apply exactly the same
second gate: allowed tables and columns come from the semantic layer's approved
objects and fields for the plan's entities, and every tenant row filter must appear as
an equality predicate.
"""

from __future__ import annotations

from app.query.sql_validator import SqlPolicy
from app.query.validator import ResolvedPlan
from app.semantic.loader import SemanticBundle


def build_sql_policy(
    bundle: SemanticBundle, resolved: ResolvedPlan, dialect_name: str, *, max_limit: int
) -> SqlPolicy:
    tables: set[str] = set()
    columns: set[str] = set()
    for source in resolved.sources:
        entity = bundle.entities[source.entity]
        for obj in entity.source.allowed_database_objects:
            tables.add(obj.lower())
            if source.schema_name and source.schema_name != "main":
                tables.add(f"{source.schema_name}.{obj}".lower())
        columns |= {f.lower() for f in entity.physical_fields()}
        columns |= {k.lower() for pair in source.join_keys for k in pair}
    return SqlPolicy(
        dialect=dialect_name,
        allowed_tables=frozenset(tables),
        allowed_columns=frozenset(columns),
        required_equality_columns=frozenset(rf.field.lower() for rf in resolved.row_filters),
        max_limit=max_limit,
    )
