"""Turn raw result rows into a typed, UI-ready, JSON-safe result.

* Values are converted per column format (currency/decimal rounding, ISO dates).
* KPI cards: every measure when there are no dimensions; totals of *additive*
  measures (sum/count) otherwise - never for averages or distinct counts, and
  never when the result is truncated (a partial total would mislead).
* Comparisons merge current/previous periods by dimension key and add
  ``_previous``, ``_change`` and ``_change_pct`` columns.
* Chart suggestion is deterministic from the result shape - and then *validated*
  against the data: a field that is free text, an identifier, blank for most records
  or a single value is never a chart axis (``validate_chart``). A rejected chart is
  disabled with its reasons and the answer falls back to KPI cards or a table.
* Every breakdown field is profiled (NULL / blank / placeholder counts, distinct
  meaningful values, weighted by record counts) and missing data is reported as a
  warning ("Source is missing for 10 of 20 leads") instead of becoming a category.
* Last line of defence: any output column whose name matches a sensitive-field
  policy is redacted (the validator should already have made this impossible).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.connectors.common.types import CanonicalType, QueryResult
from app.query.compiler import OutputColumn
from app.query.quality import DimensionQuality, is_blank, profile_dimension
from app.query.validator import ResolvedMeasure, ResolvedPlan
from app.semantic.models import ValueFormat
from app.semantic.policy import SensitiveFieldPolicy

JsonScalar = str | int | float | bool | None
REDACTED = "[REDACTED]"
ADDITIVE = frozenset({"sum", "count"})
COUNTS = frozenset({"count", "count_distinct"})
MAX_CHART_CATEGORIES = 50
MAX_PIE_SLICES = 6
ChartHint = Literal["share", "relationship"]
_DECIMALS = {ValueFormat.CURRENCY: 2, ValueFormat.PERCENT: 2, ValueFormat.DECIMAL: 4}


class ColumnMeta(BaseModel):
    name: str
    label: str
    kind: Literal["dimension", "time", "measure", "previous", "change", "change_pct"]
    type: CanonicalType
    format: ValueFormat | None = None
    time_grain: str | None = None
    role: str | None = None  # dimensions: category | name | identifier | free_text | time


class KpiCard(BaseModel):
    name: str
    label: str
    value: JsonScalar
    format: ValueFormat | None
    previous: JsonScalar = None
    change: JsonScalar = None
    change_pct: float | None = None
    scope: Literal["value", "total"] = "value"


class ChartValidation(BaseModel):
    valid: bool = True
    reasons: list[str] = Field(default_factory=list)


class ChartSpec(BaseModel):
    type: Literal["kpi", "bar", "grouped_bar", "line", "table", "pie", "scatter"]
    x: str | None = None
    y: list[str] = Field(default_factory=list)
    series: str | None = None
    # False when validation rejected the chart; ``type`` is then the fallback view.
    enabled: bool = True
    title: str | None = None
    validation: ChartValidation = Field(default_factory=ChartValidation)


class FormattedResult(BaseModel):
    columns: list[ColumnMeta]
    rows: list[list[JsonScalar]]
    row_count: int
    truncated: bool
    kpis: list[KpiCard]
    chart: ChartSpec
    redacted_columns: list[str] = Field(default_factory=list)
    quality: list[DimensionQuality] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


def humanize(name: str) -> str:
    text = name.replace("__", " ").replace("_", " ").strip()
    return text[:1].upper() + text[1:]


def to_json(value: Any, fmt: ValueFormat | None = None) -> JsonScalar:
    if value is None or isinstance(value, bool | str):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal | float):
        if fmt is ValueFormat.INTEGER:
            return round(float(value))
        return round(float(value), _DECIMALS.get(fmt or ValueFormat.DECIMAL, 4))
    if isinstance(value, int):
        return value
    if isinstance(value, bytes):
        return REDACTED  # binary data is never returned
    return str(value)


def _num(value: Any) -> float | None:
    return (
        float(value)
        if isinstance(value, int | float | Decimal) and not isinstance(value, bool)
        else None
    )


def _change(current: Any, previous: Any) -> tuple[float | None, float | None]:
    cur, prev = _num(current), _num(previous)
    if cur is None or prev is None:
        return None, None
    delta = cur - prev
    return delta, (round(delta / prev * 100, 2) if prev else None)


def _leaf_aggregations(m: ResolvedMeasure) -> set[str]:
    if m.op is None:
        return {m.aggregation or ""}
    return {"metric"}


def _chart(
    output: list[OutputColumn],
    row_count: int,
    comparison: bool,
    hint: ChartHint | None = None,
) -> ChartSpec:
    dims = [c for c in output if c.kind != "measure"]
    measures = [c.name for c in output if c.kind == "measure"]
    if not dims:
        return ChartSpec(type="kpi", y=measures)
    additive = [c.name for c in output if c.kind == "measure" and c.aggregation in ADDITIVE]
    if hint == "share" and len(dims) == 1 and dims[0].kind != "time" and additive:
        return ChartSpec(type="pie", x=dims[0].name, y=additive[:1])
    if hint == "relationship" and len(dims) == 1 and len(measures) >= 2 and not comparison:
        return ChartSpec(type="scatter", x=dims[0].name, y=measures[:2])
    time = next((c for c in dims if c.kind == "time"), None)
    if time is not None and len(dims) <= 2 and not comparison:
        other = next((c.name for c in dims if c is not time), None)
        return ChartSpec(type="line", x=time.name, y=measures[:3], series=other)
    if not measures or len(dims) > 2 or row_count > 50:
        return ChartSpec(type="table")
    if comparison:
        return ChartSpec(
            type="grouped_bar", x=dims[0].name, y=[measures[0], f"{measures[0]}_previous"]
        )
    if len(dims) == 2:
        return ChartSpec(type="grouped_bar", x=dims[0].name, y=measures[:1], series=dims[1].name)
    return ChartSpec(type="bar", x=dims[0].name, y=measures[:3])


def format_result(
    resolved: ResolvedPlan,
    output: list[OutputColumn],
    current: QueryResult,
    previous: QueryResult | None,
    policy: SensitiveFieldPolicy,
    *,
    max_blank_ratio: float = 0.5,
    noun: str = "records",
    chart_hint: ChartHint | None = None,
) -> FormattedResult:
    formats = {c.name: c.format for c in output}
    measures = [c for c in output if c.kind == "measure"]
    dims = [c for c in output if c.kind != "measure"]
    columns = [
        ColumnMeta(
            name=c.name,
            label=humanize(c.ref),
            kind=c.kind,  # type: ignore[arg-type]
            type=c.type,
            format=c.format,
            time_grain=c.time_grain.value if c.time_grain else None,
            role=c.role,
        )
        for c in output
    ]
    raw_rows = [list(r) for r in current.rows]

    if previous is not None:
        prev_by_key = {tuple(r[: len(dims)]): r for r in previous.rows}
        merged = []
        for row in raw_rows:
            key = tuple(row[: len(dims)])
            prev = prev_by_key.get(key)
            extra: list[Any] = []
            for i, _ in enumerate(measures):
                cur_v = row[len(dims) + i]
                prev_v = prev[len(dims) + i] if prev is not None else None
                delta, pct = _change(cur_v, prev_v)
                extra += [prev_v, delta, pct]
            merged.append(row + extra)
        raw_rows = merged
        for m in measures:
            columns += [
                ColumnMeta(
                    name=f"{m.name}_previous",
                    label=f"{humanize(m.ref)} (previous)",
                    kind="previous",
                    type=m.type,
                    format=m.format,
                ),
                ColumnMeta(
                    name=f"{m.name}_change",
                    label=f"{humanize(m.ref)} change",
                    kind="change",
                    type=m.type,
                    format=m.format,
                ),
                ColumnMeta(
                    name=f"{m.name}_change_pct",
                    label=f"{humanize(m.ref)} change %",
                    kind="change_pct",
                    type=CanonicalType.FLOAT,
                    format=ValueFormat.PERCENT,
                ),
            ]
            formats[f"{m.name}_previous"] = m.format
            formats[f"{m.name}_change"] = m.format
            formats[f"{m.name}_change_pct"] = ValueFormat.PERCENT

    redacted = [c.name for c in columns if policy.classify(c.name.split("__")[-1]) is not None]
    redact_idx = {i for i, c in enumerate(columns) if c.name in redacted}
    rows = [
        [
            REDACTED if i in redact_idx else to_json(v, formats.get(columns[i].name))
            for i, v in enumerate(r)
        ]
        for r in raw_rows
    ]

    kpis: list[KpiCard] = []
    by_label = {m.label: m for m in resolved.measures}
    if not dims and raw_rows:
        row = raw_rows[0]
        for i, m in enumerate(measures):
            cur_v = row[i]
            prev_v = row[len(measures) + 3 * i] if previous is not None else None
            delta, pct = _change(cur_v, prev_v) if previous is not None else (None, None)
            kpis.append(
                KpiCard(
                    name=m.name,
                    label=humanize(m.ref),
                    value=to_json(cur_v, m.format),
                    format=m.format,
                    previous=to_json(prev_v, m.format),
                    change=to_json(delta, m.format),
                    change_pct=pct,
                )
            )
    elif dims and not current.truncated:
        for i, m in enumerate(measures):
            if _leaf_aggregations(by_label[m.name]) <= ADDITIVE:
                total = sum((_num(r[len(dims) + i]) or 0.0) for r in current.rows)
                kpis.append(
                    KpiCard(
                        name=m.name,
                        label=_total_label(humanize(m.ref)),
                        value=to_json(total, m.format),
                        format=m.format,
                        scope="total",
                    )
                )
    quality = profile_result(columns, output, rows, max_blank_ratio)
    chart = _chart(output, current.row_count, previous is not None, chart_hint)
    validation = validate_chart(chart, columns, rows, quality)
    if validation.valid:
        chart = chart.model_copy(update={"title": chart_title(chart, columns)})
    else:
        every_dim_unusable = all(not q.usable for q in quality)
        chart = chart.model_copy(
            update={
                "type": "kpi" if kpis and every_dim_unusable else "table",
                "enabled": False,
                "validation": validation,
            }
        )
    return FormattedResult(
        columns=columns,
        rows=rows,
        row_count=current.row_count,
        truncated=current.truncated,
        kpis=kpis,
        chart=chart,
        redacted_columns=redacted,
        quality=quality,
        warnings=quality_warnings(quality, noun),
    )


# ---------------------------------------------------------------- validation
def profile_result(
    columns: list[ColumnMeta],
    output: list[OutputColumn],
    rows: list[list[JsonScalar]],
    max_blank_ratio: float,
) -> list[DimensionQuality]:
    """Profile every dimension/time column, weighted by the first count measure."""
    counts = next(
        (
            i
            for i, c in enumerate(output)
            if c.kind == "measure" and c.aggregation in COUNTS and i < len(columns)
        ),
        None,
    )
    weights = [float(_num(r[counts]) or 0.0) for r in rows] if counts is not None else None
    profiles: list[DimensionQuality] = []
    for i, column in enumerate(columns):
        if column.kind not in {"dimension", "time"}:
            continue
        role = column.role or ("time" if column.kind == "time" else "category")
        profiles.append(
            profile_dimension(
                name=column.name,
                label=column.label,
                role=role,  # type: ignore[arg-type]
                values=[r[i] for r in rows],
                weights=weights,
                max_blank_ratio=max_blank_ratio,
            )
        )
    return profiles


def validate_chart(
    chart: ChartSpec,
    columns: list[ColumnMeta],
    rows: list[list[JsonScalar]],
    quality: list[DimensionQuality],
) -> ChartValidation:
    """Is this chart a truthful picture of meaningful data?"""
    if chart.type in {"kpi", "table"}:
        return ChartValidation()
    by_name = {c.name: c for c in columns}
    profiles = {q.name: q for q in quality}
    reasons: list[str] = []
    x = by_name.get(chart.x or "")
    if x is None:
        reasons.append("the chart's category field is not in the result")
    else:
        q = profiles.get(x.name)
        if q is not None:
            if not q.usable:
                reasons.append(f"{x.label} is {q.reason}")
            elif q.distinct_values < 2:
                reasons.append(
                    "a trend needs at least two periods"
                    if chart.type == "line"
                    else f"{x.label} has a single value, so there is nothing to compare"
                )
            elif chart.type != "line" and q.distinct_values > MAX_CHART_CATEGORIES:
                reasons.append(f"{x.label} has too many values ({q.distinct_values}) to chart")
            elif chart.type == "pie" and q.distinct_values > MAX_PIE_SLICES:
                reasons.append(f"{x.label} has too many values for a pie chart")
    for name in chart.y:
        column = by_name.get(name)
        if column is None or column.kind not in {"measure", "previous"}:
            reasons.append(f"'{name}' is not a measure")
    if chart.type == "pie":
        index = next((i for i, c in enumerate(columns) if c.name in chart.y), None)
        if index is not None and any((_num(r[index]) or 0) < 0 for r in rows):
            reasons.append("a pie chart cannot show negative values")
    if chart.series is not None:
        series = profiles.get(chart.series)
        if series is not None and not series.usable:
            reasons.append(f"{series.label} is {series.reason}")
    return ChartValidation(valid=not reasons, reasons=reasons)


def explainable_data(result: FormattedResult) -> tuple[list[str], list[list[JsonScalar]]]:
    """What a narrative may describe: no blank category and no rejected breakdown.

    Rows whose label is blank are left out (their count is in ``warnings``); when every
    breakdown was rejected (free text, identifiers, mostly blank) only the figures remain.
    """
    unfit = {q.name for q in result.quality if not q.usable}
    labels = [i for i, c in enumerate(result.columns) if c.kind in {"dimension", "time"}]
    kept_labels = [i for i in labels if result.columns[i].name not in unfit]
    if labels and not kept_labels and result.kpis:
        return [k.name for k in result.kpis], [[k.value for k in result.kpis]]
    keep = [i for i, c in enumerate(result.columns) if i not in labels or i in kept_labels]
    rows = [
        [row[i] for i in keep]
        for row in result.rows
        if not any(is_blank(row[i]) for i in kept_labels)
    ]
    return [result.columns[i].name for i in keep], rows


def quality_warnings(quality: list[DimensionQuality], noun: str) -> list[str]:
    """Missing data, stated once and plainly - never shown as a "(blank)" category."""
    warnings: list[str] = []
    for q in quality:
        if q.role == "identifier" or q.total <= 0:
            continue
        if q.missing >= q.total:
            warnings.append(f"{q.label} is not recorded for any of the {q.total} {noun}.")
        elif q.missing > 0:
            warnings.append(f"{q.label} is missing for {q.missing} of {q.total} {noun}.")
        if q.role == "free_text":
            warnings.append(f"{q.label} is free text, so it is not used as a chart category.")
    return warnings


def chart_title(chart: ChartSpec, columns: list[ColumnMeta]) -> str | None:
    labels = {c.name: c.label for c in columns}
    if chart.type in {"kpi", "table"} or chart.x not in labels or not chart.y:
        return None
    x = labels[chart.x or ""]
    measure = labels.get(chart.y[0], chart.y[0])
    if chart.type == "scatter" and len(chart.y) > 1:
        return f"{measure} vs {labels.get(chart.y[1], chart.y[1]).lower()} by {x.lower()}"
    if chart.type == "pie":
        return f"Share of {measure.lower()} by {x.lower()}"
    series = f" and {labels[chart.series].lower()}" if chart.series in labels else ""
    return f"{measure} by {x.lower()}{series}"


def _total_label(label: str) -> str:
    return label if label.lower().startswith("total") else f"Total {label.lower()}"
