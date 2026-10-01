"""Optional hosted (third-party) OpenAI-compatible provider - disabled by default.

Sending prompts to a third party is a data-egress decision: it requires
``LLM_PROVIDER=external`` *and* ``EXTERNAL_LLM_ENABLED=true`` (settings refuse the
former without the latter), and the provider re-checks at construction time.
Prompts never contain ERP result rows or sensitive fields regardless of provider.
"""

from __future__ import annotations

import httpx

from app.config import Settings
from app.llm.base import ExternalLLMDisabledError
from app.llm.vllm_client import OpenAICompatibleProvider


class ExternalProvider(OpenAICompatibleProvider):
    name = "external"

    def __init__(self, settings: Settings, http: httpx.AsyncClient) -> None:
        if not settings.external_llm_enabled:
            raise ExternalLLMDisabledError()
        if not settings.llm_base_url.startswith("https://"):
            raise ExternalLLMDisabledError("External LLM endpoints must use HTTPS.")
        super().__init__(
            settings,
            http,
            base_url=settings.llm_base_url,
            model=settings.llm_model,
            api_key=settings.llm_api_key.get_secret_value() if settings.llm_api_key else None,
        )
