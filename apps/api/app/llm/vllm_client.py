"""OpenAI-compatible chat providers (vLLM, llama.cpp server, TGI, hosted APIs).

Requests use ``response_format = {"type": "json_schema", ...}`` with ``strict``
so servers that support guided decoding (vLLM does) constrain generation to the
schema. The result is *still* parsed and validated locally - server-side
constraints are a performance aid, not a security control.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
from pydantic import BaseModel

from app.config import Settings
from app.core.logging import get_logger
from app.core.metrics import LLM_REQUEST_DURATION, LLM_TOKENS, LLM_VALIDATION_FAILURES
from app.core.resilience import CircuitBreaker, call_with_resilience, is_transient
from app.llm.base import ChatMessage, LLMOutputError, LLMUnavailableError
from app.llm.structured_output import (
    StructuredOutputError,
    parse_structured,
    repair_messages,
    response_schema,
)
from app.llm.usage import record_llm_usage

logger = get_logger(__name__)

MAX_REPAIR_ATTEMPTS = 1


class StructuredChatProvider:
    """Shared generate → parse → (one) repair loop with resilience and metrics."""

    name = "base"

    def __init__(self, settings: Settings) -> None:
        self._retries = settings.llm_max_retries
        self._max_tokens = settings.llm_max_output_tokens
        self._timeout = settings.llm_timeout_seconds
        self._breaker = CircuitBreaker(
            f"llm:{self.name}",
            failure_threshold=settings.llm_circuit_failure_threshold,
            reset_seconds=settings.llm_circuit_reset_seconds,
        )

    async def _complete(
        self,
        messages: list[ChatMessage],
        schema: dict[str, Any],
        schema_name: str,
        temperature: float,
    ) -> tuple[str, int, int]:
        """Return (raw text, prompt tokens, completion tokens)."""
        raise NotImplementedError

    async def generate_structured[M: BaseModel](
        self,
        messages: list[ChatMessage],
        response_model: type[M],
        temperature: float = 0,
    ) -> M:
        schema = response_schema(response_model)
        schema_name = response_model.__name__
        started = time.perf_counter()
        outcome = "error"
        conversation = list(messages)
        try:
            for attempt in range(MAX_REPAIR_ATTEMPTS + 1):

                async def call(conv: list[ChatMessage] = conversation) -> tuple[str, int, int]:
                    return await self._complete(conv, schema, schema_name, temperature)

                try:
                    raw, prompt_tokens, completion_tokens = await call_with_resilience(
                        call, breaker=self._breaker, max_retries=self._retries
                    )
                except httpx.HTTPError as exc:
                    raise LLMUnavailableError(
                        details={"provider": self.name, "transient": is_transient(exc)}
                    ) from exc
                except StructuredOutputError as exc:  # malformed envelope, not content
                    LLM_VALIDATION_FAILURES.labels(
                        provider=self.name, schema=schema_name, stage="envelope"
                    ).inc()
                    raise LLMOutputError(details={"reason": exc.reason}) from exc
                LLM_TOKENS.labels(provider=self.name, kind="prompt").inc(prompt_tokens)
                LLM_TOKENS.labels(provider=self.name, kind="completion").inc(completion_tokens)
                record_llm_usage(prompt_tokens, completion_tokens)
                try:
                    result = parse_structured(raw, response_model)
                except StructuredOutputError as exc:
                    stage = "initial" if attempt == 0 else "repair"
                    LLM_VALIDATION_FAILURES.labels(
                        provider=self.name, schema=schema_name, stage=stage
                    ).inc()
                    logger.warning(
                        "llm output rejected",
                        extra={
                            "provider": self.name,
                            "schema": schema_name,
                            "reason": exc.reason,
                            "stage": stage,
                        },
                    )
                    if attempt >= MAX_REPAIR_ATTEMPTS:
                        raise LLMOutputError(details={"reason": exc.reason}) from exc
                    conversation = repair_messages(conversation, raw, exc)
                    continue
                outcome = "success"
                return result
            raise LLMOutputError()  # pragma: no cover - loop always returns or raises
        finally:
            LLM_REQUEST_DURATION.labels(provider=self.name, outcome=outcome).observe(
                time.perf_counter() - started
            )


class OpenAICompatibleProvider(StructuredChatProvider):
    name = "openai_compatible"

    def __init__(
        self,
        settings: Settings,
        http: httpx.AsyncClient,
        *,
        base_url: str,
        model: str,
        api_key: str | None,
    ) -> None:
        super().__init__(settings)
        self._http = http
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._model = model
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    async def _complete(
        self,
        messages: list[ChatMessage],
        schema: dict[str, Any],
        schema_name: str,
        temperature: float,
    ) -> tuple[str, int, int]:
        response = await self._http.post(
            self._url,
            json={
                "model": self._model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": self._max_tokens,
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {"name": schema_name, "schema": schema, "strict": True},
                },
            },
            headers=self._headers,
            timeout=self._timeout,
        )
        response.raise_for_status()
        body = response.json()
        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise StructuredOutputError("MALFORMED_RESPONSE") from exc
        usage = body.get("usage") or {}
        return (
            str(content or ""),
            int(usage.get("prompt_tokens", 0)),
            int(usage.get("completion_tokens", 0)),
        )


class VLLMProvider(OpenAICompatibleProvider):
    """vLLM's OpenAI-compatible server (GPU production default)."""

    name = "vllm"
