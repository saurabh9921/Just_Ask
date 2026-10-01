"""Secret storage and resolution for ERP database credentials.

Supported ``secret_ref`` schemes:

* ``local://<uuid>`` - ciphertext in ``encrypted_secrets``: AES-256-GCM with a key
  derived from ``ENCRYPTION_KEY`` via HKDF-SHA256. The AAD binds the ciphertext
  to its tenant and row ID, so a ciphertext copied to another tenant or row
  fails authentication.
* ``env://ERP_SECRET_<TENANT_SLUG>_<NAME>`` - an environment variable injected by
  the deployment (e.g. from Kubernetes Secrets). The tenant-slug prefix stops one
  tenant from referencing another tenant's (or the platform's) secrets.

* ``vault://<mount>/<path>#<field>`` - HashiCorp Vault KV v2 (Phase 7), enabled by
  ``VAULT_ADDR``; the path must start with the tenant's ``VAULT_TENANT_PREFIX``
  (``app/services/vault_client.py``).

``aws-sm://``, ``gcp-sm://`` and ``azure-kv://`` are reserved and rejected with a clear
error. Plaintext secrets exist only transiently in memory and are never logged.

Key rotation: ciphertexts record the ``key_version`` they were encrypted with. The
cipher knows the current key (``ENCRYPTION_KEY`` / ``ENCRYPTION_KEY_VERSION``) and,
during a rotation, the previous one (``ENCRYPTION_KEY_PREVIOUS``, version - 1);
``rotate_encrypted_secrets`` re-encrypts old rows with the current key.
"""

from __future__ import annotations

import os
import re
import secrets
import uuid
from typing import Final

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from pydantic import SecretStr
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core.exceptions import AppError
from app.core.logging import get_logger
from app.db.models import EncryptedSecret
from app.services.vault_client import (
    VaultClient,
    VaultReferenceError,
    VaultUnavailableError,
    parse_vault_ref,
)

logger = get_logger(__name__)

NONCE_BYTES: Final = 12
MAX_SECRET_LENGTH: Final = 4096
_RESERVED_SCHEMES: Final = ("aws-sm://", "gcp-sm://", "azure-kv://")
_ENV_NAME: Final = re.compile(r"^ERP_SECRET_[A-Z0-9_]{1,100}$")


class SecretReferenceError(AppError):
    status_code = 422
    code = "INVALID_SECRET_REFERENCE"
    default_message = "The secret reference is invalid or not permitted."


class SecretUnavailableError(AppError):
    status_code = 503
    code = "SECRET_UNAVAILABLE"
    default_message = "The connection secret could not be retrieved."


def env_prefix_for(tenant_slug: str) -> str:
    return "ERP_SECRET_" + re.sub(r"[^A-Z0-9]", "_", tenant_slug.upper()) + "_"


def _derive(master: SecretStr, version: int) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"erp-insight-ai/encrypted-secrets",
        info=f"v{version}".encode(),
    ).derive(master.get_secret_value().encode("utf-8"))


class SecretCipher:
    """AES-256-GCM with versioned keys. Also carries the settings the external secret
    backends (Vault) are built from, so every ``SecretStore`` sees the same config."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.version = settings.encryption_key_version
        self._keys = {self.version: _derive(settings.encryption_key, self.version)}
        if settings.encryption_key_previous is not None and self.version > 1:
            previous = self.version - 1
            self._keys[previous] = _derive(settings.encryption_key_previous, previous)

    @staticmethod
    def _aad(tenant_id: uuid.UUID, secret_id: uuid.UUID, purpose: str) -> bytes:
        return f"{tenant_id}|{secret_id}|{purpose}".encode()

    def encrypt(
        self, plaintext: str, *, tenant_id: uuid.UUID, secret_id: uuid.UUID, purpose: str
    ) -> bytes:
        nonce = secrets.token_bytes(NONCE_BYTES)
        ciphertext = AESGCM(self._keys[self.version]).encrypt(
            nonce, plaintext.encode("utf-8"), self._aad(tenant_id, secret_id, purpose)
        )
        return nonce + ciphertext

    def decrypt(
        self,
        blob: bytes,
        *,
        tenant_id: uuid.UUID,
        secret_id: uuid.UUID,
        purpose: str,
        key_version: int | None = None,
    ) -> str:
        key = self._keys.get(self.version if key_version is None else key_version)
        if key is None:
            logger.error("no key for ciphertext version", extra={"key_version": key_version})
            raise SecretUnavailableError()
        nonce, ciphertext = blob[:NONCE_BYTES], blob[NONCE_BYTES:]
        try:
            plaintext = AESGCM(key).decrypt(
                nonce, ciphertext, self._aad(tenant_id, secret_id, purpose)
            )
        except InvalidTag as exc:
            raise SecretUnavailableError() from exc
        return plaintext.decode("utf-8")


class SecretStore:
    """Tenant-scoped secret persistence and resolution."""

    def __init__(self, session: AsyncSession, cipher: SecretCipher) -> None:
        self._session = session
        self._cipher = cipher

    async def store(self, *, tenant_id: uuid.UUID, purpose: str, value: SecretStr) -> str:
        raw = value.get_secret_value()
        if not raw or len(raw) > MAX_SECRET_LENGTH:
            raise SecretReferenceError("The secret is empty or too long.")
        secret_id = uuid.uuid4()
        self._session.add(
            EncryptedSecret(
                id=secret_id,
                tenant_id=tenant_id,
                purpose=purpose,
                ciphertext=self._cipher.encrypt(
                    raw, tenant_id=tenant_id, secret_id=secret_id, purpose=purpose
                ),
                key_version=self._cipher.version,
            )
        )
        await self._session.flush()
        return f"local://{secret_id}"

    async def delete_if_local(self, *, tenant_id: uuid.UUID, ref: str | None) -> None:
        secret_id = _local_id(ref)
        if secret_id is not None:
            await self._session.execute(
                delete(EncryptedSecret).where(
                    EncryptedSecret.id == secret_id, EncryptedSecret.tenant_id == tenant_id
                )
            )

    def validate_external_ref(self, ref: str, *, tenant_slug: str) -> str:
        """Validate a caller-supplied reference (``local://`` refs are server-issued only)."""
        ref = ref.strip()
        if ref.startswith("vault://"):
            settings = self._cipher.settings
            if not settings.vault_addr:
                raise SecretReferenceError(
                    "Vault is not configured for this deployment.",
                    details={"reason": "SCHEME_NOT_SUPPORTED"},
                )
            try:
                parse_vault_ref(ref, settings=settings, tenant_slug=tenant_slug)
            except VaultReferenceError as exc:
                raise SecretReferenceError(
                    "Vault references must be vault://<mount>/"
                    f"{settings.vault_tenant_prefix.format(tenant=tenant_slug)}<name>#<field>.",
                    details={"reason": "VAULT_PATH_NOT_ALLOWED"},
                ) from exc
            return ref
        if ref.startswith(_RESERVED_SCHEMES):
            raise SecretReferenceError(
                "This secret manager integration is not enabled yet.",
                details={"reason": "SCHEME_NOT_SUPPORTED"},
            )
        if not ref.startswith("env://"):
            raise SecretReferenceError(details={"reason": "SCHEME_NOT_ALLOWED"})
        name = ref.removeprefix("env://")
        prefix = env_prefix_for(tenant_slug)
        if not _ENV_NAME.fullmatch(name) or not name.startswith(prefix) or len(name) == len(prefix):
            raise SecretReferenceError(
                f"Environment secrets must be named {prefix}<NAME>.",
                details={"reason": "ENV_NAME_NOT_ALLOWED"},
            )
        return ref

    async def resolve(
        self, *, tenant_id: uuid.UUID, tenant_slug: str, ref: str | None
    ) -> SecretStr | None:
        if not ref:
            return None
        local_id = _local_id(ref)
        if local_id is not None:
            row = (
                await self._session.execute(
                    select(EncryptedSecret).where(
                        EncryptedSecret.id == local_id, EncryptedSecret.tenant_id == tenant_id
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                raise SecretUnavailableError()
            return SecretStr(
                self._cipher.decrypt(
                    row.ciphertext,
                    tenant_id=tenant_id,
                    secret_id=row.id,
                    purpose=row.purpose,
                    key_version=row.key_version,
                )
            )
        self.validate_external_ref(ref, tenant_slug=tenant_slug)
        if ref.startswith("vault://"):
            settings = self._cipher.settings
            try:
                return await VaultClient(settings).read(
                    parse_vault_ref(ref, settings=settings, tenant_slug=tenant_slug)
                )
            except VaultUnavailableError as exc:
                raise SecretUnavailableError() from exc
        value = os.environ.get(ref.removeprefix("env://"))
        if value is None:
            raise SecretUnavailableError()
        return SecretStr(value)


def _local_id(ref: str | None) -> uuid.UUID | None:
    if not ref or not ref.startswith("local://"):
        return None
    try:
        return uuid.UUID(ref.removeprefix("local://"))
    except ValueError:
        return None


async def rotate_encrypted_secrets(session: AsyncSession, cipher: SecretCipher) -> int:
    """Re-encrypt every ``local://`` secret not yet on the current key version.

    Idempotent and restartable; commits per row so a failure leaves each secret
    readable (old or new version). Returns the number of rows re-encrypted.
    """
    rows = list(
        await session.scalars(
            select(EncryptedSecret).where(EncryptedSecret.key_version != cipher.version)
        )
    )
    for row in rows:
        plaintext = cipher.decrypt(
            row.ciphertext,
            tenant_id=row.tenant_id,
            secret_id=row.id,
            purpose=row.purpose,
            key_version=row.key_version,
        )
        row.ciphertext = cipher.encrypt(
            plaintext, tenant_id=row.tenant_id, secret_id=row.id, purpose=row.purpose
        )
        row.key_version = cipher.version
        await session.commit()
    return len(rows)
