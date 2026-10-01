#!/usr/bin/env python3
"""Validate the semantic layer and (re)build a connection's semantic index.

    # CI / pre-commit: validate YAML only (no database, no vector store)
    python scripts/ingest_semantic_layer.py --validate-only

    # Operator: rebuild the index for one connection (runs in-process, not via Celery)
    python scripts/ingest_semantic_layer.py --tenant demo --connection "Demo ERP"

Uses the same settings as the API (``.env`` / environment): APP_DATABASE_URL,
QDRANT_URL, EMBEDDING_*. Requires a successful metadata sync for the connection.
Run with the API virtualenv (apps/api/.venv).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "api"))

import httpx  # noqa: E402
from sqlalchemy import func, select  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db.models import DatabaseConnection, SemanticVersion, Tenant  # noqa: E402
from app.db.session import Database  # noqa: E402
from app.rag.embeddings import SparseEncoder, build_embedding_provider  # noqa: E402
from app.rag.ingestion import IngestionDeps, run_ingestion  # noqa: E402
from app.rag.qdrant_client import VectorStore, build_qdrant_client  # noqa: E402
from app.semantic.loader import SemanticLayerError, load_semantic_layer  # noqa: E402
from app.services.audit_service import AuditService  # noqa: E402


def validate(path: str) -> int:
    try:
        bundle = load_semantic_layer(path)
    except SemanticLayerError as exc:
        for issue in exc.issues:
            sys.stderr.write(f"ERROR {issue}\n")
        return 1
    for warning in bundle.warnings:
        sys.stderr.write(f"WARN  {warning}\n")
    sys.stdout.write(
        f"semantic layer OK: {len(bundle.entities)} entities, {len(bundle.metrics)} metrics, "
        f"{len(bundle.joins)} joins, {len(bundle.terms)} terms, {len(bundle.abbreviations)} "
        f"abbreviations, {len(bundle.examples)} examples (hash {bundle.content_hash[:12]})\n"
    )
    return 0


async def ingest(tenant_slug: str, connection_name: str) -> int:
    settings = get_settings()
    if validate(settings.semantic_layer_path):
        return 1
    db = Database(settings)
    key = settings.qdrant_api_key.get_secret_value() if settings.qdrant_api_key else None
    qdrant = build_qdrant_client(settings.qdrant_url, key)
    try:
        async with db.session_factory() as session:
            conn = await session.scalar(
                select(DatabaseConnection)
                .join(Tenant, Tenant.id == DatabaseConnection.tenant_id)
                .where(
                    Tenant.slug == tenant_slug,
                    DatabaseConnection.name == connection_name,
                    DatabaseConnection.deleted_at.is_(None),
                )
            )
            if conn is None:
                sys.stderr.write("error: connection not found for that tenant\n")
                return 1
            current = await session.scalar(
                select(func.max(SemanticVersion.version)).where(
                    SemanticVersion.connection_id == conn.id
                )
            )
            row = SemanticVersion(
                id=uuid.uuid4(),
                tenant_id=conn.tenant_id,
                connection_id=conn.id,
                version=(current or 0) + 1,
                status="pending",
                document_counts={},
                entity_bindings={},
            )
            session.add(row)
            await session.commit()
            version_id = row.id
        async with httpx.AsyncClient() as http:
            embedder = build_embedding_provider(settings, http)
            await run_ingestion(
                IngestionDeps(
                    settings=settings,
                    session_factory=db.session_factory,
                    store=VectorStore(
                        qdrant,
                        prefix=settings.qdrant_collection_prefix,
                        dimensions=embedder.dimensions,
                    ),
                    embedder=embedder,
                    sparse=SparseEncoder(),
                    audit=AuditService(db.session_factory),
                ),
                version_id,
            )
        async with db.session_factory() as session:
            done = await session.get(SemanticVersion, version_id)
            assert done is not None  # noqa: S101 - just created
            sys.stdout.write(
                f"version {done.version}: {done.status}"
                + (f" ({done.error_code})" if done.error_code else "")
                + f" documents={done.document_counts}\n"
            )
            return 0 if done.status == "active" else 1
    finally:
        await qdrant.close()
        await db.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--path", help="semantic-layer directory (default: SEMANTIC_LAYER_PATH)")
    parser.add_argument("--tenant", help="tenant slug")
    parser.add_argument("--connection", help="connection name")
    args = parser.parse_args()
    if args.validate_only:
        raise SystemExit(validate(args.path or get_settings().semantic_layer_path))
    if not args.tenant or not args.connection:
        parser.error("--tenant and --connection are required unless --validate-only")
    raise SystemExit(asyncio.run(ingest(args.tenant, args.connection)))


if __name__ == "__main__":
    main()
