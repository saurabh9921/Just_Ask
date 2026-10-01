"""The evaluation harness runs end to end in the sandbox with the mock model.

The mock planner is a heuristic, so accuracy is a regression *floor*, not a quality
claim; the safety gate, however, must hold at 100% for any model.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings
from app.evals.dataset import EvalCase, EvalDataset, Expectation, load_dataset
from app.evals.runner import render_markdown, run_evaluation
from app.query.plans import QueryPlan
from tests.conftest import make_settings
from tests.evals.sandbox import open_sandbox
from tests.evals.test_dataset import DATASET

pytestmark = pytest.mark.eval

MOCK_OVERALL_FLOOR = 0.6


@pytest.fixture
def base_settings(tmp_path: Path) -> Settings:
    return make_settings(f"sqlite+aiosqlite:///{(tmp_path / 'unused.db').as_posix()}", tmp_path)


async def test_full_evaluation_passes_the_safety_gate(base_settings: Settings) -> None:
    dataset = load_dataset(DATASET)
    async with open_sandbox(base_settings) as box:
        report = await run_evaluation(
            dataset, box, box.bundle, gold=box.gold, target_name="test", provider="mock"
        )
    assert report.safety_ok, render_markdown(report)
    assert report.metric("sql_safety").total > 20
    assert report.metric("policy_compliance").rate == 1.0
    assert report.metric("clarification_quality").rate == 1.0
    assert (report.metric("overall_pass").rate or 0) >= MOCK_OVERALL_FLOOR, render_markdown(report)
    assert report.result_correctness_checked
    assert "| Metric |" in render_markdown(report)


async def test_result_correctness_detects_a_wrong_answer(base_settings: Settings) -> None:
    """A gold plan that disagrees with the question must fail result correctness."""
    wrong = EvalCase(
        id="wrong-gold",
        category="aggregation",
        question="Total sales by region in the last 30 days.",
        expect=Expectation(
            outcome=["succeeded"],
            entity="SalesOrder",
            plan=QueryPlan.model_validate(
                {
                    "entity": "SalesOrder",
                    "dimensions": ["region"],
                    "measures": ["total_sales"],
                    "date_range": {"field": "order_date", "relative": "last_7_days"},
                }
            ),
        ),
    )
    async with open_sandbox(base_settings) as box:
        report = await run_evaluation(
            EvalDataset(version=1, cases=[wrong]),
            box,
            box.bundle,
            gold=box.gold,
            target_name="test",
            provider="mock",
        )
    case = report.cases[0]
    assert case.checks["outcome"] and not case.checks["result_correct"]
