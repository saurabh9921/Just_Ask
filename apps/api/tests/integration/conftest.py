"""Integration test configuration.

Integration tests talk to real services started by Docker Compose:

    docker compose -f infrastructure/docker/docker-compose.yml up -d postgres redis qdrant

and are enabled by environment variables (``scripts/dev.py test-integration`` sets
them for the default compose ports):

    TEST_POSTGRES_ADMIN_URL=postgresql+asyncpg://app:app@localhost:5432/postgres
    TEST_REDIS_URL=redis://localhost:6379/15
    TEST_QDRANT_URL=http://localhost:6333

Each test is skipped (not failed) when its service variable is unset, so the
suite can run partially on machines without Docker. CI sets all of them.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.integration


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"{name} not set; start docker compose services to run this test")
    return value
