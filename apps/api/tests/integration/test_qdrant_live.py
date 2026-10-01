"""Vector store behaviour against a real Qdrant server (docker compose)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest

from app.core.permissions import Permission
from app.rag.collections import Collection
from app.rag.embeddings import HashingEmbeddings, SparseEncoder
from app.rag.qdrant_client import RetrievalScope, VectorPoint, VectorStore, build_qdrant_client
from tests.integration.conftest import require_env

pytestmark = pytest.mark.integration


@pytest.fixture
async def store() -> AsyncIterator[VectorStore]:
    client = build_qdrant_client(require_env("TEST_QDRANT_URL"), None)
    prefix = f"it{uuid.uuid4().hex[:8]}_"
    vs = VectorStore(client, prefix=prefix, dimensions=64)
    await vs.ensure()
    try:
        yield vs
    finally:
        for collection in Collection:
            await client.delete_collection(vs.name(collection))
        await client.close()


async def test_hybrid_legs_and_scope_on_real_qdrant(store: VectorStore) -> None:
    emb, sparse = HashingEmbeddings(64), SparseEncoder()
    tenant, other, conn = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    texts = {"a": "Invoice overdue days", "b": "Warehouse WH-01 stock", "c": "Invoice overdue days"}
    points = []
    for key, text in texts.items():
        idx, val = sparse.encode(text)
        points.append(
            VectorPoint(
                id=str(uuid.uuid4()),
                dense=await emb.embed_query(text),
                sparse_indices=idx,
                sparse_values=val,
                payload={
                    "tenant_id": str(other if key == "c" else tenant),
                    "connection_id": str(conn),
                    "version": 1,
                    "permissions": ["analytics.query"],
                    "doc_key": key,
                },
            )
        )
    await store.upsert(Collection.BUSINESS_GLOSSARY, points, batch_size=2)
    scope = RetrievalScope(tenant, conn, 1, frozenset(p.value for p in Permission))
    dense = await store.search_dense(
        Collection.BUSINESS_GLOSSARY, scope, await emb.embed_query("overdue invoice"), 10
    )
    assert dense[0].payload["doc_key"] == "a"
    assert "c" not in {h.payload["doc_key"] for h in dense}  # other tenant
    q_idx, q_val = sparse.encode("WH-01", query=True)
    lexical = await store.search_sparse(Collection.BUSINESS_GLOSSARY, scope, q_idx, q_val, 10)
    assert lexical[0].payload["doc_key"] == "b"
