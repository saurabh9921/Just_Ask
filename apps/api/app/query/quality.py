"""Data-quality profile of result fields: are the values meaningful business categories?

Run on every result before an answer is built. A field whose records are NULL, empty,
whitespace or a placeholder ("N/A", "NULL", "-") is not a business category: it is
never a chart axis or the subject of a summary, and its missing values are reported
separately ("28 lead candidates have no analysis note").

For grouped results the profile is weighted by the record count of each group, so it
describes records, not result rows.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Final

from pydantic import BaseModel

from app.semantic.roles import ResultRole

# Compared after trimming and lower-casing. Deliberately short: "unknown" or "other"
# can be real business categories.
BLANK_MARKERS: Final = frozenset({"", "null", "n/a", "na", "none", "nil", "-", "--", "undefined"})


def is_blank(value: Any) -> bool:
    """NULL, empty/whitespace text or a missing-value placeholder."""
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() in BLANK_MARKERS
    return False


class DimensionQuality(BaseModel):
    name: str
    label: str
    role: ResultRole
    total: int  # records represented (sum of group counts, or rows)
    null_count: int
    blank_count: int  # empty, whitespace or placeholder text
    distinct_values: int  # distinct meaningful values
    meaningful_ratio: float
    weighted: bool  # totals come from a count measure (grouped result)
    usable: bool  # meaningful enough to describe or chart
    reason: str | None = None

    @property
    def missing(self) -> int:
        return self.null_count + self.blank_count


def profile_dimension(
    *,
    name: str,
    label: str,
    role: ResultRole,
    values: Sequence[Any],
    weights: Sequence[float] | None,
    max_blank_ratio: float,
) -> DimensionQuality:
    """Profile one result column. ``weights`` are per-row record counts, if known."""
    weighted = weights is not None
    w = list(weights) if weights is not None else [1.0] * len(values)
    total = sum(max(x, 0.0) for x in w)
    nulls = sum(max(x, 0.0) for v, x in zip(values, w, strict=True) if v is None)
    blanks = sum(
        max(x, 0.0) for v, x in zip(values, w, strict=True) if v is not None and is_blank(v)
    )
    distinct = len({_key(v) for v in values if not is_blank(v)})
    meaningful = total - nulls - blanks
    ratio = round(meaningful / total, 4) if total > 0 else 0.0
    reason: str | None = None
    if role == "free_text":
        reason = "free text, not a business category"
    elif role == "identifier":
        reason = "an identifier, not a business category"
    elif total <= 0:
        reason = "no records"
    elif distinct == 0:
        reason = "only blank values"
    elif 1 - ratio > max_blank_ratio:
        reason = f"mostly blank ({round((1 - ratio) * 100)}% missing)"
    return DimensionQuality(
        name=name,
        label=label,
        role=role,
        total=round(total),
        null_count=round(nulls),
        blank_count=round(blanks),
        distinct_values=distinct,
        meaningful_ratio=ratio,
        weighted=weighted,
        usable=reason is None,
        reason=reason,
    )


def _key(value: Any) -> str:
    return value.strip().lower() if isinstance(value, str) else repr(value)
