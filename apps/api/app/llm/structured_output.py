"""Strict parsing of model output into Pydantic models.

Rules:
* The output must be exactly one JSON object (a single surrounding Markdown code
  fence is tolerated because many local models add one; nothing else is).
* Size-capped; ``NaN``/``Infinity`` rejected; duplicate keys rejected.
* Validated against the response model, which must forbid extra keys.
* On failure the caller may make **one** repair attempt that feeds back the
  validation errors (never the user's data) and asks for corrected JSON.
"""

from __future__ import annotations

import json
import re
from typing import Any, Final

from pydantic import BaseModel, ValidationError

from app.llm.base import ChatMessage

MAX_OUTPUT_CHARS: Final = 100_000
_FENCE: Final = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```$", re.DOTALL)


class StructuredOutputError(Exception):
    def __init__(self, reason: str, errors: list[str] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.errors = errors or []


def ensure_strict(model: type[BaseModel]) -> None:
    if model.model_config.get("extra") != "forbid":
        raise TypeError(f"{model.__name__} must set model_config extra='forbid'")


def response_schema(model: type[BaseModel]) -> dict[str, Any]:
    ensure_strict(model)
    return model.model_json_schema()


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise StructuredOutputError("DUPLICATE_KEYS")
    return dict(pairs)


def _reject_constant(value: str) -> Any:
    raise StructuredOutputError("NON_FINITE_NUMBER")


def parse_structured[M: BaseModel](raw: str, model: type[M]) -> M:
    ensure_strict(model)
    text = raw.strip()
    if not text:
        raise StructuredOutputError("EMPTY")
    if len(text) > MAX_OUTPUT_CHARS:
        raise StructuredOutputError("TOO_LONG")
    fenced = _FENCE.match(text)
    if fenced:
        text = fenced.group(1).strip()
    if not text.startswith("{") or not text.endswith("}"):
        raise StructuredOutputError("NOT_A_JSON_OBJECT")
    try:
        data = json.loads(text, object_pairs_hook=_no_duplicates, parse_constant=_reject_constant)
    except json.JSONDecodeError as exc:
        raise StructuredOutputError("INVALID_JSON") from exc
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        errors = [
            f"{'.'.join(str(p) for p in e['loc']) or '<root>'}: {e['msg']}"
            for e in exc.errors()[:20]
        ]
        raise StructuredOutputError("SCHEMA_VALIDATION", errors) from exc


def repair_messages(
    messages: list[ChatMessage], raw: str, error: StructuredOutputError
) -> list[ChatMessage]:
    details = "; ".join(error.errors) if error.errors else error.reason
    return [
        *messages,
        {"role": "assistant", "content": raw[:4000]},
        {
            "role": "user",
            "content": (
                "Your previous reply was rejected: "
                f"{details}. Reply again with ONLY a single JSON object that matches the "
                "required schema exactly. No prose, no code fences, no extra keys."
            ),
        },
    ]
