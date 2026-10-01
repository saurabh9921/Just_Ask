"""Shared schema building blocks."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, StringConstraints


def _normalise(value: object) -> object:
    """Trim and lower-case *before* pattern validation (constraints run on raw input)."""
    return value.strip().lower() if isinstance(value, str) else value


TenantSlug = Annotated[
    str,
    BeforeValidator(_normalise),
    StringConstraints(
        strip_whitespace=True,
        to_lower=True,
        min_length=2,
        max_length=63,
        pattern=r"^[a-z0-9][a-z0-9-]*$",
    ),
]
Email = Annotated[
    str,
    BeforeValidator(_normalise),
    StringConstraints(
        strip_whitespace=True,
        to_lower=True,
        min_length=3,
        max_length=320,
        pattern=r"^[^@\s]+@[^@\s]+$",
    ),
]
DisplayName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class StrictModel(BaseModel):
    """Request models: unknown fields are rejected (e.g. ``read_only: false``)."""

    model_config = ConfigDict(extra="forbid", str_max_length=10_000)


class ResponseModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)
