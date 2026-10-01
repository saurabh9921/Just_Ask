"""Self-contained evaluation environment (no Docker) on the demo ERP test fixture.

Builds, in a temporary directory: a SQLite application database, the demo ERP
(SQLite, tenant ``acme`` plus a second tenant ``globex`` in the same tables), an
in-memory Qdrant, fakeredis, and the real FastAPI app wired to them. Then it creates
users, a governed connection, syncs metadata and builds the semantic index through
the public API - exactly the path a customer takes - before questions are asked.

The LLM (and embeddings) come from the normal settings/environment, so the same
sandbox evaluates the mock provider (default), Ollama or vLLM.
Development tool: requires the ``dev`` extra (fakeredis).
"""

from __future__ import annotations

import secrets
import tempfile
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select

from app.config import Settings
from app.connectors.base import DatabaseAdapter
from app.connectors.common.types import ConnectionConfig, ConnectorType
from app.connectors.factory import ConnectorFactory
from app.core.permissions import ROLE_PERMISSIONS, Role
from app.core.security import PasswordService
from app.db.bootstrap import create_schema_with_rbac
from app.db.models import Role as RoleModel
from app.db.models import Tenant, User, UserRole
from app.dependencies import AppResources
from app.evals.runner import GoldExecutor, GoldResult, UserName
from app.main import build_resources, close_resources, create_app
from app.query.plans import QueryPlan
from app.query.result_formatter import format_result
from app.query.validator import PlanContext, resolve_plan
from app.semantic.loader import SemanticBundle, load_semantic_layer
from app.semantic.validator import bind_to_catalog
from app.services.analytics_service import tenant_today
from tests.fixtures.demo_erp import clone_tenant_sqlite, seed_demo

TENANT = "acme"
OTHER_TENANT = "globex"
DEMO_DB = "demo.db"
DEMO_SEMANTIC_LAYER = Path(__file__).resolve().parents[1] / "fixtures" / "semantic-layer"
GRANTED_ROLES = [Role.TENANT_ADMIN.value, Role.DATA_ADMIN.value, Role.ANALYST.value]


class SandboxError(RuntimeError):
    pass


@dataclass
class Sandbox:
    settings: Settings
    client: httpx.AsyncClient
    connection_id: str
    bundle: SemanticBundle
    gold: GoldExecutor
    headers: dict[str, dict[str, str]] = field(default_factory=dict)

    def has_user(self, user: UserName) -> bool:
        return user in self.headers

    async def ask(self, question: str, user: UserName) -> tuple[int, dict[str, Any]]:
        response = await self.client.post(
            "/api/v1/analytics/query",
            json={"question": question, "connection_id": self.connection_id},
            headers=self.headers[user],
        )
        body = response.json()
        return response.status_code, body if isinstance(body, dict) else {}


def sandbox_settings(base: Settings, workdir: Path) -> Settings:
    data = workdir / "sqlite-data"
    data.mkdir(parents=True, exist_ok=True)
    values = base.model_dump()
    values.update(
        {
            "app_env": "development",
            "app_database_url": f"sqlite+aiosqlite:///{(workdir / 'app.db').as_posix()}",
            "sqlite_data_dir": str(data),
            "sqlite_connections_enabled": True,
            "export_dir": str(workdir / "exports"),
            "qdrant_url": ":memory:",
            "qdrant_api_key": None,
            "metadata_sync_mode": "inline",
            "semantic_layer_path": str(DEMO_SEMANTIC_LAYER),
            "query_rate_limit_per_user_per_minute": 100_000,
            "query_rate_limit_per_tenant_per_minute": 100_000,
            # Measure real work: every question executes.
            "query_result_cache_enabled": False,
            "jwt_secret": secrets.token_urlsafe(48),
            "encryption_key": secrets.token_urlsafe(48),
        }
    )
    for key, value in list(values.items()):
        if hasattr(value, "get_secret_value"):
            values[key] = value.get_secret_value()
    return Settings(_env_file=None, **values)


async def _create_user(
    settings: Settings, resources: AppResources, tenant: Tenant, email: str, role: Role
) -> str:
    password = secrets.token_urlsafe(24)
    async with resources.database.session_factory() as session:
        user = User(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            email=email,
            display_name=email.split("@", maxsplit=1)[0],
            password_hash=PasswordService(settings).hash(password),
            is_active=True,
            failed_login_count=0,
        )
        session.add(user)
        await session.flush()
        role_id = await session.scalar(select(RoleModel.id).where(RoleModel.name == role.value))
        session.add(UserRole(user_id=user.id, role_id=role_id, tenant_id=tenant.id))
        await session.commit()
    return password


async def _login(client: httpx.AsyncClient, email: str, password: str) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/login", json={"tenant": TENANT, "email": email, "password": password}
    )
    if response.status_code != 200:
        raise SandboxError(f"login failed ({response.status_code})")
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def _expect(response: httpx.Response, status: int, what: str) -> dict[str, Any]:
    if response.status_code != status:
        raise SandboxError(f"{what} failed ({response.status_code}): {response.text[:300]}")
    body: dict[str, Any] = response.json()
    return body


async def _setup_connection(
    client: httpx.AsyncClient, resources: AppResources, settings: Settings
) -> tuple[Tenant, str, dict[str, dict[str, str]]]:
    """Tenant, users (admin/analyst/ungranted viewer) and an indexed demo connection."""
    async with resources.database.session_factory() as session:
        tenant = Tenant(id=uuid.uuid4(), slug=TENANT, name="Acme", status="active", settings={})
        session.add(tenant)
        await session.commit()
    logins = {
        "admin": ("admin@acme.eval", Role.DATA_ADMIN),
        "analyst": ("analyst@acme.eval", Role.ANALYST),
        "ungranted": ("viewer@acme.eval", Role.VIEWER),
    }
    headers: dict[str, dict[str, str]] = {}
    for key, (email, role) in logins.items():
        password = await _create_user(settings, resources, tenant, email, role)
        headers[key] = await _login(client, email, password)
    admin = headers["admin"]
    conn = await _expect(
        await client.post(
            "/api/v1/connections",
            json={
                "name": "Demo ERP",
                "connector_type": "sqlite",
                "database_name": DEMO_DB,
                "granted_roles": GRANTED_ROLES,
            },
            headers=admin,
        ),
        201,
        "create connection",
    )
    cid = str(conn["id"])
    await _expect(
        await client.post(f"/api/v1/connections/{cid}/metadata/sync", headers=admin),
        202,
        "metadata sync",
    )
    await resources.metadata_jobs.wait_idle()
    await _expect(
        await client.post(f"/api/v1/connections/{cid}/semantic/ingest", headers=admin),
        202,
        "semantic ingest",
    )
    await resources.semantic_jobs.wait_idle()
    return tenant, cid, headers


async def _gold_executor(
    settings: Settings, bundle: SemanticBundle, tenant: Tenant, cid: str, today: date
) -> tuple[GoldExecutor, DatabaseAdapter]:
    """Runs a gold plan through resolve -> compile -> execute -> format directly."""
    adapter = ConnectorFactory(settings).create(
        ConnectionConfig(
            connection_id=uuid.UUID(cid),
            tenant_id=tenant.id,
            connector_type=ConnectorType.SQLITE,
            host=None,
            port=None,
            database=DEMO_DB,
            schema=None,
            username=None,
            password=None,
            max_rows=settings.max_query_rows,
        )
    )
    catalog = (await adapter.get_catalog()).objects
    bindings = bind_to_catalog(bundle, catalog)
    ctx = PlanContext(
        tenant_id=tenant.id,
        tenant_slug=TENANT,
        tenant_settings={},
        connection_id=uuid.UUID(cid),
        semantic_version=1,
        permissions=frozenset(p.value for p in ROLE_PERMISSIONS[Role.ANALYST]),
        today=today,
        max_rows=settings.max_query_rows,
    )

    async def gold(plan: QueryPlan) -> GoldResult:
        resolved = resolve_plan(plan, bundle=bundle, bindings=bindings, catalog=catalog, ctx=ctx)
        compiled = adapter.compile_query_plan(resolved)
        current = await adapter.execute_readonly(compiled.statement, max_rows=resolved.limit)
        previous = None
        if compiled.previous_statement is not None:
            previous = await adapter.execute_readonly(
                compiled.previous_statement, max_rows=resolved.limit
            )
        formatted = format_result(resolved, compiled.output, current, previous, bundle.policy)
        rows: list[list[Any]] = formatted.model_dump(mode="json")["rows"]
        return [c.name for c in formatted.columns], rows

    return gold, adapter


@asynccontextmanager
async def open_sandbox(base: Settings) -> AsyncIterator[Sandbox]:
    try:
        from fakeredis import FakeAsyncRedis
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise SandboxError("the eval sandbox needs the 'dev' extra (fakeredis)") from exc

    with tempfile.TemporaryDirectory(prefix="erp-evals-") as tmp:
        settings = sandbox_settings(base, Path(tmp))
        today = tenant_today({})
        demo_path = Path(settings.sqlite_data_dir) / DEMO_DB
        await seed_demo(
            f"sqlite+aiosqlite:///{demo_path.as_posix()}", anchor_date=today, tenant_code=TENANT
        )
        clone_tenant_sqlite(str(demo_path), OTHER_TENANT)
        await create_schema_with_rbac(settings)
        resources = build_resources(settings, redis=FakeAsyncRedis())
        app = create_app(settings, resources=resources)
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        adapter: DatabaseAdapter | None = None
        try:
            async with httpx.AsyncClient(
                transport=transport, base_url="http://sandbox", timeout=300
            ) as client:
                tenant, cid, headers = await _setup_connection(client, resources, settings)
                bundle = load_semantic_layer(settings.semantic_layer_path)
                gold, adapter = await _gold_executor(settings, bundle, tenant, cid, today)
                yield Sandbox(
                    settings=settings,
                    client=client,
                    connection_id=cid,
                    bundle=bundle,
                    gold=gold,
                    headers={"analyst": headers["analyst"], "ungranted": headers["ungranted"]},
                )
        finally:
            if adapter is not None:
                await adapter.close()
            await close_resources(resources)
