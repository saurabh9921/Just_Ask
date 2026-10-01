"""Semantic layer models.

The semantic layer is the only vocabulary the AI may use: entities (backed by
approved reporting views), dimensions, measures, time fields, derived metrics,
approved joins, glossary terms and example questions. Everything is validated
with ``extra="forbid"`` so a typo in YAML fails loudly instead of silently
widening what the model can reach.

No free-form SQL exists anywhere in the semantic layer: derived metrics use a
small structured expression language, and row-level filters reference principal
attributes rather than SQL snippets.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from app.connectors.common.types import CanonicalType

Identifier = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,62}$")]
EntityName = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Za-z0-9]{0,62}$")]
# Physical names: plain identifiers only (no quoting tricks, no dots, no whitespace).
PhysicalName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_$]{0,127}$")]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
Synonym = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Aggregation(StrEnum):
    SUM = "sum"
    COUNT = "count"
    COUNT_DISTINCT = "count_distinct"
    AVG = "avg"
    MIN = "min"
    MAX = "max"


class ValueFormat(StrEnum):
    CURRENCY = "currency"
    INTEGER = "integer"
    DECIMAL = "decimal"
    PERCENT = "percent"
    DATE = "date"
    TEXT = "text"


class Classification(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


class TimeGrain(StrEnum):
    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    QUARTER = "quarter"
    YEAR = "year"


# Numeric aggregations are only valid on numeric canonical types.
NUMERIC_TYPES = frozenset({CanonicalType.INTEGER, CanonicalType.DECIMAL, CanonicalType.FLOAT})
NUMERIC_AGGREGATIONS = frozenset({Aggregation.SUM, Aggregation.AVG})


class Source(_Model):
    logical_name: PhysicalName
    allowed_database_objects: list[PhysicalName] = Field(min_length=1, max_length=10)
    # Optional schema qualifier; defaults to the connection's schema.
    schema_name: PhysicalName | None = None

    @model_validator(mode="after")
    def _logical_is_allowed(self) -> Self:
        if self.logical_name not in self.allowed_database_objects:
            raise ValueError("logical_name must be one of allowed_database_objects")
        return self


DimensionRole = Literal["category", "name", "identifier", "free_text"]


class Dimension(_Model):
    name: Identifier
    field: PhysicalName
    type: CanonicalType = CanonicalType.STRING
    description: Text | None = None
    synonyms: list[Synonym] = Field(default_factory=list, max_length=30)
    sensitive: bool = False
    # What the field means to the business (see ``app.semantic.roles``); inferred from
    # the name when omitted. Free text and identifiers are never charted automatically.
    role: DimensionRole | None = None


class Measure(_Model):
    name: Identifier
    field: PhysicalName
    aggregation: Aggregation
    type: CanonicalType = CanonicalType.DECIMAL
    format: ValueFormat = ValueFormat.DECIMAL
    description: Text | None = None
    synonyms: list[Synonym] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def _numeric_aggregation(self) -> Self:
        if self.aggregation in NUMERIC_AGGREGATIONS and self.type not in NUMERIC_TYPES:
            raise ValueError(
                f"{self.aggregation.value} requires a numeric type, got {self.type.value}"
            )
        return self


class TimeField(_Model):
    name: Identifier
    field: PhysicalName
    type: Literal[CanonicalType.DATE, CanonicalType.TIMESTAMP] = CanonicalType.DATE
    default: bool = False
    grains: list[TimeGrain] = Field(default_factory=lambda: list(TimeGrain))
    description: Text | None = None


class RowFilter(_Model):
    """A mandatory row-level predicate: ``field = <principal attribute>``.

    ``value_from`` is one of ``tenant.slug``, ``tenant.id`` or
    ``tenant.setting:<key>`` - never literal SQL.
    """

    field: PhysicalName
    value_from: Annotated[
        str, StringConstraints(pattern=r"^tenant\.(slug|id|setting:[a-z0-9_]{1,63})$")
    ]


class Security(_Model):
    required_filters: list[RowFilter] = Field(default_factory=list, max_length=10)
    forbidden_fields: list[PhysicalName] = Field(default_factory=list, max_length=200)
    required_permissions: list[Annotated[str, StringConstraints(pattern=r"^[a-z]+\.[a-z]+$")]] = (
        Field(default_factory=lambda: ["analytics.query"])
    )

    @field_validator("required_filters", mode="before")
    @classmethod
    def _shorthand(cls, value: object) -> object:
        """``- tenant_id`` shorthand == ``{field: tenant_id, value_from: tenant.slug}``."""
        if isinstance(value, list):
            return [
                {"field": item, "value_from": "tenant.slug"} if isinstance(item, str) else item
                for item in value
            ]
        return value


class Formatting(_Model):
    currency: Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")] = "USD"
    locale: Annotated[str, StringConstraints(pattern=r"^[a-z]{2}(-[A-Z]{2})?$")] = "en-US"


class Entity(_Model):
    entity: EntityName
    description: Text
    source: Source
    dimensions: list[Dimension] = Field(default_factory=list, max_length=200)
    measures: list[Measure] = Field(default_factory=list, max_length=200)
    time_fields: list[TimeField] = Field(default_factory=list, max_length=20)
    synonyms: list[Synonym] = Field(default_factory=list, max_length=30)
    security: Security = Field(default_factory=Security)
    classification: Classification = Classification.INTERNAL
    # Large fact tables must be queried with a date filter (enforced in Phase 4).
    large_fact: bool = False
    default_time_field: Identifier | None = None
    formatting: Formatting = Field(default_factory=Formatting)

    @model_validator(mode="after")
    def _consistency(self) -> Self:
        names = (
            [d.name for d in self.dimensions]
            + [m.name for m in self.measures]
            + [t.name for t in self.time_fields]
        )
        duplicates = sorted({n for n in names if names.count(n) > 1})
        if duplicates:
            raise ValueError(f"duplicate field names: {duplicates}")
        forbidden = set(self.security.forbidden_fields)
        exposed = (
            {d.field for d in self.dimensions}
            | {m.field for m in self.measures}
            | {t.field for t in self.time_fields}
        )
        leaked = sorted(forbidden & exposed)
        if leaked:
            raise ValueError(f"forbidden fields must not be exposed: {leaked}")
        if self.default_time_field and self.default_time_field not in {
            t.name for t in self.time_fields
        }:
            raise ValueError("default_time_field must name a time field")
        if self.large_fact and not self.time_fields:
            raise ValueError("large_fact entities need at least one time field")
        return self

    def physical_fields(self) -> set[str]:
        return (
            {d.field for d in self.dimensions}
            | {m.field for m in self.measures}
            | {t.field for t in self.time_fields}
            | {f.field for f in self.security.required_filters}
        )


class EntityFile(_Model):
    """One YAML file may hold a single entity or a list under ``entities``."""

    entities: list[Entity] = Field(min_length=1, max_length=100)


# ------------------------------------------------------------------ metrics
MetricOp = Literal["add", "subtract", "multiply", "divide", "percent_of"]


class MetricExpression(_Model):
    op: MetricOp
    left: Identifier  # a measure (or metric) of the same entity
    right: Identifier


class Metric(_Model):
    name: Identifier
    entity: EntityName
    description: Text
    expression: MetricExpression
    format: ValueFormat = ValueFormat.DECIMAL
    synonyms: list[Synonym] = Field(default_factory=list, max_length=30)


class MetricFile(_Model):
    metrics: list[Metric] = Field(min_length=1, max_length=500)


# -------------------------------------------------------------------- joins
class JoinKey(_Model):
    from_field: PhysicalName
    to_field: PhysicalName


class Join(_Model):
    name: Identifier
    from_entity: EntityName = Field(alias="from")
    to_entity: EntityName = Field(alias="to")
    relationship: Literal["many_to_one", "one_to_one", "one_to_many"] = "many_to_one"
    on: list[JoinKey] = Field(min_length=1, max_length=5)

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    @model_validator(mode="before")
    @classmethod
    def _yaml_on_key(cls, data: Any) -> Any:
        """YAML 1.1 parses an unquoted ``on:`` key as boolean ``True``; accept it."""
        if isinstance(data, dict) and True in data and "on" not in data:
            data = {("on" if k is True else k): v for k, v in data.items()}
        return data


class JoinFile(_Model):
    joins: list[Join] = Field(default_factory=list, max_length=500)


# --------------------------------------------------------- glossary/synonyms
class TermTarget(_Model):
    entity: EntityName
    measure: Identifier | None = None
    dimension: Identifier | None = None
    metric: Identifier | None = None


class GlossaryTerm(_Model):
    term: Synonym
    definition: Text
    synonyms: list[Synonym] = Field(default_factory=list, max_length=30)
    maps_to: TermTarget | None = None


class Abbreviation(_Model):
    abbreviation: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9&/.-]{1,20}$")]
    expansion: Synonym


class GlossaryFile(_Model):
    terms: list[GlossaryTerm] = Field(default_factory=list, max_length=2000)
    abbreviations: list[Abbreviation] = Field(default_factory=list, max_length=2000)


# ----------------------------------------------------------------- examples
class ExampleFile(_Model):
    examples: list[QueryExample] = Field(min_length=1, max_length=5000)


class QueryExample(_Model):
    id: Annotated[str, StringConstraints(pattern=r"^[a-z0-9-]{1,40}$")]
    question: Text
    intent: Literal[
        "aggregation", "ranking", "comparison", "trend", "detail", "explanation", "clarification"
    ]
    entity: EntityName
    dimensions: list[Identifier] = Field(default_factory=list)
    measures: list[Identifier] = Field(default_factory=list)
    time_field: Identifier | None = None
    notes: Text | None = None
    # Structured plan sketch; the full QueryPlan model arrives in Phase 4.
    plan: dict[str, Any] = Field(default_factory=dict)


# ------------------------------------------------------------------ policies
class SensitivePattern(_Model):
    pattern: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    classification: Classification
    action: Literal["forbid", "mask"]
    description: Text | None = None

    @field_validator("pattern")
    @classmethod
    def _compiles(cls, value: str) -> str:
        re.compile(value)
        return value


class PolicyFile(_Model):
    sensitive_fields: list[SensitivePattern] = Field(default_factory=list, max_length=500)


ExampleFile.model_rebuild()
