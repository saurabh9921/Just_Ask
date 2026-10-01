"""Performance budgets (docs/performance.md).

Budgets are deliberately generous so they hold on shared CI runners; they exist to catch
order-of-magnitude regressions (an accidental N+1, a cache that stops hitting, a writer
that buffers the whole export), not to benchmark.
"""

from __future__ import annotations

import time
import tracemalloc
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from app.query.compiler import compile_plan
from app.query.sql_validator import validate_sql
from app.services.export_writer import ExportWriter
from tests.unit.test_analytics_api import REGION_Q, ask, demo_db, fixed_today
from tests.unit.test_query_engine import ALL_DIALECTS, Engine, data_examples, engine, sql_policy
from tests.unit.test_semantic_api import setup_indexed

pytestmark = pytest.mark.performance
__all__ = ["demo_db", "engine", "fixed_today"]

PLAN_PIPELINE_BUDGET_MS = 50.0  # resolve + compile + validate, per plan and dialect (p95)
CACHED_QUESTION_BUDGET_MS = 500.0  # end to end through the API with the mock LLM
EXPORT_ROWS = 200_000
EXPORT_PEAK_BUDGET_BYTES = 4 * 1024 * 1024  # independent of the row count


def p95(samples: list[float]) -> float:
    ordered = sorted(samples)
    return ordered[int(len(ordered) * 0.95) - 1]


async def test_plan_pipeline_latency(engine: Engine) -> None:
    samples: list[float] = []
    for _, plan in data_examples(engine.bundle):
        for dialect_name, dialect in ALL_DIALECTS:
            if dialect_name == "hana":
                continue  # foundation dialect
            start = time.perf_counter()
            resolved = engine.resolve(plan)
            compiled = compile_plan(resolved, dialect_name)
            validate_sql(compiled.render(dialect), sql_policy(engine, resolved, dialect_name))
            samples.append((time.perf_counter() - start) * 1000)
    assert len(samples) >= 100
    assert p95(samples) < PLAN_PIPELINE_BUDGET_MS, f"p95={p95(samples):.1f}ms"


async def test_cached_question_is_fast(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str
) -> None:
    _, headers, cid = await setup_indexed(app, client, demo_db)
    assert (await ask(client, headers, cid, REGION_Q)).json()["cache"]["hit"] is False
    samples: list[float] = []
    for _ in range(5):
        start = time.perf_counter()
        body = (await ask(client, headers, cid, REGION_Q)).json()
        samples.append((time.perf_counter() - start) * 1000)
        assert body["cache"]["hit"] is True
    assert min(samples) < CACHED_QUESTION_BUDGET_MS, f"samples={samples}"


def test_export_writer_memory_is_bounded(tmp_path: Path) -> None:
    columns = [("region", "Region"), ("order_date", "Order date"), ("total", "Total")]
    batch: list[tuple[object, ...]] = [
        (f"Region {i % 7}", "2026-09-15", i * 1.5) for i in range(2_000)
    ]
    tracemalloc.start()
    try:
        with ExportWriter(tmp_path / "big.csv", "csv", columns) as writer:
            for _ in range(EXPORT_ROWS // len(batch)):
                writer.write_batch(batch)
            writer.finish({})
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert writer.rows == EXPORT_ROWS
    assert (tmp_path / "big.csv").stat().st_size > EXPORT_PEAK_BUDGET_BYTES
    assert peak < EXPORT_PEAK_BUDGET_BYTES, f"peak={peak}"
