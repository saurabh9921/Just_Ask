"""Onboard a live ERP database through the public API.

    python -m app.onboarding connect --name "ERP production" --type postgresql \
        --host erp-replica.corp --port 5432 --database erp --schema reporting \
        --db-user erp_insight_ro --tls verify-full
    python -m app.onboarding draft   --name "ERP production" --objects "sales_*,customers"
    #   ... review semantic-layer/entities/*.yaml ...
    python -m app.onboarding ingest  --name "ERP production"
    python -m app.onboarding status  --name "ERP production"

Every step goes through the running API as a signed-in administrator, so the same
authorization, SSRF host policy, read-only posture check and audit trail apply as for
any other client. Common options: ``--api-url`` (default ``http://127.0.0.1:8080``),
``--tenant`` and ``--email``. Passwords are **never** accepted as arguments: the admin
password comes from ``ERP_ADMIN_PASSWORD`` or a prompt, the database password from
``ERP_DB_PASSWORD`` or a prompt (or use ``--secret-ref env://...`` / ``vault://...``).
"""

from __future__ import annotations

import argparse
import asyncio
import fnmatch
import getpass
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from app.config import _default_semantic_layer_path
from app.connectors.common.types import TlsMode
from app.semantic.drafting import (
    DraftSet,
    DraftSource,
    check_with_layer,
    draft_semantic_layer,
    write_drafts,
)
from app.semantic.loader import SemanticLayerError, load_semantic_layer

DEFAULT_API_URL = "http://127.0.0.1:8080"
MAX_OBJECTS_WITHOUT_FILTER = 40
POLL_SECONDS = 2.0


class OnboardingError(RuntimeError):
    """A step failed; the message is safe to print (no secrets, no raw responses)."""


def _error(response: httpx.Response, what: str) -> OnboardingError:
    try:
        error = response.json().get("error", {})
        detail = f"{error.get('code')}: {error.get('message')}"
        extra = error.get("details") or {}
        if extra:
            detail += f" {extra}"
    except ValueError:
        detail = f"HTTP {response.status_code}"
    return OnboardingError(f"{what} failed - {detail}")


@dataclass
class OnboardingClient:
    """Thin, typed wrapper over the admin endpoints used for onboarding."""

    http: httpx.AsyncClient
    headers: dict[str, str]

    @classmethod
    async def login(
        cls, http: httpx.AsyncClient, *, tenant: str, email: str, password: str
    ) -> OnboardingClient:
        r = await http.post(
            "/api/v1/auth/login", json={"tenant": tenant, "email": email, "password": password}
        )
        if r.status_code != 200:
            raise _error(r, "sign-in")
        return cls(http, {"Authorization": f"Bearer {r.json()['access_token']}"})

    async def _json(self, method: str, path: str, what: str, **kwargs: Any) -> Any:
        r = await self.http.request(method, path, headers=self.headers, **kwargs)
        if r.status_code >= 400:
            raise _error(r, what)
        return r.json() if r.content else None

    async def find_connection(self, name: str) -> dict[str, Any] | None:
        page = await self._json("GET", "/api/v1/connections", "list connections")
        return next((c for c in page["items"] if c["name"] == name), None)

    async def require_connection(self, name: str) -> dict[str, Any]:
        conn = await self.find_connection(name)
        if conn is None:
            raise OnboardingError(f"no connection named '{name}' (run `connect` first)")
        return conn

    async def create_connection(self, body: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = await self._json(
            "POST", "/api/v1/connections", "create connection", json=body
        )
        return result

    async def test_connection(self, connection_id: str) -> dict[str, Any]:
        result: dict[str, Any] = await self._json(
            "POST", f"/api/v1/connections/{connection_id}/test", "connection test"
        )
        return result

    async def sync_metadata(self, connection_id: str, wait_seconds: float) -> dict[str, Any]:
        await self._json(
            "POST", f"/api/v1/connections/{connection_id}/metadata/sync", "metadata sync"
        )
        return await self._wait(
            f"/api/v1/connections/{connection_id}/sync-status",
            done={"succeeded", "failed"},
            wait_seconds=wait_seconds,
            what="metadata sync",
        )

    async def ingest(self, connection_id: str, wait_seconds: float) -> dict[str, Any]:
        await self._json(
            "POST", f"/api/v1/connections/{connection_id}/semantic/ingest", "semantic ingest"
        )
        return await self._wait(
            f"/api/v1/connections/{connection_id}/semantic/status",
            done={"active", "failed"},
            wait_seconds=wait_seconds,
            what="semantic ingest",
        )

    async def _wait(
        self, path: str, *, done: set[str], wait_seconds: float, what: str
    ) -> dict[str, Any]:
        deadline = time.monotonic() + wait_seconds
        while True:
            status = await self._json("GET", path, f"{what} status")
            latest: dict[str, Any] = status.get("latest") or {}
            if latest.get("status") in done:
                return latest
            if time.monotonic() > deadline:
                raise OnboardingError(f"{what} did not finish within {wait_seconds:.0f} s")
            await asyncio.sleep(POLL_SECONDS)

    async def list_objects(self, connection_id: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = await self._json(
                "GET",
                f"/api/v1/connections/{connection_id}/entities",
                "list discovered objects",
                params={"limit": 500, "offset": offset},
            )
            items += page["items"]
            offset += len(page["items"])
            if not page["items"] or offset >= page["total"]:
                return items

    async def object_detail(self, connection_id: str, qualified: str) -> dict[str, Any]:
        result: dict[str, Any] = await self._json(
            "GET",
            f"/api/v1/connections/{connection_id}/entities/{qualified}",
            f"read {qualified}",
        )
        return result


# ------------------------------------------------------------------ steps
def select_objects(objects: list[dict[str, Any]], patterns: str | None) -> list[dict[str, Any]]:
    """Filter by comma-separated globs on ``name`` or ``schema.name`` (case-insensitive)."""
    if not patterns:
        if len(objects) > MAX_OBJECTS_WITHOUT_FILTER:
            raise OnboardingError(
                f"{len(objects)} objects discovered - choose some with --objects "
                "(comma-separated patterns, e.g. 'sales_*,customers,reporting.v_*')"
            )
        return objects
    globs = [p.strip().lower() for p in patterns.split(",") if p.strip()]
    chosen = [
        o
        for o in objects
        if any(
            fnmatch.fnmatchcase(o["name"].lower(), g)
            or fnmatch.fnmatchcase(f"{o['schema_name']}.{o['name']}".lower(), g)
            for g in globs
        )
    ]
    if not chosen:
        raise OnboardingError("no discovered object matches --objects")
    return chosen


async def draft_for_connection(
    client: OnboardingClient,
    connection: dict[str, Any],
    *,
    layer_root: Path,
    patterns: str | None,
    overwrite: bool,
) -> tuple[DraftSet, list[Path]]:
    objects = select_objects(await client.list_objects(connection["id"]), patterns)
    details = [
        await client.object_detail(connection["id"], f"{o['schema_name']}.{o['name']}")
        for o in objects
    ]
    try:
        existing = load_semantic_layer(layer_root)
    except SemanticLayerError as exc:
        raise OnboardingError(f"the current semantic layer is invalid: {exc}") from exc
    drafts = draft_semantic_layer(
        [DraftSource.from_api(d) for d in details],
        existing.policy,
        existing_entities=existing.entities,
    )
    try:
        check_with_layer(layer_root, drafts, connection["name"])
    except SemanticLayerError as exc:  # pragma: no cover - drafts are valid by design
        raise OnboardingError(f"drafts do not validate: {exc}") from exc
    try:
        written = write_drafts(layer_root, drafts, connection["name"], overwrite=overwrite)
    except FileExistsError as exc:
        raise OnboardingError(
            f"draft files already exist ({exc}); edit them, or pass --force to regenerate"
        ) from exc
    return drafts, written


# ------------------------------------------------------------------ CLI
def _secret(env: str, prompt: str) -> str:
    value = os.environ.get(env)
    if value:
        return value
    if not sys.stdin.isatty():
        raise OnboardingError(f"set {env} (no terminal to prompt on)")
    return getpass.getpass(prompt)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.onboarding", description=__doc__)
    parser.add_argument("--api-url", default=os.environ.get("ERP_API_URL", DEFAULT_API_URL))
    parser.add_argument("--tenant", default=os.environ.get("ERP_TENANT"))
    parser.add_argument("--email", default=os.environ.get("ERP_ADMIN_EMAIL"))
    parser.add_argument("--wait", type=float, default=900.0, help="seconds to wait for sync/ingest")
    sub = parser.add_subparsers(dest="command", required=True)

    c = sub.add_parser("connect", help="create (or reuse) a connection, test it, sync metadata")
    c.add_argument("--name", required=True)
    c.add_argument(
        "--type", dest="connector_type", required=True, choices=["postgresql", "mysql", "sqlserver"]
    )
    c.add_argument("--host", required=True)
    c.add_argument("--port", type=int)
    c.add_argument("--database", required=True)
    c.add_argument("--schema")
    c.add_argument("--db-user", required=True)
    c.add_argument("--secret-ref", help="env://ERP_SECRET_<TENANT>_<NAME> or vault://...")
    c.add_argument(
        "--tls",
        default="verify-full",
        choices=[m.value for m in TlsMode],
    )
    c.add_argument("--replica-host")
    c.add_argument("--replica-port", type=int)
    c.add_argument("--max-rows", type=int, default=1000)
    c.add_argument("--timeout-seconds", type=int, default=30, dest="query_timeout")

    d = sub.add_parser("draft", help="draft semantic entities from the discovered objects")
    d.add_argument("--name", required=True)
    d.add_argument("--objects", help="comma-separated patterns, e.g. 'sales_*,reporting.v_*'")
    d.add_argument(
        "--layer",
        type=Path,
        default=Path(os.environ.get("SEMANTIC_LAYER_PATH") or _default_semantic_layer_path()),
        help="semantic layer directory to write into",
    )
    d.add_argument("--force", action="store_true", help="overwrite existing draft files")
    d.add_argument("--list", action="store_true", help="only list discovered objects")

    i = sub.add_parser("ingest", help="build the semantic index for the connection")
    i.add_argument("--name", required=True)
    s = sub.add_parser("status", help="show connection, metadata and index status")
    s.add_argument("--name", required=True)
    return parser


def _out(line: str) -> None:
    sys.stdout.write(line + "\n")


async def _run(args: argparse.Namespace) -> None:  # noqa: PLR0912, PLR0915 - command dispatch
    if not args.tenant or not args.email:
        raise OnboardingError("--tenant and --email are required (or ERP_TENANT/ERP_ADMIN_EMAIL)")
    password = _secret("ERP_ADMIN_PASSWORD", f"password for {args.email}: ")
    async with httpx.AsyncClient(base_url=args.api_url.rstrip("/"), timeout=120) as http:
        client = await OnboardingClient.login(
            http, tenant=args.tenant, email=args.email, password=password
        )
        if args.command == "connect":
            conn = await client.find_connection(args.name)
            if conn is None:
                body: dict[str, Any] = {
                    "name": args.name,
                    "connector_type": args.connector_type,
                    "host": args.host,
                    "database_name": args.database,
                    "username": args.db_user,
                    "tls_mode": args.tls,
                    "max_rows": args.max_rows,
                    "query_timeout_seconds": args.query_timeout,
                }
                optional = {
                    "port": args.port,
                    "schema_name": args.schema,
                    "read_replica_host": args.replica_host,
                    "read_replica_port": args.replica_port,
                }
                body.update({k: v for k, v in optional.items() if v is not None})
                if args.secret_ref:
                    body["secret_ref"] = args.secret_ref
                else:
                    body["password"] = _secret("ERP_DB_PASSWORD", f"password for {args.db_user}: ")
                conn = await client.create_connection(body)
                _out(f"connection created: {conn['name']} ({conn['id']})")
            else:
                _out(f"using existing connection: {conn['name']} ({conn['id']})")
            test = await client.test_connection(conn["id"])
            if not test.get("ok"):
                raise OnboardingError(
                    f"connection test failed: {test.get('error_code')} - {test.get('message')}"
                )
            _out(f"connection test ok ({test.get('latency_ms')} ms, {test.get('server_version')})")
            for warning in test.get("warnings") or []:
                _out(f"  warning: {warning}")
            sync = await client.sync_metadata(conn["id"], args.wait)
            if sync.get("status") != "succeeded":
                raise OnboardingError(f"metadata sync failed: {sync.get('error_code')}")
            _out(f"metadata synced: {sync.get('object_count')} tables/views discovered")
            _out("next: python -m app.onboarding draft --name " + repr(args.name))
        elif args.command == "draft":
            conn = await client.require_connection(args.name)
            if args.list:
                for o in await client.list_objects(conn["id"]):
                    rows = o.get("row_estimate")
                    _out(f"  {o['schema_name']}.{o['name']}  ({o['kind']}, ~{rows} rows)")
                return
            drafts, written = await draft_for_connection(
                client, conn, layer_root=args.layer, patterns=args.objects, overwrite=args.force
            )
            for entity in drafts.entities:
                forbidden = entity.document["security"]["forbidden_fields"]
                _out(
                    f"  {entity.entity:<32} dims={len(entity.document['dimensions'])} "
                    f"measures={len(entity.document['measures'])} "
                    f"time={len(entity.document['time_fields'])} "
                    f"hidden={len(forbidden)} skipped={len(entity.skipped)}"
                )
            for path in written:
                _out(f"wrote {path}")
            _out(
                "REVIEW the drafts, then: python -m app.onboarding ingest --name " + repr(args.name)
            )
        elif args.command == "ingest":
            conn = await client.require_connection(args.name)
            latest = await client.ingest(conn["id"], args.wait)
            if latest.get("status") != "active":
                code = latest.get("error_code")
                hint = (
                    " - no semantic entity matches this database yet: run `draft` first"
                    if code == "NO_ENTITY_BOUND"
                    else ""
                )
                raise OnboardingError(f"semantic ingest failed: {code}{hint}")
            bound = latest.get("entity_bindings") or {}
            _out(f"semantic index active: version {latest.get('version')}, {len(bound)} entities")
            _out("users can now ask questions about this connection in the extension")
        else:
            conn = await client.require_connection(args.name)
            _out(f"connection: {conn['name']} status={conn['status']} ({conn['connector_type']})")
            sync = await client._json(
                "GET", f"/api/v1/connections/{conn['id']}/sync-status", "sync"
            )
            sem = await client._json(
                "GET", f"/api/v1/connections/{conn['id']}/semantic/status", "semantic status"
            )
            _out(f"metadata: {(sync.get('latest') or {}).get('status')} at {sync.get('synced_at')}")
            _out(f"semantic index: active version {sem.get('active_version')}")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        asyncio.run(_run(args))
    except OnboardingError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1
    except httpx.HTTPError as exc:
        sys.stderr.write(f"error: cannot reach the API at {args.api_url} ({type(exc).__name__})\n")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
