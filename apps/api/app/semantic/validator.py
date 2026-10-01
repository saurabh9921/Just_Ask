"""Cross-reference validation of a semantic bundle (and binding to a real catalog)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from app.connectors.common.types import CanonicalType, ObjectMetadata
from app.core.permissions import Permission
from app.semantic.loader import SemanticBundle
from app.semantic.models import NUMERIC_AGGREGATIONS, NUMERIC_TYPES, Entity


@dataclass
class ValidationReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _entity_names(entity: Entity) -> dict[str, str]:
    names: dict[str, str] = {}
    for d in entity.dimensions:
        names[d.name] = "dimension"
    for m in entity.measures:
        names[m.name] = "measure"
    for t in entity.time_fields:
        names[t.name] = "time_field"
    return names


Err = Callable[[str], None]
KNOWN_PERMISSIONS = frozenset(p.value for p in Permission)


def validate_bundle(bundle: SemanticBundle) -> ValidationReport:
    report = ValidationReport()
    err, warn = report.errors.append, report.warnings.append
    _check_entities(bundle, err, warn)
    seen_metrics = _check_metrics(bundle, err)
    _check_metric_cycles(bundle, err)
    _check_joins(bundle, err)
    _check_glossary(bundle, err, seen_metrics)
    _check_examples(bundle, err)
    return report


def _check_entities(bundle: SemanticBundle, err: Err, warn: Err) -> None:
    policy = bundle.policy
    for entity in bundle.entities.values():
        for dim in entity.dimensions:
            cls = policy.classify(dim.field)
            if cls and cls.action == "forbid":
                err(f"{entity.entity}.{dim.name}: field '{dim.field}' is forbidden by policy")
            elif cls and cls.action == "mask" and not dim.sensitive:
                err(f"{entity.entity}.{dim.name}: field '{dim.field}' must be marked sensitive")
        for measure in entity.measures:
            if policy.is_forbidden(measure.field):
                err(
                    f"{entity.entity}.{measure.name}: field '{measure.field}' "
                    "is forbidden by policy"
                )
        if not entity.measures:
            warn(f"{entity.entity}: no measures defined")
        unknown = sorted(set(entity.security.required_permissions) - KNOWN_PERMISSIONS)
        if unknown:
            err(f"{entity.entity}: unknown required_permissions {unknown}")


def _check_metrics(bundle: SemanticBundle, err: Err) -> set[tuple[str, str]]:
    seen_metrics: set[tuple[str, str]] = set()
    for metric in bundle.metrics:
        metric_entity = bundle.entities.get(metric.entity)
        metric_key = (metric.entity, metric.name)
        if metric_key in seen_metrics:
            err(f"metric {metric.entity}.{metric.name}: duplicate")
        seen_metrics.add(metric_key)
        if metric_entity is None:
            err(f"metric {metric.name}: unknown entity {metric.entity}")
            continue
        names = _entity_names(metric_entity)
        if metric.name in names:
            err(f"metric {metric.entity}.{metric.name}: clashes with a {names[metric.name]}")
        own_metrics = {m.name for m in bundle.metrics if m.entity == metric.entity}
        for operand in (metric.expression.left, metric.expression.right):
            if names.get(operand) != "measure" and operand not in own_metrics:
                err(
                    f"metric {metric.entity}.{metric.name}: operand '{operand}' "
                    "is not a measure/metric"
                )
    return seen_metrics


def _check_joins(bundle: SemanticBundle, err: Err) -> None:
    join_names: set[str] = set()
    for join in bundle.joins:
        if join.name in join_names:
            err(f"join {join.name}: duplicate")
        join_names.add(join.name)
        for side in (join.from_entity, join.to_entity):
            if side not in bundle.entities:
                err(f"join {join.name}: unknown entity {side}")
        if join.from_entity == join.to_entity:
            err(f"join {join.name}: self-joins are not allowed")
        for join_key in join.on:
            for ent_name, fld in (
                (join.from_entity, join_key.from_field),
                (join.to_entity, join_key.to_field),
            ):
                ent = bundle.entities.get(ent_name)
                if ent and fld in ent.security.forbidden_fields:
                    err(f"join {join.name}: key '{fld}' is forbidden on {ent_name}")


def _check_glossary(bundle: SemanticBundle, err: Err, seen_metrics: set[tuple[str, str]]) -> None:
    for term in bundle.terms:
        target = term.maps_to
        if target is None:
            continue
        term_entity = bundle.entities.get(target.entity)
        if term_entity is None:
            err(f"term '{term.term}': unknown entity {target.entity}")
            continue
        names = _entity_names(term_entity)
        if target.measure and names.get(target.measure) != "measure":
            err(f"term '{term.term}': unknown measure {target.entity}.{target.measure}")
        if target.dimension and names.get(target.dimension) != "dimension":
            err(f"term '{term.term}': unknown dimension {target.entity}.{target.dimension}")
        if target.metric and (target.entity, target.metric) not in seen_metrics:
            err(f"term '{term.term}': unknown metric {target.entity}.{target.metric}")
    abbreviations = [a.abbreviation.lower() for a in bundle.abbreviations]
    for dup in sorted({a for a in abbreviations if abbreviations.count(a) > 1}):
        err(f"abbreviation '{dup}': duplicate")


def _check_examples(bundle: SemanticBundle, err: Err) -> None:
    example_ids: set[str] = set()
    for ex in bundle.examples:
        if ex.id in example_ids:
            err(f"example {ex.id}: duplicate id")
        example_ids.add(ex.id)
        ex_entity = bundle.entities.get(ex.entity)
        if ex_entity is None:
            err(f"example {ex.id}: unknown entity {ex.entity}")
            continue
        names = _entity_names(ex_entity)
        metric_names = {m.name for m in bundle.metrics if m.entity == ex.entity}
        for dim_name in ex.dimensions:
            if names.get(dim_name) not in {"dimension", "time_field"}:
                err(f"example {ex.id}: unknown dimension {ex.entity}.{dim_name}")
        for measure_name in ex.measures:
            if names.get(measure_name) != "measure" and measure_name not in metric_names:
                err(f"example {ex.id}: unknown measure {ex.entity}.{measure_name}")
        if ex.time_field and names.get(ex.time_field) != "time_field":
            err(f"example {ex.id}: unknown time field {ex.entity}.{ex.time_field}")


def _check_metric_cycles(bundle: SemanticBundle, err: Callable[[str], None]) -> None:
    graph: dict[tuple[str, str], list[tuple[str, str]]] = {}
    names = {(m.entity, m.name) for m in bundle.metrics}
    for m in bundle.metrics:
        graph[(m.entity, m.name)] = [
            (m.entity, op)
            for op in (m.expression.left, m.expression.right)
            if (m.entity, op) in names
        ]
    state: dict[tuple[str, str], int] = {}

    def visit(node: tuple[str, str]) -> bool:
        if state.get(node) == 1:
            return True
        if state.get(node) == 2:
            return False
        state[node] = 1
        cyclic = any(visit(n) for n in graph.get(node, []))
        state[node] = 2
        return cyclic

    for node in graph:
        if visit(node):
            err(f"metric {node[0]}.{node[1]}: circular definition")
            return


# ---------------------------------------------------------------- binding
@dataclass
class EntityBinding:
    entity: str
    bound: bool
    schema_name: str | None = None
    object_name: str | None = None
    problems: list[str] = field(default_factory=list)


def bind_to_catalog(
    bundle: SemanticBundle, objects: list[ObjectMetadata], default_schema: str | None = None
) -> dict[str, EntityBinding]:
    """Check each entity against a connection's discovered metadata.

    An entity binds when its ``logical_name`` object exists (in its schema or the
    connection default) and every referenced physical field exists with a
    compatible type. Unbound entities are excluded from retrieval for that
    connection, so the model never sees vocabulary the database cannot answer.
    """
    by_key: dict[tuple[str, str], ObjectMetadata] = {}
    by_name: dict[str, list[ObjectMetadata]] = {}
    for obj in objects:
        by_key[(obj.schema_name.lower(), obj.name.lower())] = obj
        by_name.setdefault(obj.name.lower(), []).append(obj)

    results: dict[str, EntityBinding] = {}
    for entity in bundle.entities.values():
        binding = EntityBinding(entity=entity.entity, bound=False)
        schema = entity.source.schema_name or default_schema
        name = entity.source.logical_name.lower()
        found: ObjectMetadata | None = by_key.get((schema.lower(), name)) if schema else None
        if found is None:
            candidates = by_name.get(name, [])
            found = candidates[0] if len(candidates) == 1 else None
        if found is None:
            binding.problems.append(f"object '{entity.source.logical_name}' not found")
            results[entity.entity] = binding
            continue
        obj = found
        columns = {c.name.lower(): c for c in obj.columns}
        for fld in sorted(entity.physical_fields()):
            if fld.lower() not in columns:
                binding.problems.append(f"field '{fld}' not found on {obj.name}")
        for measure in entity.measures:
            col = columns.get(measure.field.lower())
            if (
                col is not None
                and measure.aggregation in NUMERIC_AGGREGATIONS
                and col.canonical_type not in NUMERIC_TYPES
                and col.canonical_type is not CanonicalType.OTHER
            ):
                binding.problems.append(
                    f"measure '{measure.name}' aggregates non-numeric column "
                    f"{col.name} ({col.canonical_type.value})"
                )
        binding.bound = not binding.problems
        binding.schema_name = obj.schema_name
        binding.object_name = obj.name
        results[entity.entity] = binding
    return results
