"""The labelled evaluation set is well-formed, held out and covers every category."""

from __future__ import annotations

import re
from pathlib import Path

from app.config import get_settings
from app.evals.dataset import REQUIRED_CATEGORIES, SAFETY_CATEGORIES, load_dataset
from app.semantic.loader import load_semantic_layer
from tests.unit.test_query_engine import Engine, engine

__all__ = ["engine"]

DATASET = Path(__file__).resolve().parents[1] / "fixtures" / "eval-questions.yaml"


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def test_dataset_has_at_least_30_cases_covering_every_category() -> None:
    dataset = load_dataset(DATASET)
    assert len(dataset.cases) >= 30
    assert {c.category for c in dataset.cases} == REQUIRED_CATEGORIES
    assert sum(1 for c in dataset.cases if c.category in SAFETY_CATEGORIES) >= 6


def test_questions_are_held_out_from_the_few_shot_examples() -> None:
    examples = {
        _norm(e.question) for e in load_semantic_layer(get_settings().semantic_layer_path).examples
    }
    leaked = [c.id for c in load_dataset(DATASET).cases if _norm(c.question) in examples]
    assert leaked == []


def test_answerable_cases_carry_a_gold_plan_and_safety_cases_do_not_expect_answers() -> None:
    for case in load_dataset(DATASET).cases:
        if case.expect.outcome == ["succeeded"]:
            assert case.expect.plan is not None and case.expect.entity, case.id
            assert case.expect.plan.entity == case.expect.entity, case.id
        if case.category in {"sensitive", "prompt_injection"}:
            assert "succeeded" not in case.expect.outcome, case.id


async def test_every_gold_plan_resolves_and_compiles(engine: Engine) -> None:
    for case in load_dataset(DATASET).cases:
        if case.expect.plan is None:
            continue
        resolved = engine.resolve(case.expect.plan)
        compiled = engine.adapter.compile_query_plan(resolved)
        result = await engine.adapter.execute_readonly(compiled.statement)
        assert result.columns, case.id
