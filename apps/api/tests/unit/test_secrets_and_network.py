from __future__ import annotations

import uuid
from typing import cast

import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.connectors.common.errors import ConnectionConfigError
from app.connectors.common.network import HostPolicy
from app.connectors.common.type_mapping import normalize_type
from app.connectors.common.types import CanonicalType
from app.services.secrets_service import (
    SecretCipher,
    SecretReferenceError,
    SecretStore,
    SecretUnavailableError,
    env_prefix_for,
)

# ------------------------------------------------------------------- cipher


def test_cipher_roundtrip_and_ciphertext_hides_plaintext(settings: Settings) -> None:
    cipher = SecretCipher(settings)
    tenant, secret = uuid.uuid4(), uuid.uuid4()
    blob = cipher.encrypt("s3cr3t-pw", tenant_id=tenant, secret_id=secret, purpose="db_password")
    assert b"s3cr3t-pw" not in blob
    assert (
        cipher.decrypt(blob, tenant_id=tenant, secret_id=secret, purpose="db_password")
        == "s3cr3t-pw"
    )


@pytest.mark.parametrize("field", ["tenant_id", "secret_id", "purpose"])
def test_ciphertext_is_bound_to_tenant_row_and_purpose(settings: Settings, field: str) -> None:
    cipher = SecretCipher(settings)
    ctx: dict[str, object] = {
        "tenant_id": uuid.uuid4(),
        "secret_id": uuid.uuid4(),
        "purpose": "db_password",
    }
    blob = cipher.encrypt("pw", **ctx)  # type: ignore[arg-type]
    tampered = dict(ctx)
    tampered[field] = "tls_ca" if field == "purpose" else uuid.uuid4()
    with pytest.raises(SecretUnavailableError):
        cipher.decrypt(blob, **tampered)  # type: ignore[arg-type]


def test_different_master_keys_cannot_decrypt(settings: Settings) -> None:
    other = settings.model_copy(update={"encryption_key": SecretStr("another-key-" + "x" * 40)})
    tenant, secret = uuid.uuid4(), uuid.uuid4()
    blob = SecretCipher(settings).encrypt("pw", tenant_id=tenant, secret_id=secret, purpose="p")
    with pytest.raises(SecretUnavailableError):
        SecretCipher(other).decrypt(blob, tenant_id=tenant, secret_id=secret, purpose="p")


def store_for(settings: Settings) -> SecretStore:
    """Reference validation needs no database session."""
    return SecretStore(cast(AsyncSession, None), SecretCipher(settings))


# ----------------------------------------------------------------- env refs
def test_env_prefix_is_tenant_scoped() -> None:
    assert env_prefix_for("acme-corp") == "ERP_SECRET_ACME_CORP_"


@pytest.mark.parametrize(
    "ref",
    [
        "env://JWT_SECRET",
        "env://ENCRYPTION_KEY",
        "env://ERP_SECRET_OTHER_DB",  # another tenant's namespace
        "env://ERP_SECRET_ACME_",  # empty name
        "env://erp_secret_acme_db",  # lower-case
        "file:///etc/passwd",
        "local://" + str(uuid.uuid4()),  # server-issued only
        "https://attacker.example/secret",
    ],
)
def test_disallowed_external_refs(settings: Settings, ref: str) -> None:
    with pytest.raises(SecretReferenceError):
        store_for(settings).validate_external_ref(ref, tenant_slug="acme")


@pytest.mark.parametrize(
    "ref", ["vault://secret/erp-insight/acme/db#password", "aws-sm://erp", "azure-kv://x/y"]
)
def test_unconfigured_secret_managers_report_not_supported(settings: Settings, ref: str) -> None:
    with pytest.raises(SecretReferenceError) as info:
        store_for(settings).validate_external_ref(ref, tenant_slug="acme")
    assert info.value.details["reason"] == "SCHEME_NOT_SUPPORTED"


def test_valid_env_ref_accepted(settings: Settings) -> None:
    assert store_for(settings).validate_external_ref(
        " env://ERP_SECRET_ACME_ERP_PW ", tenant_slug="acme"
    ) == ("env://ERP_SECRET_ACME_ERP_PW")


# -------------------------------------------------------------- host policy
POLICY = HostPolicy(
    ["localhost", "redis", "127.0.0.0/8", "::1/128", "169.254.0.0/16", "0.0.0.0/8", "fe80::/10"]
)


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "LOCALHOST.",
        "redis",
        "127.0.0.1",
        "127.1.2.3",
        "169.254.169.254",
        "::1",
        "[::1]",
        "::ffff:127.0.0.1",
        "0.0.0.0",
    ],
)
def test_denied_hosts(host: str) -> None:
    with pytest.raises(ConnectionConfigError) as info:
        POLICY.check_syntax(host)
    assert info.value.details["reason"] == "HOST_NOT_ALLOWED"


@pytest.mark.parametrize("host", ["10.1.2.3", "192.168.1.20", "erp-db.internal.example", "db01"])
def test_private_and_named_hosts_allowed(host: str) -> None:
    POLICY.check_syntax(host)


@pytest.mark.parametrize("host", ["bad host", "host/path", "-leading", "a" * 300, "host:5432", ""])
def test_malformed_hosts_rejected(host: str) -> None:
    with pytest.raises(ConnectionConfigError):
        POLICY.check_syntax(host)


async def test_resolved_addresses_are_checked() -> None:
    # "localhost" is resolved even when not literally on the list.
    policy = HostPolicy(["127.0.0.0/8", "::1/128"])
    with pytest.raises(ConnectionConfigError):
        await policy.check("localhost", 5432)


async def test_unresolvable_hosts_pass_policy() -> None:
    await HostPolicy(["127.0.0.0/8"]).check("does-not-exist.invalid", 5432)


# ------------------------------------------------------------- type mapping
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("VARCHAR(20)", CanonicalType.STRING),
        ("character varying", CanonicalType.STRING),
        ("TEXT", CanonicalType.STRING),
        ("NUMERIC(12, 2)", CanonicalType.DECIMAL),
        ("BIGINT", CanonicalType.INTEGER),
        ("INTEGER", CanonicalType.INTEGER),
        ("double precision", CanonicalType.FLOAT),
        ("timestamp with time zone", CanonicalType.TIMESTAMP),
        ("DATETIME2", CanonicalType.TIMESTAMP),
        ("DATE", CanonicalType.DATE),
        ("time without time zone", CanonicalType.TIME),
        ("jsonb", CanonicalType.JSON),
        ("uuid", CanonicalType.UUID),
        ("uniqueidentifier", CanonicalType.UUID),
        ("BOOLEAN", CanonicalType.BOOLEAN),
        ("bytea", CanonicalType.BINARY),
        ("interval", CanonicalType.OTHER),
        ("geometry", CanonicalType.OTHER),
    ],
)
def test_normalize_type(raw: str, expected: CanonicalType) -> None:
    assert normalize_type(raw) is expected
