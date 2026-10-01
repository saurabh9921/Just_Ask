"""Structured analytics returned next to every answer (``QueryResponse.analysis``).

Built by deterministic code from validated query results - never by a model:

* every metric carries the query it came from (``source``/``query``) and ``validated``;
* metrics that cannot be computed are listed in ``unavailable`` with the reason (a
  missing or empty field), never estimated;
* ``dimensions`` are the data-quality profiles of the fields the answer uses;
* ``chart`` is only enabled after validation (``chart.validation.reasons`` says why not);
* ``validation`` summarises the checks: data, entity resolution, filter applied, chart.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.query.quality import DimensionQuality
from app.query.result_formatter import ChartValidation
from app.semantic.models import ValueFormat


class EntityOut(BaseModel):
    """A business entity named in the question ("pending metals co")."""

    type: str  # "company"
    mention: str  # as written in the question
    name: str | None = None  # the resolved record's name
    resolved: bool
    match: Literal["exact", "normalized", "id", "ambiguous", "suggestions", "not_found"]
    keys: list[Any] = Field(default_factory=list)  # e.g. company ids used as the filter
    candidates: list[str] = Field(default_factory=list)  # shown when not resolved
    filter_field: str | None = None  # the subject's field the filter was applied to


class MetricOut(BaseModel):
    key: str
    label: str
    value: float | int | None
    format: ValueFormat | None = None
    source: Literal["query_result"] = "query_result"
    query: str  # label of the sub-query that produced it (see ``queries``)
    validated: bool = True
    detail: str | None = None


class UnavailableMetricOut(BaseModel):
    key: str
    label: str
    reason: str


class InsightOut(BaseModel):
    text: str
    metrics: list[str] = Field(default_factory=list)  # keys of the metrics it states
    kind: Literal["fact", "observation"] = "fact"


class ChartPointOut(BaseModel):
    label: str
    value: float | int | None


class AnalysisChartOut(BaseModel):
    enabled: bool
    type: Literal["bar", "line", "pie", "scatter"] | None = None
    title: str | None = None
    dimension: str | None = None
    metric: str | None = None
    data: list[ChartPointOut] = Field(default_factory=list)
    validation: ChartValidation = Field(default_factory=ChartValidation)


class AnalysisValidationOut(BaseModel):
    data_validated: bool
    entity_resolved: bool | None = None  # None: the question names no entity
    entity_filter_applied: bool | None = None
    chart_validated: bool
    unsupported_claims: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class AnalysisQueryOut(BaseModel):
    label: str
    sql: str
    row_count: int
    validated: bool = True


class AnalysisOut(BaseModel):
    intent: str  # e.g. "lead_analysis", or the query intent ("aggregation")
    title: str | None = None  # "Lead Analysis — Pending Metals Co"
    entity: EntityOut | None = None
    metrics: list[MetricOut] = Field(default_factory=list)
    unavailable: list[UnavailableMetricOut] = Field(default_factory=list)
    dimensions: list[DimensionQuality] = Field(default_factory=list)
    insights: list[InsightOut] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    chart: AnalysisChartOut = Field(default_factory=lambda: AnalysisChartOut(enabled=False))
    validation: AnalysisValidationOut
    queries: list[AnalysisQueryOut] = Field(default_factory=list)
