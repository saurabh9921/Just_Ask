from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fakeredis import FakeAsyncRedis
from redis.exceptions import ConnectionError as RedisConnectionError

from app.config import Settings
from app.core.exceptions import RateLimitedError
from app.core.rate_limit import RateLimiter
from app.core.security import (
    MAX_PASSWORD_LENGTH,
    InvalidTokenError,
    PasswordService,
    TokenExpiredError,
    TokenService,
    as_utc,
    hash_refresh_token,
    new_refresh_token,
)


# ------------------------------------------------------------------ passwords
def test_password_hash_roundtrip_uses_argon2id(settings: Settings) -> None:
    svc = PasswordService(settings)
    hashed = svc.hash("a-very-long-passphrase")
    assert hashed.startswith("$argon2id$")
    assert svc.verify(hashed, "a-very-long-passphrase")
    assert not svc.verify(hashed, "wrong")


def test_verify_without_hash_is_false_and_does_not_raise(settings: Settings) -> None:
    svc = PasswordService(settings)
    assert svc.verify(None, "anything") is False
    svc.burn("anything")


def test_overlong_passwords_rejected(settings: Settings) -> None:
    svc = PasswordService(settings)
    with pytest.raises(ValueError, match="too long"):
        svc.hash("x" * (MAX_PASSWORD_LENGTH + 1))
    assert svc.verify(svc.hash("ok-password"), "x" * 10_000) is False


def test_malformed_hash_is_rejected_safely(settings: Settings) -> None:
    assert PasswordService(settings).verify("not-a-hash", "pw") is False


# --------------------------------------------------------------------- tokens
def _ids() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    return uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


def test_access_token_roundtrip(settings: Settings) -> None:
    svc = TokenService(settings)
    user, tenant, session = _ids()
    token, expires = svc.issue_access_token(user_id=user, tenant_id=tenant, session_id=session)
    claims = svc.decode_access_token(token)
    assert (claims.user_id, claims.tenant_id, claims.session_id) == (user, tenant, session)
    assert claims.expires_at == expires.replace(microsecond=0)


def test_expired_token_raises_token_expired(settings: Settings) -> None:
    svc = TokenService(settings)
    user, tenant, session = _ids()
    past = datetime.now(tz=UTC) - timedelta(hours=1)
    token, _ = svc.issue_access_token(user_id=user, tenant_id=tenant, session_id=session, now=past)
    with pytest.raises(TokenExpiredError) as info:
        svc.decode_access_token(token)
    assert info.value.code == "TOKEN_EXPIRED"


def _forge(settings: Settings, **overrides: object) -> str:
    now = int(datetime.now(tz=UTC).timestamp())
    claims: dict[str, object] = {
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
        "sub": str(uuid.uuid4()),
        "tid": str(uuid.uuid4()),
        "sid": str(uuid.uuid4()),
        "jti": "x",
        "iat": now,
        "nbf": now,
        "exp": now + 60,
        "typ": "access",
    }
    claims.update(overrides)
    return jwt.encode(claims, settings.jwt_secret.get_secret_value(), algorithm="HS256")


@pytest.mark.parametrize(
    "overrides",
    [
        {"aud": "someone-else"},
        {"iss": "evil"},
        {"typ": "refresh"},
        {"sub": "not-a-uuid"},
    ],
)
def test_invalid_claims_rejected(settings: Settings, overrides: dict[str, object]) -> None:
    with pytest.raises(InvalidTokenError):
        TokenService(settings).decode_access_token(_forge(settings, **overrides))


def test_wrong_signature_rejected(settings: Settings) -> None:
    token = _forge(settings)
    tampered = token[:-2] + ("AA" if not token.endswith("AA") else "BB")
    with pytest.raises(InvalidTokenError):
        TokenService(settings).decode_access_token(tampered)


def test_alg_none_rejected(settings: Settings) -> None:
    token = jwt.encode({"sub": "x"}, key="", algorithm="none")
    with pytest.raises(InvalidTokenError):
        TokenService(settings).decode_access_token(token)


def test_refresh_tokens_are_random_and_hashed() -> None:
    tokens = {new_refresh_token() for _ in range(100)}
    assert len(tokens) == 100
    digest = hash_refresh_token("abc")
    assert len(digest) == 64 and digest != "abc"


def test_as_utc_normalises_naive_values() -> None:
    naive = datetime(2026, 1, 1, 12, 0)  # noqa: DTZ001 - simulating SQLite output
    assert as_utc(naive) == datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    assert as_utc(None) is None


# ---------------------------------------------------------------- rate limits
async def test_rate_limiter_allows_up_to_limit_then_rejects() -> None:
    limiter = RateLimiter(FakeAsyncRedis())
    for _ in range(3):
        await limiter.enforce("login_ip", "10.0.0.1", limit=3)
    with pytest.raises(RateLimitedError) as info:
        await limiter.enforce("login_ip", "10.0.0.1", limit=3)
    assert 1 <= info.value.details["retry_after_seconds"] <= 60
    # other identities are independent
    await limiter.enforce("login_ip", "10.0.0.2", limit=3)


async def test_rate_limiter_keys_do_not_contain_identity() -> None:
    redis = FakeAsyncRedis()
    await RateLimiter(redis).hit("login_account", "acme|ceo@acme.test", limit=5)
    keys = [k.decode() for k in await redis.keys("*")]
    assert keys and all("ceo@acme.test" not in k for k in keys)
    ttls = [await redis.ttl(k) for k in keys]
    assert all(t > 0 for t in ttls)


async def test_rate_limiter_fails_open_when_redis_down() -> None:
    class Broken(FakeAsyncRedis):
        def pipeline(self, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
            raise RedisConnectionError("down")

    decision = await RateLimiter(Broken()).hit("login_ip", "1.2.3.4", limit=1)
    assert decision.allowed
