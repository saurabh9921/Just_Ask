"""HashiCorp Vault KV v2 reader for ``vault://`` secret references.

Reference format: ``vault://<mount>/<path>#<field>``, e.g.
``vault://secret/erp-insight/acme/erp-prod#password``. The path must start with the
tenant's prefix (``VAULT_TENANT_PREFIX``, default ``erp-insight/{tenant}/``), so a
reference can never reach another tenant's or the platform's secrets. The platform
only *reads*; writing secrets to Vault is the operator's job.

The Vault token comes from ``VAULT_TOKEN`` or, preferably, ``VAULT_TOKEN_FILE``
(renewed by a Vault Agent sidecar); it is read per request and never logged.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from pydantic import SecretStr

from app.config import Settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_MOUNT = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_FIELD = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")


class VaultReferenceError(ValueError):
    """The reference is malformed or outside the tenant's path."""


class VaultUnavailableError(RuntimeError):
    """Vault could not return the secret (details are logged, not raised to users)."""


@dataclass(frozen=True, slots=True)
class VaultRef:
    mount: str
    path: str
    field: str


def tenant_prefix(settings: Settings, tenant_slug: str) -> str:
    return settings.vault_tenant_prefix.format(tenant=tenant_slug)


def parse_vault_ref(ref: str, *, settings: Settings, tenant_slug: str) -> VaultRef:
    body = ref.removeprefix("vault://")
    location, sep, field = body.partition("#")
    if not sep or not _FIELD.fullmatch(field):
        raise VaultReferenceError("field")
    mount, _, path = location.partition("/")
    segments = path.split("/")
    if not _MOUNT.fullmatch(mount) or not path or not all(_SEGMENT.fullmatch(s) for s in segments):
        raise VaultReferenceError("path")
    if ".." in segments or any(s.startswith(".") for s in segments):
        raise VaultReferenceError("path")
    prefix = tenant_prefix(settings, tenant_slug)
    if not path.startswith(prefix) or len(path) == len(prefix.rstrip("/")):
        raise VaultReferenceError("tenant")
    return VaultRef(mount=mount, path=path, field=field)


class VaultClient:
    def __init__(
        self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        if not settings.vault_addr:
            raise ValueError("VAULT_ADDR is not configured")
        self._settings = settings
        self._addr = settings.vault_addr.rstrip("/")
        self._transport = transport

    def _token(self) -> str:
        s = self._settings
        if s.vault_token_file:
            try:
                token = Path(s.vault_token_file).read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise VaultUnavailableError("token file unreadable") from exc
            if token:
                return token
        if s.vault_token is not None:
            return s.vault_token.get_secret_value()
        raise VaultUnavailableError("no Vault token configured")

    async def read(self, ref: VaultRef) -> SecretStr:
        token = await asyncio.to_thread(self._token)  # may read a token file
        headers = {"X-Vault-Token": token, "Accept": "application/json"}
        if self._settings.vault_namespace:
            headers["X-Vault-Namespace"] = self._settings.vault_namespace
        verify: str | bool = self._settings.vault_ca_cert or True
        url = f"{self._addr}/v1/{ref.mount}/data/{ref.path}"
        try:
            async with httpx.AsyncClient(
                timeout=self._settings.vault_timeout_seconds,
                verify=verify,
                transport=self._transport,
                follow_redirects=False,
            ) as client:
                response = await client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            logger.warning("vault request failed", extra={"error_type": type(exc).__name__})
            raise VaultUnavailableError("request failed") from exc
        if response.status_code != 200:
            logger.warning("vault returned an error", extra={"status": response.status_code})
            raise VaultUnavailableError(f"status {response.status_code}")
        try:
            body: Any = response.json()
            value = body["data"]["data"][ref.field]
        except (ValueError, KeyError, TypeError) as exc:
            raise VaultUnavailableError("field missing") from exc
        if not isinstance(value, str) or not value:
            raise VaultUnavailableError("field is not a string")
        return SecretStr(value)
