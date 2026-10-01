"""Labelled evaluation questions (``evals/questions.yaml``)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from app.agents.schemas import Intent
from app.query.plans import QueryPlan, RefusalReason

Outcome = Literal[
    "succeeded", "clarification", "refused", "blocked", "invalid", "not_found", "error"
]
Category = Literal[
    "aggregation",
    "grouping",
    "date_filter",
    "comparison",
    "ranking",
    "inventory",
    "ambiguous",
    "synonym",
    "abbreviation",
    "unauthorized",
    "sensitive",
    "prompt_injection",
]
# Failing any case in these categories is a safety failure, not an accuracy miss.
SAFETY_CATEGORIES: frozenset[str] = frozenset({"unauthorized", "sensitive", "prompt_injection"})
REQUIRED_CATEGORIES: frozenset[str] = frozenset(Category.__args__)  # type: ignore[attr-defined]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Expectation(_Strict):
    outcome: list[Outcome] = Field(min_length=1)
    intent: list[Intent] = Field(default_factory=list)
    entity: str | None = None
    plan: QueryPlan | None = None
    refusal: list[RefusalReason] = Field(default_factory=list)
    clarify_terms: list[str] = Field(default_factory=list)


class EvalCase(_Strict):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,79}$")
    category: Category
    question: str = Field(min_length=3, max_length=2000)
    as_user: Literal["analyst", "ungranted"] = "analyst"
    tenant_isolation: bool = False
    expect: Expectation

    @property
    def safety_critical(self) -> bool:
        return self.category in SAFETY_CATEGORIES


class EvalDataset(_Strict):
    version: int
    cases: list[EvalCase] = Field(min_length=1)


def load_dataset(path: Path) -> EvalDataset:
    raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    dataset = EvalDataset.model_validate(raw)
    ids = [c.id for c in dataset.cases]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise ValueError(f"duplicate case ids: {duplicates}")
    return dataset
