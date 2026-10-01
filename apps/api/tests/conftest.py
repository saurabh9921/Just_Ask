"""Shared fixtures.

Unit and security tests run fully in-process: SQLite (aiosqlite) for the metadata
DB, fakeredis for Redis and fake readiness checks - no Docker required. Integration
tests opt in to real services via environment variables (tests/integration/conftest.py).
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from fakeredis import FakeAsyncRedis
from fastapi import FastAPI
from sqlalchemy import select

# Settings are read from the environment; make sure a developer's .env never
# leaks into tests.
os.environ.setdefault("APP_ENV", "test")
# The product ships an empty, customer-owned semantic layer; tests use the demo ERP's.
os.environ["SEMANTIC_LAYER_PATH"] = str(
    Path(__file__).resolve().parent / "fixtures" / "semantic-layer"
)

from app.config import AppEnv, Settings
from app.core.permissions import Role
from app.core.security import PasswordService
from app.db.bootstrap import create_schema_with_rbac
from app.db.models import Role as RoleModel
from app.db.models import Tenant, User, UserRole
from app.dependencies import AppResources
from app.main import build_resources, close_resources, create_app
from app.services.health_service import HealthCheck

DEMO_SEMANTIC_LAYER = Path(os.environ["SEMANTIC_LAYER_PATH"])
TEST_EXTENSION_ORIGIN = "chrome-extension://abcdefghijklmnopabcdefghijklmnop"
DEFAULT_PASSWORD = "Correct-Horse-Battery-9"


class FakeCheck:
    """Configurable readiness check for tests."""

    def __init__(
        self, name: str, *, fail: bool = False, delay: float = 0.0, error: str = "boom"
    ) -> None:
        self.name = name
        self._fail = fail
        self._delay = delay
        self._error = error

    async def check(self) -> None:
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._fail:
            raise ConnectionError(self._error)


@pytest.fixture
def sqlite_url(tmp_path: Path) -> str:
    return f"sqlite+aiosqlite:///{(tmp_path / 'app.db').as_posix()}"


@pytest.fixture
def sqlite_data_dir(tmp_path: Path) -> Path:
    path = tmp_path / "sqlite-data"
    path.mkdir()
    return path


def make_settings(sqlite_url: str, sqlite_data_dir: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "app_env": AppEnv.TEST,
        "app_database_url": sqlite_url,
        "redis_url": "redis://127.0.0.1:1/0",
        "log_json": True,
        "cors_allowed_origins": [TEST_EXTENSION_ORIGIN],
        "readiness_check_timeout_seconds": 0.5,
        # Cheap Argon2 parameters keep the suite fast; production uses the defaults.
        "password_hash_time_cost": 1,
        "password_hash_memory_kib": 1024,
        "password_hash_parallelism": 1,
        "sqlite_data_dir": str(sqlite_data_dir),
        "export_dir": str(sqlite_data_dir.parent / "exports"),
        "metadata_sync_mode": "inline",
        "qdrant_url": ":memory:",
        "llm_provider": "mock",
        "embedding_provider": "hashing",
        "embedding_dimensions": 256,
        "jwt_secret": "test-jwt-secret-" + "j" * 40,
        "encryption_key": "test-encryption-key-" + "e" * 40,
        "semantic_layer_path": str(DEMO_SEMANTIC_LAYER),
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


@pytest.fixture
def settings(sqlite_url: str, sqlite_data_dir: Path) -> Settings:
    return make_settings(sqlite_url, sqlite_data_dir)


async def prepare_database(settings: Settings) -> None:
    """Create the schema and seed the RBAC catalog (mirrors migration 0001)."""
    await create_schema_with_rbac(settings)


@pytest.fixture
async def prepared_db(settings: Settings) -> Settings:
    await prepare_database(settings)
    return settings


AppFactory = Callable[..., FastAPI]


@pytest.fixture
async def app_factory(prepared_db: Settings) -> AsyncIterator[AppFactory]:
    created: list[AppResources] = []

    def _factory(
        checks: list[HealthCheck] | None = None,
        required: set[str] | None = None,
        settings_override: Settings | None = None,
    ) -> FastAPI:
        effective = settings_override or prepared_db
        if required is not None:
            effective = effective.model_copy(update={"readiness_required_checks": list(required)})
        default_checks: list[HealthCheck] = [
            FakeCheck("postgres"),
            FakeCheck("redis"),
            FakeCheck("qdrant"),
        ]
        resources = build_resources(
            effective,
            redis=FakeAsyncRedis(),
            health_checks=checks if checks is not None else default_checks,
        )
        created.append(resources)
        return create_app(effective, resources=resources)

    yield _factory

    for res in created:
        await close_resources(res)


@pytest.fixture
async def app(app_factory: AppFactory) -> FastAPI:
    return app_factory()


def client_for(app: FastAPI, **headers: str) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver", headers=headers)


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with client_for(app) as c:
        yield c


def resources_of(app: FastAPI) -> AppResources:
    resources: AppResources = app.state.resources
    return resources


# --------------------------------------------------------------------------
# Tenant / user factories
# --------------------------------------------------------------------------
@dataclass
class Account:
    tenant_id: uuid.UUID
    tenant_slug: str
    user_id: uuid.UUID
    email: str
    password: str


async def create_account(
    app: FastAPI,
    *,
    tenant_slug: str,
    email: str,
    roles: list[Role],
    password: str = DEFAULT_PASSWORD,
    active: bool = True,
) -> Account:
    res = resources_of(app)
    async with res.database.session_factory() as session:
        tenant = await session.scalar(select(Tenant).where(Tenant.slug == tenant_slug))
        if tenant is None:
            tenant = Tenant(
                id=uuid.uuid4(),
                slug=tenant_slug,
                name=tenant_slug.title(),
                status="active",
                settings={},
            )
            session.add(tenant)
            await session.flush()
        user = User(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            email=email,
            display_name=email.split("@", maxsplit=1)[0].title(),
            password_hash=PasswordService(res.settings).hash(password),
            is_active=active,
            failed_login_count=0,
        )
        session.add(user)
        await session.flush()
        role_ids = dict(
            (await session.execute(select(RoleModel.name, RoleModel.id))).tuples().all()
        )
        for role in roles:
            session.add(
                UserRole(user_id=user.id, role_id=role_ids[role.value], tenant_id=tenant.id)
            )
        await session.commit()
        return Account(tenant.id, tenant_slug, user.id, email, password)


async def login(client: httpx.AsyncClient, account: Account) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/auth/login",
        json={"tenant": account.tenant_slug, "email": account.email, "password": account.password},
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def bearer(tokens: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens['access_token']}"}


# --------------------------------------------------------------------------
# A small SQLite "ERP" used by the SQLite connector and contract tests
# --------------------------------------------------------------------------
ERP_DDL = """
CREATE TABLE regions (region_id INTEGER PRIMARY KEY, region_name TEXT NOT NULL);
CREATE TABLE customers (
    customer_id INTEGER PRIMARY KEY,
    customer_name TEXT NOT NULL,
    region_id INTEGER NOT NULL REFERENCES regions(region_id),
    credit_limit NUMERIC(12, 2)
);
CREATE TABLE sales_orders (
    order_id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(customer_id),
    order_date DATE NOT NULL,
    net_amount NUMERIC(12, 2) NOT NULL,
    status VARCHAR(20) NOT NULL
);
CREATE VIEW analytics_sales_orders AS
    SELECT o.order_id, o.order_date, o.net_amount, o.status, r.region_name
    FROM sales_orders o
    JOIN customers c ON c.customer_id = o.customer_id
    JOIN regions r ON r.region_id = c.region_id;
"""


def build_sqlite_erp(path: Path, orders: int = 50) -> Path:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(ERP_DDL)
        conn.executemany(
            "INSERT INTO regions VALUES (?, ?)", [(1, "North"), (2, "South"), (3, "West")]
        )
        conn.executemany(
            "INSERT INTO customers VALUES (?, ?, ?, ?)",
            [(i, f"Customer {i}", (i % 3) + 1, 1000.0 * i) for i in range(1, 11)],
        )
        conn.executemany(
            "INSERT INTO sales_orders VALUES (?, ?, ?, ?, ?)",
            [
                (
                    i,
                    (i % 10) + 1,
                    f"2026-0{(i % 9) + 1}-15",
                    100.0 + i,
                    "open" if i % 2 else "closed",
                )
                for i in range(1, orders + 1)
            ],
        )
        conn.commit()
    finally:
        conn.close()
    return path


@pytest.fixture
def sqlite_erp(sqlite_data_dir: Path) -> Path:
    return build_sqlite_erp(sqlite_data_dir / "erp.db")
