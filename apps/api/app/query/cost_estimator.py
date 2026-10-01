"""Pre-execution cost estimation.

Uses the connector's EXPLAIN (never EXPLAIN ANALYZE, which would execute the
query). PostgreSQL returns planner cost and row estimates; engines without cost
information (SQLite) fall back to a heuristic on the metadata row estimate.
Queries over ``QUERY_MAX_COST`` are rejected before touching the data.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.connectors.base import DatabaseAdapter
from app.connectors.common.errors import ConnectorError
from app.core.exceptions import AppError
from app.core.logging import get_logger
from app.query.compiler import CompiledQuery

logger = get_logger(__name__)


class QueryTooExpensiveError(AppError):
    status_code = 422
    code = "QUERY_TOO_EXPENSIVE"
    default_message = (
        "This question would scan too much data. Narrow the period or add filters and try again."
    )


@dataclass(frozen=True, slots=True)
class CostEstimate:
    source: Literal["explain", "metadata", "unknown"]
    estimated_cost: float | None
    estimated_rows: float | None
    within_limits: bool


async def estimate_cost(
    adapter: DatabaseAdapter,
    compiled: CompiledQuery,
    *,
    max_cost: float,
    object_row_estimates: dict[str, int],
    max_scan_rows: int,
) -> CostEstimate:
    try:
        explain = await adapter.explain_query(compiled.statement)
    except (ConnectorError, AppError) as exc:
        logger.info("explain unavailable", extra={"error_type": type(exc).__name__})
        explain = None
    if explain is not None and explain.estimated_cost is not None:
        estimate = CostEstimate(
            "explain",
            explain.estimated_cost,
            explain.estimated_rows,
            explain.estimated_cost <= max_cost,
        )
    else:
        scanned = [object_row_estimates[t] for t in compiled.tables if t in object_row_estimates]
        if scanned:
            rows = float(max(scanned))
            estimate = CostEstimate("metadata", None, rows, rows <= max_scan_rows)
        else:
            estimate = CostEstimate("unknown", None, None, True)
    if not estimate.within_limits:
        raise QueryTooExpensiveError(
            details={
                "estimated_cost": estimate.estimated_cost,
                "estimated_rows": estimate.estimated_rows,
            }
        )
    return estimate
