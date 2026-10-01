"""Business analysis: "lead analysis for pending metals co" as validated metrics.

An analysis request names a subject ("lead") and, optionally, an entity ("for X").
Instead of one query grouped by whatever field shares a word with the question, the
analysis runs a fixed set of small queries over the subject entity - each through the
same validation, compilation, SQL-safety check and read-only execution as any question:

* the total (count of the entity's key);
* breakdowns by the fields that matter for the subject - status/stage, source,
  priority, tier, type, owner, product, segment, location - each profiled and rejected
  when free text, an identifier, mostly blank or a single value;
* value and score measures, recent records and the monthly trend;
* follow-ups due/overdue, conversion rate and high-priority records - only when the
  fields exist *and* hold data.

Every metric names the query it came from; anything that cannot be computed is listed
with the reason. Insights are sentences over validated metrics only, and the chart is
the first breakdown that passes validation - or none.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Final, Literal

from app.agents.entities import plural_noun, singular, singular_noun
from app.connectors.common.types import QueryResult
from app.query.compiler import CompiledQuery
from app.query.plans import DateRange, FilterOp, QueryFilter, QueryPlan, RelativePeriod
from app.query.quality import DimensionQuality, is_blank, profile_dimension
from app.query.result_formatter import ChartValidation
from app.query.validator import ResolvedPlan
from app.rag.text import tokenize
from app.schemas.insights import (
    AnalysisChartOut,
    AnalysisOut,
    AnalysisQueryOut,
    AnalysisValidationOut,
    ChartPointOut,
    EntityOut,
    InsightOut,
    MetricOut,
    UnavailableMetricOut,
)
from app.semantic.loader import SemanticBundle
from app.semantic.models import Aggregation, Dimension, Entity, Measure, TimeField, TimeGrain
from app.semantic.roles import dimension_role

ANALYSIS_WORDS: Final = (
    r"analysis|analytics|overview|insights?|report|summary|breakdown|health\s+check"
)
_AFTER: Final = re.compile(
    rf"\b(?P<subject>[a-z][a-z ]{{0,60}}?)\s+(?:{ANALYSIS_WORDS})\b", re.IGNORECASE
)
_BEFORE: Final = re.compile(
    r"\b(?:analy[sz]e|analysis\s+of|overview\s+of|summary\s+of|report\s+on|insights?\s+(?:on|into))"
    r"\s+(?:the\s+|my\s+|our\s+|all\s+)?(?P<subject>[a-z][a-z ]{0,40}?)"
    r"(?=\s+(?:for|of|at|about|in|from|during|this|last|by|with)\b|[?.!,]|$)",
    re.IGNORECASE,
)

# "leads by analysis note": the analysis word belongs to a field, not to the request.
_LINKING: Final = frozenset(
    {"by", "per", "across", "of", "for", "with", "from", "in", "on", "to", "about", "vs"}
)

# (key, words that identify the field, label) - in order of importance for an analysis.
BREAKDOWNS: Final[tuple[tuple[str, frozenset[str], str], ...]] = (
    ("status", frozenset({"status", "stage", "phase", "funnel", "verdict", "decision"}), "status"),
    ("source", frozenset({"source", "channel", "origin"}), "source"),
    ("priority", frozenset({"priority", "urgency"}), "priority"),
    ("tier", frozenset({"tier", "temperature", "grade"}), "tier"),
    ("type", frozenset({"type", "kind", "category", "class"}), "type"),
    (
        "owner",
        frozenset({"owner", "salesperson", "assigned", "rep", "executive", "agent", "user"}),
        "salesperson",
    ),
    ("product", frozenset({"product", "item", "service"}), "product"),
    ("segment", frozenset({"segment", "sector", "industry"}), "segment"),
    ("location", frozenset({"city", "region", "zone", "country", "territory"}), "location"),
)
MAX_BREAKDOWNS: Final = 6
# A lead's own worth - not a company's turnover or a product's price.
VALUE_TOKENS: Final = frozenset(
    {"value", "amount", "revenue", "worth", "budget", "deal", "potential"}
)
SCORE_TOKENS: Final = frozenset({"score", "probability", "likelihood", "rating", "confidence"})
FOLLOW_UP_TOKENS: Final = frozenset(
    {"follow", "followup", "due", "next", "reminder", "callback", "deadline"}
)
CONVERTED: Final = frozenset(
    {"won", "converted", "closed won", "success", "successful", "customer", "signed", "ordered"}
)
HIGH_PRIORITY: Final = frozenset({"high", "hot", "urgent", "critical", "p1", "very high", "top"})
CHART_GROUPS: Final = ("status", "source", "priority", "tier", "type", "segment", "location")


@dataclass(frozen=True, slots=True)
class AnalysisRequest:
    kind: str  # "lead_analysis"
    subject: str  # entity name ("Leads")
    label: str  # "Lead Analysis"
    noun: str  # "lead"
    plural: str  # "leads"


@dataclass
class SubResult:
    """One governed sub-query: plan → resolved → compiled → validated SQL → rows."""

    label: str
    plan: QueryPlan
    resolved: ResolvedPlan
    compiled: CompiledQuery
    sql: str
    result: QueryResult
    filter_applied: bool


SubRunner = Callable[[QueryPlan, str], Awaitable[SubResult]]


@dataclass
class _Breakdown:
    key: str
    label: str
    dim: Dimension
    quality: DimensionQuality
    categories: list[tuple[str, int]]  # meaningful values, largest first
    query: str
    sub: SubResult


@dataclass
class AnalysisOutcome:
    report: AnalysisOut
    main: SubResult  # the total query
    chart_source: SubResult | None
    summary: str
    highlights: list[str] = field(default_factory=list)


# ----------------------------------------------------------------- detection
def detect_analysis(
    question: str, bundle: SemanticBundle, usable: frozenset[str]
) -> AnalysisRequest | None:
    """ "lead analysis for X", "analyze leads", "overview of campaigns" → the subject."""
    for pattern in (_AFTER, _BEFORE):
        for match in pattern.finditer(question):
            words = match.group("subject").lower().split()
            if words and words[-1] in _LINKING:
                continue
            for n in (3, 2, 1):  # the longest trailing phrase that names an entity
                if len(words) < n:
                    continue
                entity = _entity_for(" ".join(words[-n:]), bundle, usable)
                if entity is not None:
                    noun = singular_noun(entity.entity)
                    return AnalysisRequest(
                        kind=f"{noun.replace(' ', '_')}_analysis",
                        subject=entity.entity,
                        label=f"{noun.title()} Analysis",
                        noun=noun,
                        plural=plural_noun(entity.entity),
                    )
    return None


def _entity_for(phrase: str, bundle: SemanticBundle, usable: frozenset[str]) -> Entity | None:
    wanted = _singular_phrase(phrase)
    for name in sorted(usable):
        entity = bundle.entities[name]
        names = [plural_noun(entity.entity), *entity.synonyms]
        if any(_singular_phrase(n) == wanted for n in names):
            return entity
    return None


def _singular_phrase(text: str) -> str:
    return " ".join(singular(w) for w in tokenize(text))


# ------------------------------------------------------------------ analysis
class BusinessAnalyzer:
    def __init__(
        self,
        request: AnalysisRequest,
        subject: Entity,
        *,
        entity: EntityOut | None,
        entity_filter: QueryFilter | None,
        today: date,
        max_blank_ratio: float,
        run: SubRunner,
        chart_hint: Literal["share", "relationship"] | None = None,
    ) -> None:
        self.request = request
        self.subject = subject
        self.entity = entity
        self.filters = [entity_filter] if entity_filter is not None else []
        self.today = today
        self.max_blank_ratio = max_blank_ratio
        self.run_query = run
        self.chart_hint = chart_hint
        self.metrics: list[MetricOut] = []
        self.unavailable: list[UnavailableMetricOut] = []
        self.insights: list[InsightOut] = []
        self.warnings: list[str] = []
        self.queries: list[AnalysisQueryOut] = []
        self.subs: list[SubResult] = []
        self.dimensions: list[DimensionQuality] = []

    # --------------------------------------------------------------- helpers
    async def _run(self, plan: QueryPlan, label: str) -> SubResult:
        sub = await self.run_query(plan, label)
        self.subs.append(sub)
        self.queries.append(
            AnalysisQueryOut(label=label, sql=sub.sql, row_count=sub.result.row_count)
        )
        return sub

    def _plan(self, **kwargs: Any) -> QueryPlan:
        filters = [*self.filters, *kwargs.pop("filters", [])]
        return QueryPlan(entity=self.subject.entity, filters=filters, **kwargs)

    def _metric(
        self,
        key: str,
        label: str,
        value: float | int | None,
        query: str,
        fmt: str | None = "integer",
        detail: str | None = None,
    ) -> None:
        self.metrics.append(
            MetricOut(
                key=key,
                label=label,
                value=value,
                format=fmt,  # type: ignore[arg-type]
                query=query,
                detail=detail,
            )
        )

    def _missing(self, key: str, label: str, reason: str) -> None:
        self.unavailable.append(UnavailableMetricOut(key=key, label=label, reason=reason))

    def _say(self, text: str, *metrics: str, kind: Literal["fact", "observation"] = "fact") -> None:
        self.insights.append(InsightOut(text=text, metrics=list(metrics), kind=kind))

    @property
    def _plural(self) -> str:
        return self.request.plural

    def _of(self, n: int) -> str:
        return f"{n:,} {self.request.noun if n == 1 else self._plural}"

    # ------------------------------------------------------------------- run
    async def run(self) -> AnalysisOutcome:
        s = self.subject
        count = _count_measure(s)
        if count is None:
            raise ValueError(f"{s.entity} has no count measure")
        total_sub = await self._run(self._plan(measures=[count.name]), "total")
        total = int(_first_number(total_sub.result) or 0)
        self._metric("total", f"Total {self._plural}", total, "total")
        who = self.entity.name if self.entity and self.entity.resolved else None

        if total == 0:
            self._say(
                f"{who} has no {self._plural}." if who else f"There are no {self._plural}.",
                "total",
            )
            return self._finish(total_sub, None, None, ["no records to chart"])

        self._say(
            f"{who} has {self._of(total)}." if who else f"{self._of(total).capitalize()} in total.",
            "total",
        )
        breakdowns = await self._breakdowns(total)
        await self._measures(total)
        trend = await self._time(total)
        await self._follow_ups(total)
        self._conversion(breakdowns, total)
        self._high_priority(breakdowns, total)
        self._observations(breakdowns, total)
        chart, source, rejected = self._choose_chart(breakdowns, trend)
        return self._finish(total_sub, chart, source, rejected)

    # ------------------------------------------------------------ breakdowns
    async def _breakdowns(self, total: int) -> dict[str, _Breakdown]:
        count = _count_measure(self.subject)
        assert count is not None  # noqa: S101 - checked in run()
        found: dict[str, _Breakdown] = {}
        for key, words, label in BREAKDOWNS:
            if len(found) >= MAX_BREAKDOWNS:
                break
            dims = [
                d for d in self.subject.dimensions if not d.sensitive and _tokens(d.name) & words
            ]
            usable = _preferred([d for d in dims if dimension_role(d) in {"category", "name"}], key)
            if not usable:
                if key == "owner":
                    await self._owner_ids(dims, total)
                continue
            dim = usable[0]
            query = f"{self._plural} by {_label(dim)}"
            sub = await self._run(
                self._plan(dimensions=[dim.name], measures=[count.name], limit=60), query
            )
            rows = sub.result.rows
            quality = profile_dimension(
                name=dim.name,
                label=_label(dim).capitalize(),
                role=dimension_role(dim),
                values=[r[0] for r in rows],
                weights=[float(_num(r[1]) or 0) for r in rows],
                max_blank_ratio=self.max_blank_ratio,
            )
            self.dimensions.append(quality)
            categories = sorted(
                ((_text(r[0]), int(_num(r[1]) or 0)) for r in rows if not is_blank(r[0])),
                key=lambda c: -c[1],
            )
            metric_key = f"{self._plural}_by_{key}".replace(" ", "_")
            if not quality.usable:
                reason = (
                    f"{_label(dim).capitalize()} is not recorded for any of the {self._of(total)}."
                    if quality.distinct_values == 0
                    else f"{_label(dim).capitalize()} is {quality.reason}."
                )
                self._missing(metric_key, f"{self._plural.capitalize()} by {label}", reason)
                self.warnings.append(reason)
                continue
            self._metric(
                metric_key,
                f"{self._plural.capitalize()} by {_label(dim)}",
                len(categories),
                query,
                fmt="integer",
                detail=", ".join(f"{name} {n:,}" for name, n in categories[:8]),
            )
            if quality.missing:
                self.warnings.append(
                    f"{_label(dim).capitalize()} is missing for {quality.missing:,} of the "
                    f"{self._of(total)}."
                )
            found[key] = _Breakdown(key, label, dim, quality, categories, metric_key, sub)
            self._describe_breakdown(found[key], total)
        return found

    async def _owner_ids(self, dims: Sequence[Dimension], total: int) -> None:
        """Owners stored only as ids: say whether anyone is assigned at all."""
        key = f"{self._plural}_by_salesperson".replace(" ", "_")
        label = f"{self._plural.capitalize()} by salesperson"
        ids = [d for d in dims if dimension_role(d) == "identifier"]
        if not ids:
            self._missing(key, label, f"The {self._plural} data has no salesperson field.")
            return
        count = _count_measure(self.subject)
        assert count is not None  # noqa: S101
        dim = ids[0]
        sub = await self._run(
            self._plan(
                measures=[count.name],
                filters=[QueryFilter(field=dim.name, op=FilterOp.IS_NOT_NULL)],
            ),
            f"{self._plural} with {_label(dim)}",
        )
        assigned = int(_first_number(sub.result) or 0)
        if assigned == 0:
            reason = (
                f"No {self.request.noun} is assigned to a salesperson ({_label(dim)} is empty)."
            )
            self.warnings.append(reason)
        else:
            reason = (
                f"Salespeople are stored only as ids ({_label(dim)}; {assigned:,} of "
                f"{self._of(total)} assigned) - add a name field to the semantic layer."
            )
        self._missing(key, label, reason)

    def _describe_breakdown(self, b: _Breakdown, total: int) -> None:
        cats = b.categories
        if not cats:
            return
        top, n = cats[0]
        share = round(n / total * 100) if total else 0
        if b.key == "status":
            if len(cats) == 1:
                text = f"All {self._of(n)} with a {b.label} are “{top}”."
            else:
                rest = ", ".join(f"{name} ({k:,})" for name, k in cats[1:4])
                text = f"{share}% of {self._plural} are “{top}” ({n:,} of {total:,}); then {rest}."
            self._say(text, b.query)
        elif b.key == "source":
            text = f"The most common source is “{top}” ({self._of(n)})"
            if len(cats) > 1:
                text += f", out of {len(cats)} sources"
            self._say(text + ".", b.query)
        elif b.key in {"priority", "tier"}:
            return  # stated as high-priority records instead
        else:
            listed = ", ".join(f"{name} {k:,}" for name, k in cats[:5])
            self._say(f"By {b.label}: {listed}.", b.query)

    # --------------------------------------------------------------- measures
    async def _measures(self, total: int) -> None:
        s = self.subject
        values = [
            m
            for m in s.measures
            if _tokens(m.name) & VALUE_TOKENS
            and m.aggregation in {Aggregation.SUM, Aggregation.AVG}
        ]
        scores = [
            m
            for m in s.measures
            if _tokens(m.name) & SCORE_TOKENS and m.aggregation is Aggregation.AVG
        ]
        noun = self.request.noun
        if not values:
            reason = f"The {self._plural} data has no value or amount field."
            for key, label in (
                ("total_value", f"Total {noun} value"),
                ("average_value", f"Average {noun} value"),
                ("high_value", f"High-value {self._plural}"),
            ):
                self._missing(key, label, reason)
        wanted = [*values, *scores]
        if not wanted:
            return
        sub = await self._run(
            self._plan(measures=[m.name for m in wanted]), f"{self._plural} measures"
        )
        row = sub.result.rows[0] if sub.result.rows else []
        for i, m in enumerate(wanted):
            value = _num(row[i]) if i < len(row) else None
            fmt = m.format.value
            if m in values:
                if m.aggregation is Aggregation.SUM:
                    self._metric("total_value", f"Total {noun} value", value, sub.label, fmt)
                    if value is not None and total:
                        self._metric(
                            "average_value",
                            f"Average {noun} value",
                            round(value / total, 2),
                            sub.label,
                            fmt,
                            detail="total value divided by the number of records",
                        )
                        self._say(
                            f"Total {noun} value is {value:,.2f}; {value / total:,.2f} per {noun}.",
                            "total_value",
                            "average_value",
                        )
                else:
                    self._metric("average_value", f"Average {noun} value", value, sub.label, fmt)
                if not any(u.key == "high_value" for u in self.unavailable):
                    self._missing(
                        "high_value",
                        f"High-value {self._plural}",
                        "No high-value threshold is defined in the semantic layer.",
                    )
            elif value is not None:
                key = f"average_{m.name}"
                self._metric(
                    key, f"Average {_label_text(m.name)}", round(value, 2), sub.label, "decimal"
                )
                self._say(
                    f"The average {_label_text(m.name)} is {value:,.2f} ({self._plural} without "
                    f"a {_label_text(m.name)} are not counted).",
                    key,
                )

    # ------------------------------------------------------------------- time
    async def _time(self, total: int) -> list[tuple[str, int]]:
        field_ = _created_field(self.subject)
        if field_ is None:
            return []
        count = _count_measure(self.subject)
        assert count is not None  # noqa: S101
        recent = await self._run(
            self._plan(
                measures=[count.name],
                date_range=DateRange(field=field_.name, relative=RelativePeriod.LAST_30_DAYS),
            ),
            f"{self._plural} created in the last 30 days",
        )
        n = int(_first_number(recent.result) or 0)
        self._metric(
            "recent", f"{self._plural.capitalize()} created in the last 30 days", n, recent.label
        )
        self._say(f"{self._of(n).capitalize()} created in the last 30 days.", "recent")

        trend = await self._run(
            self._plan(
                dimensions=[field_.name],
                measures=[count.name],
                time_grain=TimeGrain.MONTH,
                limit=36,
            ),
            f"{self._plural} by month",
        )
        months = [
            (_text(r[0]), int(_num(r[1]) or 0)) for r in trend.result.rows if not is_blank(r[0])
        ]
        if months:
            self._metric(
                "by_month",
                f"{self._plural.capitalize()} by month",
                len(months),
                trend.label,
                detail=", ".join(f"{_month(m)} {k:,}" for m, k in months[-6:]),
            )
            if len(months) > 1:
                peak = max(k for _, k in months)
                busiest = [_month(m) for m, k in months if k == peak]
                if len(busiest) == len(months):
                    tail = f"each month had {self._of(peak)}"
                elif len(busiest) == 1:
                    tail = f"the busiest month was {busiest[0]} ({self._of(peak)})"
                else:
                    listed = ", ".join(busiest[:-1]) + f" and {busiest[-1]}"
                    tail = f"the busiest months were {listed} ({self._of(peak)} each)"
                self._say(
                    f"{self._plural.capitalize()} were created between {_month(months[0][0])} "
                    f"and {_month(months[-1][0])}; {tail}.",
                    "by_month",
                )
        return months

    async def _follow_ups(self, total: int) -> None:
        fields = [t for t in self.subject.time_fields if _tokens(t.name) & FOLLOW_UP_TOKENS]
        due_label = "Follow-ups due in the next 7 days"
        overdue_label = "Overdue follow-ups"
        if not fields:
            reason = f"The {self._plural} data has no follow-up date field."
            self._missing("follow_ups_due", due_label, reason)
            self._missing("follow_ups_overdue", overdue_label, reason)
            return
        f = fields[0]
        count = _count_measure(self.subject)
        assert count is not None  # noqa: S101
        covered_sub = await self._run(
            self._plan(
                measures=[count.name],
                filters=[QueryFilter(field=f.name, op=FilterOp.IS_NOT_NULL)],
            ),
            f"{self._plural} with a {_label_text(f.name)}",
        )
        covered = int(_first_number(covered_sub.result) or 0)
        if covered == 0:
            reason = f"{_label_text(f.name).capitalize()} is empty for all {self._of(total)}."
            self._missing("follow_ups_due", due_label, reason)
            self._missing("follow_ups_overdue", overdue_label, reason)
            self.warnings.append(reason)
            return
        week = self.today + timedelta(days=7)
        due = await self._run(
            self._plan(
                measures=[count.name],
                filters=[
                    QueryFilter(field=f.name, op=FilterOp.GTE, value=self.today),
                    QueryFilter(field=f.name, op=FilterOp.LTE, value=week),
                ],
            ),
            due_label.lower(),
        )
        overdue = await self._run(
            self._plan(
                measures=[count.name],
                filters=[QueryFilter(field=f.name, op=FilterOp.LT, value=self.today)],
            ),
            overdue_label.lower(),
        )
        n_due, n_over = int(_first_number(due.result) or 0), int(_first_number(overdue.result) or 0)
        self._metric("follow_ups_due", due_label, n_due, due.label)
        self._metric("follow_ups_overdue", overdue_label, n_over, overdue.label)
        self._say(
            f"{n_due:,} follow-up(s) due in the next 7 days; {n_over:,} overdue "
            f"({covered:,} of {self._of(total)} have a {_label_text(f.name)}).",
            "follow_ups_due",
            "follow_ups_overdue",
        )

    # ------------------------------------------------------- derived metrics
    def _conversion(self, breakdowns: dict[str, _Breakdown], total: int) -> None:
        label = "Conversion rate"
        status = breakdowns.get("status")
        if status is None:
            self._missing(
                "conversion_rate", label, f"The {self._plural} data has no status or stage field."
            )
            return
        won = [(name, n) for name, n in status.categories if _is_converted(name)]
        if not won:
            values = ", ".join(name for name, _ in status.categories[:6])
            self._missing(
                "conversion_rate",
                label,
                f"No won/converted {status.label} exists ({_label(status.dim)} values: {values}).",
            )
            return
        converted = sum(n for _, n in won)
        rate = round(converted / total * 100, 1) if total else 0.0
        self._metric(
            "conversion_rate",
            label,
            rate,
            status.query,
            "percent",
            detail=f"{converted:,} of {total:,} ({', '.join(n for n, _ in won)})",
        )
        self._say(f"Conversion rate: {rate}% ({converted:,} of {total:,}).", "conversion_rate")

    def _high_priority(self, breakdowns: dict[str, _Breakdown], total: int) -> None:
        label = f"High-priority {self._plural}"
        for key in ("priority", "tier"):
            b = breakdowns.get(key)
            if b is None:
                continue
            high = [(name, n) for name, n in b.categories if name.lower() in HIGH_PRIORITY]
            if not high:
                continue
            n = sum(k for _, k in high)
            values = ", ".join(f"“{name}”" for name, _ in high)
            self._metric("high_priority", label, n, b.query, detail=f"{_label(b.dim)} {values}")
            self._say(
                f"{n:,} of {self._of(total)} are high priority ({_label(b.dim)} {values}).",
                "high_priority",
            )
            return
        self._missing(
            "high_priority",
            label,
            f"No priority or tier with a high/hot value is recorded for these {self._plural}.",
        )

    def _observations(self, breakdowns: dict[str, _Breakdown], total: int) -> None:
        rate = next((m for m in self.metrics if m.key == "conversion_rate"), None)
        status = breakdowns.get("status")
        if rate is not None:
            self._say(
                f"Key observation: {rate.value}% of {self._plural} have converted.",
                "conversion_rate",
                kind="observation",
            )
        elif status is not None and status.categories:
            top, n = status.categories[0]
            share = round(n / total * 100) if total else 0
            if share >= 60:
                self._say(
                    f"Key observation: most {self._plural} ({share}%) are “{top}”.",
                    status.query,
                    kind="observation",
                )
        empty = [
            u
            for u in self.unavailable
            if "is empty" in u.reason or "not recorded" in u.reason or "No " in u.reason
        ]
        if len(empty) >= 2:
            listed = ", ".join(dict.fromkeys(u.label.lower() for u in empty))
            self._say(  # refers to the unavailable metrics it summarises
                f"Key observation: {listed} cannot be tracked from the recorded data.",
                *dict.fromkeys(u.key for u in empty),
                kind="observation",
            )

    # ------------------------------------------------------------------ chart
    def _choose_chart(
        self, breakdowns: dict[str, _Breakdown], months: list[tuple[str, int]]
    ) -> tuple[AnalysisChartOut | None, SubResult | None, list[str]]:
        rejected: list[str] = []
        for key in CHART_GROUPS:
            b = breakdowns.get(key)
            if b is None:
                continue
            if len(b.categories) < 2:
                rejected.append(f"{_label(b.dim).capitalize()} has a single value")
                continue
            if len(b.categories) > 50:
                rejected.append(f"{_label(b.dim).capitalize()} has too many values to chart")
                continue
            pie = self.chart_hint == "share" and len(b.categories) <= 6
            chart = AnalysisChartOut(
                enabled=True,
                type="pie" if pie else "bar",
                title=f"{self._plural.capitalize()} by {_label(b.dim)}",
                dimension=_label(b.dim),
                metric=f"Number of {self._plural}",
                data=[ChartPointOut(label=name, value=n) for name, n in b.categories],
                validation=ChartValidation(valid=True),
            )
            return chart, b.sub, rejected
        if len(months) >= 2:
            sub = next(s for s in self.subs if s.label == f"{self._plural} by month")
            chart = AnalysisChartOut(
                enabled=True,
                type="line",
                title=f"{self._plural.capitalize()} created by month",
                dimension="Month",
                metric=f"Number of {self._plural}",
                data=[ChartPointOut(label=_month(m), value=n) for m, n in months],
                validation=ChartValidation(valid=True),
            )
            return chart, sub, rejected
        rejected.append("no breakdown has at least two meaningful values")
        return None, None, rejected

    # ----------------------------------------------------------------- finish
    def _finish(
        self,
        main: SubResult,
        chart: AnalysisChartOut | None,
        source: SubResult | None,
        rejected: list[str],
    ) -> AnalysisOutcome:
        if chart is None:
            chart = AnalysisChartOut(
                enabled=False, validation=ChartValidation(valid=False, reasons=rejected)
            )
        title = self.request.label
        if self.entity is not None and self.entity.resolved and self.entity.name:
            title = f"{title} — {self.entity.name}"
        filter_applied: bool | None = None
        if self.filters:
            filter_applied = all(s.filter_applied for s in self.subs)
        report = AnalysisOut(
            intent=self.request.kind,
            title=title,
            entity=self.entity,
            metrics=self.metrics,
            unavailable=self.unavailable,
            dimensions=self.dimensions,
            insights=self.insights,
            warnings=list(dict.fromkeys(self.warnings)),
            chart=chart,
            validation=AnalysisValidationOut(
                data_validated=True,
                entity_resolved=self.entity.resolved if self.entity else None,
                entity_filter_applied=filter_applied,
                chart_validated=chart.enabled and chart.validation.valid,
                warnings=list(dict.fromkeys(self.warnings)),
            ),
            queries=self.queries,
        )
        facts = [i.text for i in self.insights]
        return AnalysisOutcome(
            report=report,
            main=main,
            chart_source=source,
            summary=f"{title}: {facts[0]}" if facts else title,
            highlights=facts[1:6],
        )


# ------------------------------------------------------------------ helpers
def _tokens(name: str) -> set[str]:
    return {singular(t) for t in tokenize(name)} | set(tokenize(name))


def _preferred(dims: list[Dimension], key: str) -> list[Dimension]:
    """For "status": a field called status (or with that synonym) before "verdict"."""

    def rank(dim: Dimension) -> tuple[int, int]:
        named = key in _tokens(dim.name) or any(key in _tokens(s) for s in dim.synonyms)
        return (0 if named else 1, 0 if dimension_role(dim) == "category" else 1)

    return sorted(dims, key=rank)  # stable: the semantic layer's order otherwise


def _label(dim: Dimension) -> str:
    return dim.name.replace("_", " ")


def _label_text(name: str) -> str:
    return name.replace("_", " ")


def _count_measure(entity: Entity) -> Measure | None:
    counts = [
        m
        for m in entity.measures
        if m.aggregation in {Aggregation.COUNT, Aggregation.COUNT_DISTINCT}
    ]
    keyed = [m for m in counts if m.field.lower() == "id" or m.name == "record_count"]
    if keyed:
        return keyed[0]
    return counts[0] if counts else None


def _created_field(entity: Entity) -> TimeField | None:
    fields = entity.time_fields
    named = next((t for t in fields if {"created", "creation"} & _tokens(t.name)), None)
    default = next((t for t in fields if t.name == entity.default_time_field), None)
    candidate = named or default
    if candidate is not None and _tokens(candidate.name) & FOLLOW_UP_TOKENS:
        return None
    return candidate


def _is_converted(value: str) -> bool:
    text = value.strip().lower()
    return text in CONVERTED or any(" " in c and c in text for c in CONVERTED)


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first_number(result: QueryResult) -> float | None:
    return _num(result.rows[0][0]) if result.rows and result.rows[0] else None


def _text(value: Any) -> str:
    return value.isoformat()[:10] if isinstance(value, date) else str(value).strip()


def _month(iso: str) -> str:
    try:
        d = date.fromisoformat(iso[:10])
    except ValueError:
        return iso
    return d.strftime("%b %Y")
