"""Per-question LLM usage accounting and the Phase 7 query metrics."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi import FastAPI

from app.agents.prompts.templates import intent_classification_messages
from app.agents.schemas import IntentClassification
from app.config import Settings
from app.core.metrics import CONNECTOR_ERRORS, QUERY_RESULT_ROWS
from app.llm.mock_server import create_mock_llm_app
from app.llm.usage import record_llm_usage, track_llm_usage
from app.llm.vllm_client import VLLMProvider
from tests.unit.test_analytics_api import REGION_Q, ask, demo_db, fixed_today
from tests.unit.test_semantic_api import setup_indexed

__all__ = ["demo_db", "fixed_today"]


def _sample_total(metric: object, suffix: str) -> float:
    return sum(
        s.value
        for m in metric.collect()  # type: ignore[attr-defined]
        for s in m.samples
        if s.name.endswith(suffix)
    )


def test_usage_outside_a_tracked_run_is_ignored() -> None:
    record_llm_usage(10, 5)  # no active tracker: must not raise or leak anywhere
    with track_llm_usage() as usage:
        record_llm_usage(10, 5)
        record_llm_usage(-3, 2)  # nonsense values from a server are clamped
    assert (usage.calls, usage.prompt_tokens, usage.completion_tokens) == (2, 10, 7)


async def test_concurrent_runs_do_not_mix_their_usage() -> None:
    async def run(tokens: int) -> int:
        with track_llm_usage() as usage:
            for _ in range(3):
                await asyncio.sleep(0)
                record_llm_usage(tokens, 0)
        return usage.prompt_tokens

    assert list(await asyncio.gather(run(1), run(100))) == [3, 300]


async def test_openai_compatible_provider_reports_tokens(settings: Settings) -> None:
    server = create_mock_llm_app(dimensions=32)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server)) as http:
        provider = VLLMProvider(settings, http, base_url="http://mock/v1", model="m", api_key=None)
        with track_llm_usage() as usage:
            await provider.generate_structured(
                intent_classification_messages("top products last month"), IntentClassification
            )
    assert usage.calls == 1
    assert (usage.prompt_tokens, usage.completion_tokens) == (42, 17)  # mock server usage


async def test_answers_report_model_calls_and_record_result_rows(
    app: FastAPI, client: httpx.AsyncClient, demo_db: str
) -> None:
    _, viewer, cid = await setup_indexed(app, client, demo_db)
    rows_before = _sample_total(QUERY_RESULT_ROWS, "_count")
    body = (await ask(client, viewer, cid, REGION_Q)).json()
    assert body["status"] == "succeeded"
    assert body["usage"]["llm_calls"] >= 2  # intent + plan (+ explanation)
    assert body["usage"]["prompt_tokens"] == 0  # the mock has no tokenizer
    assert _sample_total(QUERY_RESULT_ROWS, "_count") == rows_before + 1


@pytest.mark.parametrize("code", ["TIMEOUT"])
def test_connector_error_counter_is_labelled_by_code(code: str) -> None:
    before = _sample_total(CONNECTOR_ERRORS, "_total")
    CONNECTOR_ERRORS.labels(connector="sqlite", code=code).inc()
    assert _sample_total(CONNECTOR_ERRORS, "_total") == before + 1
