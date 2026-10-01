"""The database-independent query plan.

This is the *only* thing the model produces for a data question. It references
semantic-layer names (never physical tables, columns or SQL), is validated with
``extra="forbid"`` and bounded everywhere, and is then independently checked,
resolved and compiled by deterministic code.

Shapes (all used by ``semantic-layer/examples``):

* data plan      - ``entity`` + ``measures``/``dimensions`` (+ filters, period, sort...)
* comparison     - ``comparison: {current: <period>, previous: <period>}``
* clarification  - ``clarification_required: true`` + ``clarification_question``
* refusal        - ``refuse: true`` + ``reason`` (sensitive/forbidden data, writes)
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.semantic.models import TimeGrain

EntityName = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Za-z0-9]{0,62}$")]
# ``field`` or ``Entity.field`` (a field of an entity reachable through an approved join).
FieldRef = Annotated[
    str, StringConstraints(pattern=r"^([A-Z][A-Za-z0-9]{0,62}\.)?[a-z][a-z0-9_]{0,62}$")
]
ScalarValue = Annotated[str, StringConstraints(max_length=200)] | int | float | bool | date
MAX_IN_VALUES = 100


class RelativePeriod(StrEnum):
    TODAY = "today"
    YESTERDAY = "yesterday"
    LAST_7_DAYS = "last_7_days"
    LAST_30_DAYS = "last_30_days"
    LAST_90_DAYS = "last_90_days"
    THIS_WEEK = "this_week"
    LAST_WEEK = "last_week"
    THIS_MONTH = "this_month"
    LAST_MONTH = "last_month"
    THIS_QUARTER = "this_quarter"
    LAST_QUARTER = "last_quarter"
    QUARTER_BEFORE_LAST = "quarter_before_last"
    LAST_6_MONTHS = "last_6_months"
    LAST_12_MONTHS = "last_12_months"
    THIS_YEAR = "this_year"
    YEAR_TO_DATE = "year_to_date"
    LAST_YEAR = "last_year"
    LAST_N_DAYS = "last_n_days"
    LAST_N_MONTHS = "last_n_months"


class FilterOp(StrEnum):
    EQ = "eq"
    NE = "ne"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    IN = "in"
    NOT_IN = "not_in"
    BETWEEN = "between"
    CONTAINS = "contains"
    STARTS_WITH = "starts_with"
    IS_NULL = "is_null"
    IS_NOT_NULL = "is_not_null"


class RefusalReason(StrEnum):
    FORBIDDEN_FIELD = "FORBIDDEN_FIELD"
    SENSITIVE_FIELD = "SENSITIVE_FIELD"
    WRITE_NOT_ALLOWED = "WRITE_NOT_ALLOWED"
    OUT_OF_SCOPE = "OUT_OF_SCOPE"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class QueryFilter(_Strict):
    field: FieldRef
    op: FilterOp
    value: ScalarValue | list[ScalarValue] | None = None

    @model_validator(mode="after")
    def _value_shape(self) -> Self:
        op, value = self.op, self.value
        if op in {FilterOp.IS_NULL, FilterOp.IS_NOT_NULL}:
            if value is not None:
                raise ValueError(f"{op.value} takes no value")
        elif op in {FilterOp.IN, FilterOp.NOT_IN}:
            if not isinstance(value, list) or not 1 <= len(value) <= MAX_IN_VALUES:
                raise ValueError(f"{op.value} needs a list of 1..{MAX_IN_VALUES} values")
        elif op is FilterOp.BETWEEN:
            if not isinstance(value, list) or len(value) != 2:
                raise ValueError("between needs exactly two values")
        elif value is None or isinstance(value, list):
            raise ValueError(f"{op.value} needs a single value")
        if op in {FilterOp.CONTAINS, FilterOp.STARTS_WITH} and not isinstance(value, str):
            raise ValueError(f"{op.value} needs a text value")
        return self


class DateRange(_Strict):
    field: FieldRef | None = None  # defaults to the entity's default time field
    relative: RelativePeriod | None = None
    n: int | None = Field(default=None, ge=1, le=3660)
    start: date | None = None
    end: date | None = None  # inclusive

    @model_validator(mode="after")
    def _one_form(self) -> Self:
        absolute = self.start is not None or self.end is not None
        if self.relative is None and not absolute:
            raise ValueError("date_range needs relative or start/end")
        if self.relative is not None and absolute:
            raise ValueError("use either relative or start/end, not both")
        if absolute and (self.start is None or self.end is None or self.start > self.end):
            raise ValueError("start and end are both required and start <= end")
        needs_n = self.relative in {RelativePeriod.LAST_N_DAYS, RelativePeriod.LAST_N_MONTHS}
        if needs_n and self.n is None:
            raise ValueError(f"{self.relative} needs n")
        if not needs_n and self.n is not None:
            raise ValueError("n is only valid with last_n_days/last_n_months")
        return self


class Comparison(_Strict):
    current: DateRange
    previous: DateRange


class SortSpec(_Strict):
    by: FieldRef
    direction: Literal["asc", "desc"] = "desc"


class QueryPlan(_Strict):
    entity: EntityName | None = None
    dimensions: list[FieldRef] = Field(default_factory=list, max_length=8)
    measures: list[FieldRef] = Field(default_factory=list, max_length=10)
    filters: list[QueryFilter] = Field(default_factory=list, max_length=20)
    date_range: DateRange | None = None
    comparison: Comparison | None = None
    time_grain: TimeGrain | None = None
    sort: SortSpec | None = None
    limit: int = Field(default=100, ge=1, le=1000)
    confidence: float = Field(default=1.0, ge=0, le=1)
    clarification_required: bool = False
    clarification_question: Annotated[str, StringConstraints(max_length=300)] | None = None
    refuse: bool = False
    reason: RefusalReason | None = None

    @model_validator(mode="after")
    def _shape(self) -> Self:
        if self.refuse:
            if self.reason is None:
                raise ValueError("refuse requires reason")
            return self
        if self.clarification_required:
            if not self.clarification_question:
                raise ValueError("clarification_required needs clarification_question")
            return self
        if self.entity is None:
            raise ValueError("entity is required for a data plan")
        if not self.measures and not self.dimensions:
            raise ValueError("a data plan needs at least one measure or dimension")
        if self.date_range is not None and self.comparison is not None:
            raise ValueError("use either date_range or comparison, not both")
        if len(set(self.dimensions)) != len(self.dimensions) or len(set(self.measures)) != len(
            self.measures
        ):
            raise ValueError("dimensions and measures must be unique")
        return self

    @property
    def is_data_plan(self) -> bool:
        return not self.refuse and not self.clarification_required
