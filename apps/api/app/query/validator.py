"""Query-plan validation and resolution.

``resolve_plan`` checks a model-produced :class:`QueryPlan` against the governed
semantic layer, the connection's bindings (which entities exist in *this*
database and with which column types), the caller's permissions and platform
limits, and returns a :class:`ResolvedPlan` that the compiler can turn into SQL
without any further decisions.

Issue severities:
* ``retryable`` - the model used a wrong name/shape; one correction attempt is
  allowed with the issue list as feedback (e.g. ``UNKNOWN_FIELD``).
* not retryable - policy violations (``FORBIDDEN_FIELD``, ``SENSITIVE_FIELD``,
  ``ENTITY_NOT_PERMITTED``, ``JOIN_NOT_ALLOWED``...) end the request with 403.

Deterministic defaults are applied *here*, never silently: each one is recorded
in ``assumptions`` and shown to the user (e.g. a default period for large facts).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

from app.connectors.common.types import CanonicalType, ObjectMetadata
from app.core.exceptions import AppError
from app.query.normalizer import Window, plan_hash, resolve_period
from app.query.plans import DateRange, FilterOp, QueryPlan, RelativePeriod, SortSpec
from app.semantic.loader import SemanticBundle
from app.semantic.models import Entity, Metric, TimeGrain, ValueFormat
from app.semantic.roles import ResultRole, dimension_role
from app.semantic.validator import EntityBinding

MAX_JOINS = 2
NUMERIC = frozenset({CanonicalType.INTEGER, CanonicalType.DECIMAL, CanonicalType.FLOAT})
ORDERED = NUMERIC | {CanonicalType.DATE, CanonicalType.TIMESTAMP}


class IssueCode(StrEnum):
    UNKNOWN_ENTITY = "UNKNOWN_ENTITY"
    ENTITY_NOT_AVAILABLE = "ENTITY_NOT_AVAILABLE"
    ENTITY_NOT_PERMITTED = "ENTITY_NOT_PERMITTED"
    UNKNOWN_FIELD = "UNKNOWN_FIELD"
    FORBIDDEN_FIELD = "FORBIDDEN_FIELD"
    SENSITIVE_FIELD = "SENSITIVE_FIELD"
    JOIN_NOT_ALLOWED = "JOIN_NOT_ALLOWED"
    JOIN_MEASURE_NOT_ALLOWED = "JOIN_MEASURE_NOT_ALLOWED"
    INVALID_FILTER = "INVALID_FILTER"
    MEASURE_FILTER_NOT_SUPPORTED = "MEASURE_FILTER_NOT_SUPPORTED"
    INVALID_SORT = "INVALID_SORT"
    INVALID_PERIOD = "INVALID_PERIOD"
    ROW_FILTER_UNAVAILABLE = "ROW_FILTER_UNAVAILABLE"
    TOO_COMPLEX = "TOO_COMPLEX"


NOT_RETRYABLE = frozenset(
    {
        IssueCode.FORBIDDEN_FIELD,
        IssueCode.SENSITIVE_FIELD,
        IssueCode.ENTITY_NOT_PERMITTED,
        IssueCode.JOIN_NOT_ALLOWED,
        IssueCode.ROW_FILTER_UNAVAILABLE,
    }
)


@dataclass(frozen=True, slots=True)
class PlanIssue:
    code: IssueCode
    message: str

    @property
    def retryable(self) -> bool:
        return self.code not in NOT_RETRYABLE


class PlanValidationError(AppError):
    status_code = 422
    code = "PLAN_INVALID"
    default_message = "The question could not be mapped to a valid query."

    def __init__(self, issues: list[PlanIssue]) -> None:
        self.issues = issues
        blocked = [i for i in issues if not i.retryable]
        if blocked:
            self.status_code = 403  # type: ignore[misc]
            self.code = "QUERY_NOT_ALLOWED"  # type: ignore[misc]
        super().__init__(
            None if not blocked else "The request is not permitted by data policy.",
            details={"issues": [{"code": i.code.value, "message": i.message} for i in issues[:20]]},
        )

    @property
    def retryable(self) -> bool:
        return all(i.retryable for i in self.issues)


# ------------------------------------------------------------ resolved plan
@dataclass(frozen=True, slots=True)
class ResolvedSource:
    alias: str
    entity: str
    schema_name: str | None
    object_name: str
    column_types: dict[str, CanonicalType]
    join_keys: tuple[tuple[str, str], ...] = ()  # (left physical on main, right physical here)


@dataclass(frozen=True, slots=True)
class ResolvedDimension:
    label: str
    ref: str
    source: str
    field: str
    type: CanonicalType
    time_grain: TimeGrain | None = None
    role: ResultRole = "category"  # see app.semantic.roles


@dataclass(frozen=True, slots=True)
class ResolvedMeasure:
    label: str
    ref: str
    format: ValueFormat
    source: str | None = None
    field: str | None = None
    aggregation: str | None = None
    type: CanonicalType = CanonicalType.DECIMAL
    op: str | None = None  # metrics
    left: ResolvedMeasure | None = None
    right: ResolvedMeasure | None = None


@dataclass(frozen=True, slots=True)
class ResolvedFilter:
    source: str
    field: str
    type: CanonicalType
    op: FilterOp
    value: Any
    ref: str


@dataclass(frozen=True, slots=True)
class RowFilterValue:
    source: str
    field: str
    value: str


@dataclass(frozen=True, slots=True)
class TimeFilter:
    source: str
    field: str
    type: CanonicalType
    ref: str
    window: Window
    previous: Window | None = None  # set for comparison plans


@dataclass
class ResolvedPlan:
    plan: QueryPlan
    entity: str
    sources: list[ResolvedSource]
    dimensions: list[ResolvedDimension]
    measures: list[ResolvedMeasure]
    filters: list[ResolvedFilter]
    row_filters: list[RowFilterValue]
    time_filter: TimeFilter | None
    sort: list[tuple[str, str]]
    limit: int
    assumptions: list[str] = field(default_factory=list)
    plan_hash: str = ""

    @property
    def source_entities(self) -> list[str]:
        return [s.entity for s in self.sources]

    @property
    def is_comparison(self) -> bool:
        return self.time_filter is not None and self.time_filter.previous is not None


@dataclass(frozen=True, slots=True)
class PlanContext:
    tenant_id: uuid.UUID
    tenant_slug: str
    tenant_settings: dict[str, Any]
    connection_id: uuid.UUID
    semantic_version: int
    permissions: frozenset[str]
    today: date
    max_rows: int
    default_large_fact_period: RelativePeriod = RelativePeriod.LAST_90_DAYS


# ------------------------------------------------------------------- helpers
def _coerce(value: Any, ctype: CanonicalType) -> Any:
    """Coerce a filter value to the column's canonical type or raise ValueError."""
    if isinstance(value, list):
        return [_coerce(v, ctype) for v in value]
    if ctype is CanonicalType.INTEGER:
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str) and value.strip().lstrip("-").isdigit():
            return int(value)
        raise ValueError("expected an integer")
    if ctype in {CanonicalType.DECIMAL, CanonicalType.FLOAT}:
        if isinstance(value, bool):
            raise ValueError("expected a number")
        try:
            return Decimal(str(value)) if ctype is CanonicalType.DECIMAL else float(value)
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("expected a number") from exc
    if ctype in {CanonicalType.DATE, CanonicalType.TIMESTAMP}:
        if isinstance(value, date):
            return value
        if isinstance(value, str):
            return date.fromisoformat(value[:10])
        raise ValueError("expected a date")
    if ctype is CanonicalType.BOOLEAN:
        if isinstance(value, bool):
            return value
        if value in (0, 1, "0", "1", "true", "false"):
            return value in (1, "1", "true")
        raise ValueError("expected true/false")
    if isinstance(value, bool | int | float | date):
        return str(value)
    return str(value)


def _effective_type(catalog: CanonicalType | None, declared: CanonicalType) -> CanonicalType:
    """Catalog type wins unless unknown (e.g. untyped computed view columns in SQLite)."""
    if catalog is None or catalog is CanonicalType.OTHER:
        return declared
    return catalog


class _Resolver:
    def __init__(
        self,
        bundle: SemanticBundle,
        bindings: dict[str, EntityBinding],
        catalog: list[ObjectMetadata],
        ctx: PlanContext,
    ) -> None:
        self.bundle = bundle
        self.bindings = bindings
        self.ctx = ctx
        self.catalog = {(o.schema_name, o.name): o for o in catalog}
        self.issues: list[PlanIssue] = []
        self.assumptions: list[str] = []
        self.sources: dict[str, ResolvedSource] = {}
        self.policy = bundle.policy

    def issue(self, code: IssueCode, message: str) -> None:
        self.issues.append(PlanIssue(code, message))

    # --------------------------------------------------------- entities
    def _usable(self, name: str) -> Entity | None:
        entity = self.bundle.entities.get(name)
        if entity is None:
            self.issue(IssueCode.UNKNOWN_ENTITY, f"unknown entity '{name}'")
            return None
        binding = self.bindings.get(name)
        if binding is None or not binding.bound:
            self.issue(
                IssueCode.ENTITY_NOT_AVAILABLE,
                f"entity '{name}' is not available on this connection",
            )
            return None
        if not set(entity.security.required_permissions) <= self.ctx.permissions:
            self.issue(IssueCode.ENTITY_NOT_PERMITTED, f"you are not permitted to query '{name}'")
            return None
        return entity

    def _source(
        self, entity: Entity, join_keys: tuple[tuple[str, str], ...] = ()
    ) -> ResolvedSource:
        existing = self.sources.get(entity.entity)
        if existing is not None:
            return existing
        binding = self.bindings[entity.entity]
        obj = self.catalog.get((binding.schema_name or "", binding.object_name or ""))
        types = {c.name: c.canonical_type for c in obj.columns} if obj else {}
        source = ResolvedSource(
            alias=f"t{len(self.sources)}",
            entity=entity.entity,
            schema_name=binding.schema_name,
            object_name=binding.object_name or entity.source.logical_name,
            column_types=types,
            join_keys=join_keys,
        )
        self.sources[entity.entity] = source
        return source

    def _join_to(self, main: Entity, other_name: str) -> Entity | None:
        other = self._usable(other_name)
        if other is None:
            return None
        if other_name in self.sources:
            return other
        for join in self.bundle.joins:
            forward = join.from_entity == main.entity and join.to_entity == other_name
            backward = join.to_entity == main.entity and join.from_entity == other_name
            if forward and join.relationship in {"many_to_one", "one_to_one"}:
                keys = tuple((k.from_field, k.to_field) for k in join.on)
            elif backward and join.relationship in {"one_to_many", "one_to_one"}:
                keys = tuple((k.to_field, k.from_field) for k in join.on)
            else:
                continue
            if len(self.sources) > MAX_JOINS:
                self.issue(IssueCode.TOO_COMPLEX, f"at most {MAX_JOINS} related entities per query")
                return None
            self._source(other, keys)
            return other
        self.issue(
            IssueCode.JOIN_NOT_ALLOWED,
            f"'{other_name}' cannot be combined with '{main.entity}' "
            "(no approved many-to-one join)",
        )
        return None

    # ------------------------------------------------------------ fields
    def _field_owner(self, main: Entity, ref: str) -> tuple[Entity, str] | None:
        if "." in ref:
            entity_name, name = ref.split(".", 1)
            if entity_name == main.entity:
                return main, name
            other = self._join_to(main, entity_name)
            return (other, name) if other else None
        return main, ref

    def _check_policy(self, entity: Entity, name: str, physical: str | None) -> bool:
        candidates = {name, physical} - {None}
        if any(
            c in entity.security.forbidden_fields or self.policy.is_forbidden(str(c))
            for c in candidates
        ):
            self.issue(
                IssueCode.FORBIDDEN_FIELD, f"'{name}' is restricted data and cannot be queried"
            )
            return False
        return True

    def dimension(
        self, main: Entity, ref: str, grain: TimeGrain | None
    ) -> ResolvedDimension | None:
        owner = self._field_owner(main, ref)
        if owner is None:
            return None
        entity, name = owner
        dim = next((d for d in entity.dimensions if d.name == name), None)
        time = next((t for t in entity.time_fields if t.name == name), None)
        if dim is None and time is None:
            if self._check_policy(entity, name, None):
                self.issue(
                    IssueCode.UNKNOWN_FIELD, f"'{ref}' is not a dimension of {entity.entity}"
                )
            return None
        physical = dim.field if dim else time.field  # type: ignore[union-attr]
        if not self._check_policy(entity, name, physical):
            return None
        if dim is not None and dim.sensitive:
            self.issue(IssueCode.SENSITIVE_FIELD, f"'{ref}' is sensitive and cannot be shown")
            return None
        source = self._source(entity) if entity is main else self.sources[entity.entity]
        declared = dim.type if dim else time.type  # type: ignore[union-attr]
        ctype = _effective_type(source.column_types.get(physical), declared)
        return ResolvedDimension(
            label=ref.replace(".", "__").lower(),
            ref=ref,
            source=source.alias,
            field=physical,
            type=ctype,
            time_grain=grain if time is not None else None,
            role=dimension_role(dim) if dim is not None else "time",
        )

    def measure(self, main: Entity, ref: str, depth: int = 0) -> ResolvedMeasure | None:
        if "." in ref and ref.split(".", 1)[0] != main.entity:
            self.issue(
                IssueCode.JOIN_MEASURE_NOT_ALLOWED,
                f"measures must come from {main.entity}, not '{ref}'",
            )
            return None
        name = ref.split(".", 1)[-1]
        source = self._source(main)
        m = next((x for x in main.measures if x.name == name), None)
        if m is not None:
            if not self._check_policy(main, name, m.field):
                return None
            return ResolvedMeasure(
                label=name,
                ref=ref,
                format=m.format,
                source=source.alias,
                field=m.field,
                aggregation=m.aggregation.value,
                type=m.type,
            )
        metric: Metric | None = next(
            (x for x in self.bundle.metrics if x.entity == main.entity and x.name == name), None
        )
        if metric is None or depth > 3:
            if self._check_policy(main, name, None):
                self.issue(
                    IssueCode.UNKNOWN_FIELD, f"'{ref}' is not a measure or metric of {main.entity}"
                )
            return None
        left = self.measure(main, metric.expression.left, depth + 1)
        right = self.measure(main, metric.expression.right, depth + 1)
        if left is None or right is None:
            return None
        return ResolvedMeasure(
            label=name,
            ref=ref,
            format=metric.format,
            op=metric.expression.op,
            left=left,
            right=right,
            type=CanonicalType.DECIMAL,
        )

    def time_field(self, main: Entity, ref: str | None) -> ResolvedDimension | None:
        if ref is None:
            default = (
                main.default_time_field
                or next((t.name for t in main.time_fields if t.default), None)
                or (main.time_fields[0].name if main.time_fields else None)
            )
            if default is None:
                self.issue(
                    IssueCode.INVALID_PERIOD, f"{main.entity} has no time field for a period"
                )
                return None
            ref = default
        owner = self._field_owner(main, ref)
        if owner is None:
            return None
        entity, name = owner
        time = next((t for t in entity.time_fields if t.name == name), None)
        if time is None:
            self.issue(IssueCode.INVALID_PERIOD, f"'{ref}' is not a time field of {entity.entity}")
            return None
        source = (
            self.sources[entity.entity] if entity.entity in self.sources else self._source(entity)
        )
        return ResolvedDimension(
            label=ref,
            ref=ref,
            source=source.alias,
            field=time.field,
            type=_effective_type(source.column_types.get(time.field), time.type),
            role="time",
        )

    def filter(self, main: Entity, f: Any) -> ResolvedFilter | None:
        owner = self._field_owner(main, f.field)
        if owner is None:
            return None
        entity, name = owner
        if any(m.name == name for m in entity.measures) or any(
            m.name == name and m.entity == entity.entity for m in self.bundle.metrics
        ):
            self.issue(
                IssueCode.MEASURE_FILTER_NOT_SUPPORTED,
                f"filters on measures ('{f.field}') are not supported; filter on a dimension",
            )
            return None
        dim = self.dimension(main, f.field, None)
        if dim is None:
            return None
        op: FilterOp = f.op
        if op in {FilterOp.CONTAINS, FilterOp.STARTS_WITH} and dim.type is not CanonicalType.STRING:
            self.issue(
                IssueCode.INVALID_FILTER, f"'{op.value}' only applies to text fields ('{f.field}')"
            )
            return None
        if (
            op in {FilterOp.GT, FilterOp.GTE, FilterOp.LT, FilterOp.LTE, FilterOp.BETWEEN}
            and dim.type not in ORDERED
        ):
            self.issue(
                IssueCode.INVALID_FILTER,
                f"'{op.value}' needs a numeric or date field ('{f.field}')",
            )
            return None
        try:
            value = None if f.value is None else _coerce(f.value, dim.type)
        except ValueError as exc:
            self.issue(IssueCode.INVALID_FILTER, f"invalid value for '{f.field}': {exc}")
            return None
        return ResolvedFilter(dim.source, dim.field, dim.type, op, value, f.field)

    def row_filters(self) -> list[RowFilterValue]:
        out: list[RowFilterValue] = []
        for entity_name, source in self.sources.items():
            entity = self.bundle.entities[entity_name]
            for rf in entity.security.required_filters:
                kind = rf.value_from.removeprefix("tenant.")
                if kind == "slug":
                    value: Any = self.ctx.tenant_slug
                elif kind == "id":
                    value = str(self.ctx.tenant_id)
                else:
                    value = self.ctx.tenant_settings.get(kind.removeprefix("setting:"))
                if value in (None, ""):
                    self.issue(
                        IssueCode.ROW_FILTER_UNAVAILABLE,
                        f"row filter for {entity_name} cannot be applied (missing {rf.value_from})",
                    )
                    continue
                if rf.field not in source.column_types and source.column_types:
                    self.issue(
                        IssueCode.ROW_FILTER_UNAVAILABLE,
                        f"{entity_name} is missing its row-filter column {rf.field}",
                    )
                    continue
                out.append(RowFilterValue(source.alias, rf.field, str(value)))
        return out


def _default_sort(
    dims: list[ResolvedDimension], measures: list[ResolvedMeasure]
) -> list[tuple[str, str]]:
    time_dims = [d for d in dims if d.type in {CanonicalType.DATE, CanonicalType.TIMESTAMP}]
    if time_dims:
        return [(time_dims[0].label, "asc")]
    if measures:
        return [(measures[0].label, "desc")]
    return [(dims[0].label, "asc")] if dims else []


def resolve_plan(  # noqa: PLR0915 - linear validation pipeline
    plan: QueryPlan,
    *,
    bundle: SemanticBundle,
    bindings: dict[str, EntityBinding],
    catalog: list[ObjectMetadata],
    ctx: PlanContext,
) -> ResolvedPlan:
    if not plan.is_data_plan or plan.entity is None:
        raise ValueError("resolve_plan only accepts data plans")
    r = _Resolver(bundle, bindings, catalog, ctx)
    main = r._usable(plan.entity)
    if main is None:
        raise PlanValidationError(r.issues)
    r._source(main)

    # Time grain needs a time dimension.
    dimension_refs = list(plan.dimensions)
    time_names = {t.name for t in main.time_fields}
    if plan.time_grain and not any(d.split(".")[-1] in time_names for d in dimension_refs):
        default_time = r.time_field(main, plan.date_range.field if plan.date_range else None)
        if default_time is not None:
            dimension_refs.insert(0, default_time.ref)
            r.assumptions.append(f"Grouped by {default_time.ref} ({plan.time_grain.value}).")

    dims = [d for ref in dimension_refs if (d := r.dimension(main, ref, plan.time_grain))]
    measures = [m for ref in plan.measures if (m := r.measure(main, ref))]
    filters = [x for f in plan.filters if (x := r.filter(main, f))]

    # Period (mandatory for large facts; defaulted and disclosed).
    time_filter: TimeFilter | None = None
    period = plan.comparison.current if plan.comparison else plan.date_range
    if period is None and main.large_fact:
        period = DateRange(relative=ctx.default_large_fact_period)
        window = resolve_period(period, ctx.today)
        r.assumptions.append(
            f"No period was given; used {ctx.default_large_fact_period.value.replace('_', ' ')} "
            f"({window.label()})."
        )
    if period is not None:
        tf = r.time_field(main, period.field)
        if tf is not None:
            previous = (
                resolve_period(plan.comparison.previous, ctx.today) if plan.comparison else None
            )
            time_filter = TimeFilter(
                tf.source, tf.field, tf.type, tf.ref, resolve_period(period, ctx.today), previous
            )

    # Sort must reference selected output.
    labels = {d.ref: d.label for d in dims} | {m.ref: m.label for m in measures}
    sort: list[tuple[str, str]] = []
    if plan.sort is not None:
        s: SortSpec = plan.sort
        if s.by in labels:
            sort = [(labels[s.by], s.direction)]
        else:
            r.issue(
                IssueCode.INVALID_SORT, f"sort field '{s.by}' must be one of the selected fields"
            )
    if not sort:
        sort = _default_sort(dims, measures)

    limit = min(plan.limit, ctx.max_rows)
    if limit < plan.limit:
        r.assumptions.append(f"Result limited to {limit} rows (platform maximum).")
    if not dims and not measures and not r.issues:
        r.issue(IssueCode.UNKNOWN_FIELD, "no valid dimensions or measures")

    row_filters = r.row_filters()
    if r.issues:
        raise PlanValidationError(r.issues)

    windows = {}
    if time_filter is not None:
        windows["current"] = time_filter.window
        if time_filter.previous is not None:
            windows["previous"] = time_filter.previous
    digest = plan_hash(
        plan,
        windows,
        {
            "tenant": str(ctx.tenant_id),
            "connection": str(ctx.connection_id),
            "semantic_version": str(ctx.semantic_version),
            "row_filters": ",".join(sorted(f"{x.field}={x.value}" for x in row_filters)),
            "limit": str(limit),
            "assumed_period": str(time_filter.window.start) if time_filter else "",
        },
    )
    return ResolvedPlan(
        plan=plan,
        entity=main.entity,
        sources=list(r.sources.values()),
        dimensions=dims,
        measures=measures,
        filters=filters,
        row_filters=row_filters,
        time_filter=time_filter,
        sort=sort,
        limit=limit,
        assumptions=r.assumptions,
        plan_hash=digest,
    )
