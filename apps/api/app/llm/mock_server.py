"""Mock OpenAI-/Ollama-compatible model server (development and tests only).

Serves ``/v1/chat/completions``, ``/v1/embeddings``, ``/v1/rerank``,
``/api/chat`` and ``/api/embed`` with deterministic answers, so the real HTTP
providers (``VLLMProvider``, ``OllamaProvider``, embedding/rerank clients) can be
exercised without a GPU:

    uvicorn app.llm.mock_server:app --port 8001
    LLM_PROVIDER=vllm LLM_BASE_URL=http://127.0.0.1:8001/v1 ...

Chat answers are produced by the ``MockLLMProvider`` handlers, selected by the
``json_schema.name`` (OpenAI) or schema ``title`` (Ollama) in the request.
Never deploy this server.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from app.llm.base import ChatMessage
from app.llm.mock_client import DEFAULT_HANDLERS, Handler
from app.rag.embeddings import HashingEmbeddings
from app.rag.reranker import LexicalReranker

HANDLERS_BY_NAME: dict[str, Handler] = {model.__name__: h for model, h in DEFAULT_HANDLERS.items()}


def _answer(schema_name: str, messages: list[ChatMessage]) -> str:
    handler = HANDLERS_BY_NAME.get(schema_name)
    if handler is None:
        raise HTTPException(status_code=400, detail=f"no mock handler for schema {schema_name}")
    produced = handler(messages)
    if isinstance(produced, BaseModel):
        return produced.model_dump_json()
    if isinstance(produced, dict):
        import json  # noqa: PLC0415

        return json.dumps(produced)
    return produced


def create_mock_llm_app(*, dimensions: int = 384) -> FastAPI:
    api = FastAPI(title="ERP Insight AI mock model server", docs_url=None, redoc_url=None)
    embedder = HashingEmbeddings(dimensions)
    reranker = LexicalReranker()
    state: dict[str, Any] = {"fail_next": 0, "requests": []}
    api.state.mock = state

    def maybe_fail() -> None:
        if state["fail_next"] > 0:
            state["fail_next"] -= 1
            raise HTTPException(status_code=503, detail="injected failure")

    @api.post("/v1/chat/completions")
    async def chat(request: Request) -> dict[str, Any]:
        body = await request.json()
        state["requests"].append(body)
        maybe_fail()
        schema_name = body.get("response_format", {}).get("json_schema", {}).get("name", "")
        content = _answer(schema_name, body["messages"])
        return {
            "id": "mock-1",
            "object": "chat.completion",
            "model": body.get("model"),
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 42, "completion_tokens": 17, "total_tokens": 59},
        }

    @api.post("/api/chat")
    async def ollama_chat(request: Request) -> dict[str, Any]:
        body = await request.json()
        state["requests"].append(body)
        maybe_fail()
        schema_name = str((body.get("format") or {}).get("title", ""))
        content = _answer(schema_name, body["messages"])
        return {
            "message": {"role": "assistant", "content": content},
            "prompt_eval_count": 40,
            "eval_count": 15,
        }

    @api.post("/v1/embeddings")
    async def embeddings(request: Request) -> dict[str, Any]:
        body = await request.json()
        maybe_fail()
        inputs = body["input"] if isinstance(body["input"], list) else [body["input"]]
        vectors = await embedder.embed_documents(inputs)
        return {
            "data": [{"index": i, "embedding": v} for i, v in enumerate(vectors)],
            "model": body.get("model"),
        }

    @api.post("/api/embed")
    async def ollama_embed(request: Request) -> dict[str, Any]:
        body = await request.json()
        maybe_fail()
        return {"embeddings": await embedder.embed_documents(list(body["input"]))}

    @api.post("/v1/rerank")
    @api.post("/rerank")
    async def rerank(request: Request) -> dict[str, Any]:
        body = await request.json()
        maybe_fail()
        scores = await reranker.rerank(body["query"], body["documents"])
        return {"results": [{"index": i, "relevance_score": s} for i, s in enumerate(scores)]}

    return api


app = create_mock_llm_app()
