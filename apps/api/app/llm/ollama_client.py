"""Ollama native chat API (developer mode on CPU).

Uses ``POST /api/chat`` with ``format`` set to the JSON schema, which makes
Ollama constrain the output; the reply is still validated locally.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.config import Settings
from app.llm.base import ChatMessage
from app.llm.structured_output import StructuredOutputError
from app.llm.vllm_client import StructuredChatProvider


class OllamaProvider(StructuredChatProvider):
    name = "ollama"

    def __init__(self, settings: Settings, http: httpx.AsyncClient) -> None:
        super().__init__(settings)
        self._http = http
        self._url = settings.llm_base_url.rstrip("/").removesuffix("/v1") + "/api/chat"
        self._model = settings.llm_model

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
                "stream": False,
                "format": schema,
                "options": {"temperature": temperature, "num_predict": self._max_tokens},
            },
            timeout=self._timeout,
        )
        response.raise_for_status()
        body = response.json()
        message = body.get("message")
        if not isinstance(message, dict) or "content" not in message:
            raise StructuredOutputError("MALFORMED_RESPONSE")
        return (
            str(message["content"] or ""),
            int(body.get("prompt_eval_count", 0)),
            int(body.get("eval_count", 0)),
        )
