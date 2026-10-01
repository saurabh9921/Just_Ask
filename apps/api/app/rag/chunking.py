"""Deterministic documents, chunks and point IDs.

The same input always yields the same chunks and the same point IDs
(UUIDv5 over tenant/connection/collection/doc key/chunk/version), so ingestion
is idempotent and re-runs never duplicate points.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Final

from app.rag.collections import Collection

POINT_NAMESPACE: Final = uuid.UUID("7d0b4a0e-6a53-4f5c-9f0e-2f3a1c5b8e11")
DEFAULT_MAX_CHARS: Final = 1200
DEFAULT_OVERLAP: Final = 150


@dataclass(frozen=True)
class SourceDocument:
    collection: Collection
    doc_key: str
    title: str
    text: str
    entity_name: str | None = None
    permissions: tuple[str, ...] = ("analytics.query",)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Chunk:
    document: SourceDocument
    index: int
    text: str


def chunk_text(
    text: str, *, max_chars: int = DEFAULT_MAX_CHARS, overlap: int = DEFAULT_OVERLAP
) -> list[str]:
    """Split on paragraphs, then sentences, then words; windows overlap by ~``overlap`` chars."""
    text = text.strip()
    if len(text) <= max_chars:
        return [text] if text else []
    units: list[str] = []
    for paragraph in re.split(r"\n\s*\n", text):
        for raw_sentence in re.split(r"(?<=[.!?;])\s+|\n", paragraph):
            sentence = raw_sentence.strip()
            while len(sentence) > max_chars:  # pathological: no boundaries at all
                cut = sentence.rfind(" ", 0, max_chars)
                cut = cut if cut > 0 else max_chars
                units.append(sentence[:cut])
                sentence = sentence[cut:].strip()
            if sentence:
                units.append(sentence)
    chunks: list[str] = []
    current: list[str] = []
    for unit in units:
        candidate = " ".join([*current, unit])
        if current and len(candidate) > max_chars:
            chunks.append(" ".join(current))
            tail: list[str] = []
            for previous in reversed(current):
                if sum(len(t) + 1 for t in tail) + len(previous) > overlap:
                    break
                tail.insert(0, previous)
            current = [*tail, unit]
        else:
            current.append(unit)
    if current:
        chunks.append(" ".join(current))
    return chunks


def chunk_document(doc: SourceDocument) -> list[Chunk]:
    return [Chunk(doc, i, part) for i, part in enumerate(chunk_text(doc.text))]


def point_id(*, tenant_id: uuid.UUID, connection_id: uuid.UUID, chunk: Chunk, version: int) -> str:
    doc = chunk.document
    key = (
        f"{tenant_id}|{connection_id}|{doc.collection.value}|{doc.doc_key}|{chunk.index}|{version}"
    )
    return str(uuid.uuid5(POINT_NAMESPACE, key))
