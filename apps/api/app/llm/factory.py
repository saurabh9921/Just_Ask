"""Build the configured LLM provider. The model name is always configuration."""

from __future__ import annotations

import httpx

from app.config import LLMProviderName, Settings
from app.llm.base import LLMProvider
from app.llm.external_client import ExternalProvider
from app.llm.mock_client import MockLLMProvider
from app.llm.ollama_client import OllamaProvider
from app.llm.vllm_client import VLLMProvider


def build_llm_provider(settings: Settings, http: httpx.AsyncClient) -> LLMProvider:
    match settings.llm_provider:
        case LLMProviderName.MOCK:
            return MockLLMProvider()
        case LLMProviderName.OLLAMA:
            return OllamaProvider(settings, http)
        case LLMProviderName.EXTERNAL:
            return ExternalProvider(settings, http)
        case LLMProviderName.VLLM:
            return VLLMProvider(
                settings,
                http,
                base_url=settings.llm_base_url,
                model=settings.llm_model,
                api_key=settings.llm_api_key.get_secret_value() if settings.llm_api_key else None,
            )
