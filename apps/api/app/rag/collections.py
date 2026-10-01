"""Qdrant collection definitions.

Every collection has a named dense vector (``dense``, cosine) and a named sparse
vector (``sparse``, IDF modifier) plus keyword payload indexes on every field
used in mandatory retrieval filters.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from qdrant_client import AsyncQdrantClient
from qdrant_client import models as qm

from app.core.exceptions import AppError
from app.core.logging import get_logger

logger = get_logger(__name__)

DENSE: Final = "dense"
SPARSE: Final = "sparse"


class Collection(StrEnum):
    SEMANTIC_ENTITIES = "semantic_entities"
    BUSINESS_GLOSSARY = "business_glossary"
    QUERY_EXAMPLES = "query_examples"
    DATABASE_METADATA = "database_metadata"
    QUERY_FEEDBACK = "query_feedback"


KEYWORD_INDEXES: Final = (
    "tenant_id",
    "connection_id",
    "document_type",
    "entity_name",
    "permissions",
    "doc_key",
)
INTEGER_INDEXES: Final = ("version",)


class CollectionConfigError(AppError):
    status_code = 500
    code = "VECTOR_STORE_MISCONFIGURED"
    default_message = "The vector store configuration does not match the embedding model."


def physical_name(prefix: str, collection: Collection) -> str:
    return f"{prefix}{collection.value}"


async def ensure_collections(client: AsyncQdrantClient, *, prefix: str, dimensions: int) -> None:
    """Create missing collections/indexes; refuse to reuse one with a different vector size."""
    existing = {c.name for c in (await client.get_collections()).collections}
    for collection in Collection:
        name = physical_name(prefix, collection)
        if name in existing:
            info = await client.get_collection(name)
            vectors = info.config.params.vectors
            size = vectors[DENSE].size if isinstance(vectors, dict) and DENSE in vectors else None
            if size != dimensions:
                raise CollectionConfigError(
                    details={"collection": name, "expected": dimensions, "actual": size}
                )
            continue
        await client.create_collection(
            name,
            vectors_config={DENSE: qm.VectorParams(size=dimensions, distance=qm.Distance.COSINE)},
            sparse_vectors_config={SPARSE: qm.SparseVectorParams(modifier=qm.Modifier.IDF)},
            on_disk_payload=True,
        )
        for field in KEYWORD_INDEXES:
            await client.create_payload_index(name, field, qm.PayloadSchemaType.KEYWORD)
        for field in INTEGER_INDEXES:
            await client.create_payload_index(name, field, qm.PayloadSchemaType.INTEGER)
        logger.info("vector collection created", extra={"collection": name})
