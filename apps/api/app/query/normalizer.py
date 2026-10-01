"""Deterministic plan normalisation: period resolution and canonical hashing.

Periods resolve to half-open windows ``[start, end)`` of dates relative to
``today`` (the tenant's local date). Definitions (all "to date" periods include
today):

========================  ====================================================
today / yesterday         that single day
last_7/30/90_days         the N days ending today (inclusive)
last_n_days (n)           the n days ending today
this_week / last_week     ISO weeks (Monday start); this_week is week-to-date
this_month / last_month   month-to-date / the previous full calendar month
this_quarter              quarter-to-date
last_quarter              the previous full quarter; quarter_before_last before it
last_6/12_months          rolling: same day N months ago up to today
last_n_months (n)         rolling n months
this_year / year_to_date  1 Jan to today
last_year                 the previous full calendar year
========================  ====================================================

The canonical hash covers the *resolved* plan (concrete dates, sorted filters),
so two differently phrased but identical requests share cache/audit identity.
"""

from __future__ import annotations

import calendar
import hashlib
import json
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from app.query.plans import DateRange, QueryPlan, RelativePeriod


@dataclass(frozen=True, slots=True)
class Window:
    start: date
    end: date  # exclusive

    @property
    def last_day(self) -> date:
        return self.end - timedelta(days=1)

    def label(self) -> str:
        return f"{self.start.isoformat()} to {self.last_day.isoformat()}"


def _add_months(day: date, months: int) -> date:
    index = day.year * 12 + (day.month - 1) + months
    year, month = divmod(index, 12)
    month += 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def _month_start(day: date) -> date:
    return day.replace(day=1)


def _quarter_start(day: date) -> date:
    return date(day.year, 3 * ((day.month - 1) // 3) + 1, 1)


def resolve_period(period: DateRange, today: date) -> Window:
    if period.relative is None:
        assert period.start is not None and period.end is not None  # noqa: S101 - model-validated
        return Window(period.start, period.end + timedelta(days=1))
    tomorrow = today + timedelta(days=1)
    rel = period.relative
    days = {
        RelativePeriod.LAST_7_DAYS: 7,
        RelativePeriod.LAST_30_DAYS: 30,
        RelativePeriod.LAST_90_DAYS: 90,
    }
    if rel in days:
        return Window(tomorrow - timedelta(days=days[rel]), tomorrow)
    if rel is RelativePeriod.LAST_N_DAYS:
        return Window(tomorrow - timedelta(days=period.n or 1), tomorrow)
    if rel is RelativePeriod.TODAY:
        return Window(today, tomorrow)
    if rel is RelativePeriod.YESTERDAY:
        return Window(today - timedelta(days=1), today)
    if rel is RelativePeriod.THIS_WEEK:
        return Window(today - timedelta(days=today.weekday()), tomorrow)
    if rel is RelativePeriod.LAST_WEEK:
        monday = today - timedelta(days=today.weekday())
        return Window(monday - timedelta(days=7), monday)
    if rel is RelativePeriod.THIS_MONTH:
        return Window(_month_start(today), tomorrow)
    if rel is RelativePeriod.LAST_MONTH:
        first = _month_start(today)
        return Window(_add_months(first, -1), first)
    if rel is RelativePeriod.THIS_QUARTER:
        return Window(_quarter_start(today), tomorrow)
    if rel in {RelativePeriod.LAST_QUARTER, RelativePeriod.QUARTER_BEFORE_LAST}:
        back = 1 if rel is RelativePeriod.LAST_QUARTER else 2
        this_q = _quarter_start(today)
        return Window(_add_months(this_q, -3 * back), _add_months(this_q, -3 * (back - 1)))
    months = {RelativePeriod.LAST_6_MONTHS: 6, RelativePeriod.LAST_12_MONTHS: 12}
    if rel in months:
        return Window(_add_months(tomorrow, -months[rel]), tomorrow)
    if rel is RelativePeriod.LAST_N_MONTHS:
        return Window(_add_months(tomorrow, -(period.n or 1)), tomorrow)
    if rel in {RelativePeriod.THIS_YEAR, RelativePeriod.YEAR_TO_DATE}:
        return Window(date(today.year, 1, 1), tomorrow)
    if rel is RelativePeriod.LAST_YEAR:
        return Window(date(today.year - 1, 1, 1), date(today.year, 1, 1))
    raise ValueError(f"unsupported period {rel}")  # pragma: no cover - enum is exhaustive


def canonical_plan(plan: QueryPlan, windows: dict[str, Window]) -> dict[str, Any]:
    """Order-insensitive, date-resolved representation used for hashing."""
    data = plan.model_dump(mode="json", exclude={"confidence"})
    data["filters"] = sorted(data["filters"], key=lambda f: json.dumps(f, sort_keys=True))
    data["windows"] = {
        k: [w.start.isoformat(), w.end.isoformat()] for k, w in sorted(windows.items())
    }
    data.pop("date_range", None)
    data.pop("comparison", None)
    return data


def plan_hash(plan: QueryPlan, windows: dict[str, Window], scope: dict[str, str]) -> str:
    """Hash of the canonical plan *and* its security scope (tenant, connection, version...)."""
    payload = {"plan": canonical_plan(plan, windows), "scope": dict(sorted(scope.items()))}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
