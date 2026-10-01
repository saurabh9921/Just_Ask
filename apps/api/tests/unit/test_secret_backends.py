"""Phase 7: encryption-key rotation and HashiCorp Vault secret references."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select

from app.config import Settings
from app.db.bootstrap import create_schema_with_rbac
from app.db.models import EncryptedSecret, Tenant
from app.db.session import Database
from app.services.secrets_service import (
    SecretCipher,
    SecretReferenceError,
    SecretStore,
    SecretUnavailableError,
    rotate_encrypted_secrets,
)
from app.services.vault_client import (
    VaultClient,
    VaultReferenceError,
    VaultUnavailableError,
    parse_vault_ref,
)

OLD_KEY = "old-encryption-key-" + "o" * 40
NEW_KEY = "new-encryption-key-" + "n" * 40


def production_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "app_env": "production",
        "jwt_secret": "j" * 48,
        "encryption_key": NEW_KEY,
        "cors_allowed_origins": ["chrome-extension://abc"],
        "llm_provider": "vllm",
        "embedding_provider": "openai_compatible",
        **overrides,
    }
    return Settings(_env_file=None, **values)


# ------------------------------------------------------------ key rotation
def test_cipher_reads_previous_version_and_writes_current(settings: Settings) -> None:
    v1 = SecretCipher(settings.model_copy(update={"encryption_key": SecretStr(OLD_KEY)}))
    ids = {"tenant_id": uuid.uuid4(), "secret_id": uuid.uuid4(), "purpose": "db_password"}
    blob = v1.encrypt("pw", **ids)  # type: ignore[arg-type]
    v2 = SecretCipher(
        settings.model_copy(
            update={
                "encryption_key": SecretStr(NEW_KEY),
                "encryption_key_version": 2,
                "encryption_key_previous": SecretStr(OLD_KEY),
            }
        )
    )
    assert v2.decrypt(blob, key_version=1, **ids) == "pw"  # type: ignore[arg-type]
    with pytest.raises(SecretUnavailableError):  # current key cannot read v1 data
        v2.decrypt(blob, key_version=2, **ids)  # type: ignore[arg-type]
    with pytest.raises(SecretUnavailableError):  # unknown version
        v2.decrypt(blob, key_version=7, **ids)  # type: ignore[arg-type]


async def test_rotation_reencrypts_every_secret_and_is_idempotent(settings: Settings) -> None:
    await create_schema_with_rbac(settings)
    db = Database(settings)
    old = settings.model_copy(update={"encryption_key": SecretStr(OLD_KEY)})
    new = settings.model_copy(
        update={
            "encryption_key": SecretStr(NEW_KEY),
            "encryption_key_version": 2,
            "encryption_key_previous": SecretStr(OLD_KEY),
        }
    )
    try:
        async with db.session_factory() as session:
            tenant = Tenant(id=uuid.uuid4(), slug="acme", name="Acme", status="active", settings={})
            session.add(tenant)
            await session.flush()
            store = SecretStore(session, SecretCipher(old))
            refs = [
                await store.store(tenant_id=tenant.id, purpose="db_password", value=SecretStr(v))
                for v in ("alpha", "beta")
            ]
            await session.commit()
            before = {r.id: r.ciphertext for r in await session.scalars(select(EncryptedSecret))}

            assert await rotate_encrypted_secrets(session, SecretCipher(new)) == 2
            assert await rotate_encrypted_secrets(session, SecretCipher(new)) == 0

            rows = list(await session.scalars(select(EncryptedSecret)))
            assert {r.key_version for r in rows} == {2}
            assert all(before[r.id] != r.ciphertext for r in rows)
            # After rotation the previous key is no longer needed.
            only_new = SecretStore(
                session,
                SecretCipher(new.model_copy(update={"encryption_key_previous": None})),
            )
            values = [
                (await only_new.resolve(tenant_id=tenant.id, tenant_slug="acme", ref=r))
                for r in refs
            ]
            assert [v.get_secret_value() for v in values if v] == ["alpha", "beta"]
    finally:
        await db.dispose()


def test_production_rejects_reusing_the_previous_key() -> None:
    with pytest.raises(ValueError, match="ENCRYPTION_KEY_PREVIOUS"):
        production_settings(encryption_key_version=2, encryption_key_previous=NEW_KEY)


# ------------------------------------------------------------------- vault
@pytest.fixture
def vault_settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "vault_addr": "https://vault.example:8200",
            "vault_token": SecretStr("s.token"),
            "vault_namespace": "erp",
        }
    )


def fake_vault(
    data: dict[str, dict[str, Any]], seen: list[httpx.Request], status: int = 200
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path.removeprefix("/v1/secret/data/")
        if status != 200 or path not in data:
            return httpx.Response(status if status != 200 else 404, json={"errors": []})
        return httpx.Response(200, json={"data": {"data": data[path], "metadata": {}}})

    return httpx.MockTransport(handler)


@pytest.mark.parametrize(
    "ref",
    [
        "vault://secret/erp-insight/globex/db#password",  # another tenant
        "vault://secret/erp-insight/acme#password",  # the prefix itself
        "vault://secret/platform/jwt#value",  # outside tenant space
        "vault://secret/erp-insight/acme/../globex/db#password",
        "vault://secret/erp-insight/acme/db",  # no field
        "vault://Secret/erp-insight/acme/db#password",  # bad mount
        "vault://secret/erp-insight/acme/.hidden#password",
        "vault://secret/erp-insight/acme/db#pass word",
    ],
)
def test_vault_refs_are_tenant_scoped(vault_settings: Settings, ref: str) -> None:
    with pytest.raises(VaultReferenceError):
        parse_vault_ref(ref, settings=vault_settings, tenant_slug="acme")
    store = SecretStore(None, SecretCipher(vault_settings))  # type: ignore[arg-type]
    with pytest.raises(SecretReferenceError) as info:
        store.validate_external_ref(ref, tenant_slug="acme")
    assert info.value.details["reason"] == "VAULT_PATH_NOT_ALLOWED"


def test_prefix_of_a_similar_tenant_does_not_match(vault_settings: Settings) -> None:
    with pytest.raises(VaultReferenceError):
        parse_vault_ref(
            "vault://secret/erp-insight/acme2/db#password",
            settings=vault_settings,
            tenant_slug="acme",
        )


async def test_vault_client_reads_kv2_with_token_and_namespace(vault_settings: Settings) -> None:
    seen: list[httpx.Request] = []
    client = VaultClient(
        vault_settings, transport=fake_vault({"erp-insight/acme/db": {"password": "pw1"}}, seen)
    )
    ref = parse_vault_ref(
        "vault://secret/erp-insight/acme/db#password", settings=vault_settings, tenant_slug="acme"
    )
    assert (await client.read(ref)).get_secret_value() == "pw1"
    request = seen[0]
    assert str(request.url) == "https://vault.example:8200/v1/secret/data/erp-insight/acme/db"
    assert request.headers["X-Vault-Token"] == "s.token"
    assert request.headers["X-Vault-Namespace"] == "erp"


@pytest.mark.parametrize(
    ("data", "status"),
    [
        ({}, 200),  # path missing -> 404
        ({"erp-insight/acme/db": {"user": "x"}}, 200),  # field missing
        ({"erp-insight/acme/db": {"password": 42}}, 200),  # not a string
        ({"erp-insight/acme/db": {"password": "pw"}}, 403),  # token lacks policy
    ],
)
async def test_vault_failures_are_unavailable_not_leaked(
    vault_settings: Settings, data: dict[str, dict[str, Any]], status: int
) -> None:
    client = VaultClient(vault_settings, transport=fake_vault(data, [], status))
    ref = parse_vault_ref(
        "vault://secret/erp-insight/acme/db#password", settings=vault_settings, tenant_slug="acme"
    )
    with pytest.raises(VaultUnavailableError):
        await client.read(ref)


async def test_token_file_wins_over_token(vault_settings: Settings, tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("s.from-agent\n", encoding="utf-8")
    seen: list[httpx.Request] = []
    client = VaultClient(
        vault_settings.model_copy(update={"vault_token_file": str(token_file)}),
        transport=fake_vault({"erp-insight/acme/db": {"password": "pw"}}, seen),
    )
    await client.read(
        parse_vault_ref(
            "vault://secret/erp-insight/acme/db#password",
            settings=vault_settings,
            tenant_slug="acme",
        )
    )
    assert seen[0].headers["X-Vault-Token"] == "s.from-agent"


def test_production_requires_https_vault() -> None:
    with pytest.raises(ValueError, match="VAULT_ADDR"):
        production_settings(vault_addr="http://vault:8200")


def test_tenant_prefix_must_contain_placeholder() -> None:
    with pytest.raises(ValueError, match="VAULT_TENANT_PREFIX"):
        Settings(_env_file=None, vault_tenant_prefix="erp-insight/shared/")


def test_vault_secret_source_is_reported(settings: Settings) -> None:
    from app.db.models import DatabaseConnection
    from app.services.connection_service import ConnectionService

    conn = DatabaseConnection(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        name="x",
        connector_type="postgresql",
        secret_ref="vault://secret/erp-insight/acme/db#password",
        read_only=True,
        tls_mode="verify-full",
        pool_min_size=1,
        pool_max_size=5,
        query_timeout_seconds=30,
        max_rows=1000,
        status="untested",
        created_at=datetime.now(tz=UTC),
        updated_at=datetime.now(tz=UTC),
    )
    out = ConnectionService.to_out(conn, [])
    assert out.secret_source == "vault" and out.has_secret
    assert "vault://" not in json.dumps(out.model_dump(mode="json"))  # the ref is never echoed


async def test_rotate_secrets_cli_audits_the_rotation(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app import cli
    from app.db.models import AuditEvent

    await create_schema_with_rbac(settings)
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    assert await cli.rotate_secrets() == 0
    db = Database(settings)
    try:
        async with db.session_factory() as session:
            event = await session.scalar(
                select(AuditEvent).where(AuditEvent.event_type == "cli.secrets_rotated")
            )
        assert event is not None and event.details == {"count": 0, "key_version": 1}
    finally:
        await db.dispose()
