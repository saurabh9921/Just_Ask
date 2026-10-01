"""Answer feedback models (``/api/v1/feedback``)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints, field_validator

from app.schemas.common import StrictModel

FeedbackCategory = Literal[
    "accurate",
    "helpful",
    "fast",
    "wrong_numbers",
    "wrong_data_source",
    "wrong_period",
    "wrong_filter",
    "missing_data",
    "confusing",
    "slow",
    "other",
]
Comment = Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)]


class FeedbackCreate(StrictModel):
    query_id: uuid.UUID
    rating: Literal[-1, 1]
    categories: list[FeedbackCategory] = Field(default_factory=list, max_length=5)
    comment: Comment | None = None

    @field_validator("categories")
    @classmethod
    def _unique(cls, value: list[FeedbackCategory]) -> list[FeedbackCategory]:
        return list(dict.fromkeys(value))

    @field_validator("comment")
    @classmethod
    def _blank_is_none(cls, value: str | None) -> str | None:
        return value or None


class FeedbackOut(BaseModel):
    id: uuid.UUID
    query_id: uuid.UUID
    user_id: uuid.UUID
    question: str | None = None
    rating: int
    categories: list[str]
    comment: str | None
    created_at: datetime
    updated_at: datetime | None
    remembered: bool = False


class FeedbackPage(BaseModel):
    items: list[FeedbackOut]
    total: int
    limit: int
    offset: int
