"""LLM provider contract.

Every provider returns a *validated Pydantic model* - never free text, never SQL.
Response models must be declared with ``extra="forbid"`` so an output with
unexpected keys is rejected rather than silently accepted.
"""

from __future__ import annotations

from typing import Literal, Protocol, TypedDict

from pydantic import BaseModel

from app.core.exceptions import AppError, DependencyUnavailableError


class ChatMessage(TypedDict):
    role: Literal["system", "user", "assistant"]
    content: str


class LLMProvider(Protocol):
    name: str

    async def generate_structured[M: BaseModel](
        self,
        messages: list[ChatMessage],
        response_model: type[M],
        temperature: float = 0,
    ) -> M: ...


class LLMUnavailableError(DependencyUnavailableError):
    code = "LLM_UNAVAILABLE"
    default_message = "The language model service is unavailable."


class LLMOutputError(AppError):
    """The model did not produce valid structured output (after repair)."""

    status_code = 502
    code = "LLM_INVALID_OUTPUT"
    default_message = "The language model returned an invalid response."


class ExternalLLMDisabledError(AppError):
    status_code = 500
    code = "EXTERNAL_LLM_DISABLED"
    default_message = "External language model providers are disabled."
