"""Typed outputs the model may produce in Phase 3.

``QueryPlan`` (the model's main output) arrives in Phase 4. Every model here
forbids extra keys and bounds every string, so a compromised or confused model
cannot smuggle free-form instructions or SQL through a structured field.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Name = Annotated[str, StringConstraints(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,62}$")]
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, max_length=300)]

Intent = Literal[
    "aggregation",
    "ranking",
    "comparison",
    "trend",
    "detail",
    "explanation",
    "clarification",
    "unsupported",
]


class IntentClassification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: Intent
    confidence: float = Field(ge=0, le=1)
    needs_clarification: bool = False
    reason: ShortText = ""


class EntityResolution(BaseModel):
    """Which governed entity/fields a question refers to. Validated against the
    semantic layer after generation; unknown names are dropped, never trusted."""

    model_config = ConfigDict(extra="forbid")

    entity: Name | None
    measures: list[Name] = Field(default_factory=list, max_length=10)
    dimensions: list[Name] = Field(default_factory=list, max_length=10)
    time_field: Name | None = None
    confidence: float = Field(ge=0, le=1)
    unresolved_terms: list[ShortText] = Field(default_factory=list, max_length=10)


class ResultExplanation(BaseModel):
    """Narrative over an *aggregated* result. Numbers must come from the DATA block."""

    model_config = ConfigDict(extra="forbid")

    summary: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=600)]
    highlights: list[ShortText] = Field(default_factory=list, max_length=5)
