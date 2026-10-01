"""Password hashing, access tokens (JWT) and refresh tokens.

* Passwords: Argon2id (``argon2-cffi``). Hashes are rehashed transparently when the
  configured cost parameters change.
* Access tokens: short-lived JWTs (HS256/384/512 only; ``none`` and asymmetric
  algorithms are rejected at config load). Required claims are enforced on decode.
* Refresh tokens: 256-bit random opaque strings. Only their SHA-256 digest is stored.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.config import Settings
from app.core.exceptions import AuthenticationError

MAX_PASSWORD_LENGTH: Final = 128  # bound Argon2 input to avoid CPU/memory DoS
REFRESH_TOKEN_BYTES: Final = 32


class TokenExpiredError(AuthenticationError):
    code = "TOKEN_EXPIRED"
    default_message = "The access token has expired."


class InvalidTokenError(AuthenticationError):
    code = "UNAUTHENTICATED"
    default_message = "Authentication is required."


class PasswordService:
    def __init__(self, settings: Settings) -> None:
        self._hasher = PasswordHasher(
            time_cost=settings.password_hash_time_cost,
            memory_cost=settings.password_hash_memory_kib,
            parallelism=settings.password_hash_parallelism,
        )
        # Verified when a login targets an unknown user, so response timing does
        # not reveal whether the account exists.
        self._dummy_hash = self._hasher.hash(secrets.token_urlsafe(16))

    def hash(self, password: str) -> str:
        if len(password) > MAX_PASSWORD_LENGTH:
            raise ValueError("password too long")
        return self._hasher.hash(password)

    def verify(self, password_hash: str | None, password: str) -> bool:
        if len(password) > MAX_PASSWORD_LENGTH:
            return False
        try:
            return self._hasher.verify(password_hash or self._dummy_hash, password) and (
                password_hash is not None
            )
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False

    def burn(self, password: str) -> None:
        """Spend the same time as a real verification (unknown user path)."""
        self.verify(None, password[:MAX_PASSWORD_LENGTH])

    def needs_rehash(self, password_hash: str) -> bool:
        return self._hasher.check_needs_rehash(password_hash)


@dataclass(frozen=True, slots=True)
class AccessTokenClaims:
    user_id: uuid.UUID
    tenant_id: uuid.UUID
    session_id: uuid.UUID  # refresh-token family
    token_id: str
    expires_at: datetime


class TokenService:
    def __init__(self, settings: Settings) -> None:
        self._secret = settings.jwt_secret.get_secret_value()
        self._algorithm = settings.jwt_algorithm
        self._issuer = settings.jwt_issuer
        self._audience = settings.jwt_audience
        self.access_ttl = timedelta(seconds=settings.jwt_access_token_ttl_seconds)
        self.refresh_ttl = timedelta(seconds=settings.jwt_refresh_token_ttl_seconds)

    def issue_access_token(
        self,
        *,
        user_id: uuid.UUID,
        tenant_id: uuid.UUID,
        session_id: uuid.UUID,
        now: datetime | None = None,
    ) -> tuple[str, datetime]:
        issued = now or datetime.now(tz=UTC)
        expires = issued + self.access_ttl
        claims: dict[str, Any] = {
            "iss": self._issuer,
            "aud": self._audience,
            "sub": str(user_id),
            "tid": str(tenant_id),
            "sid": str(session_id),
            "jti": uuid.uuid4().hex,
            "iat": int(issued.timestamp()),
            "nbf": int(issued.timestamp()),
            "exp": int(expires.timestamp()),
            "typ": "access",
        }
        return jwt.encode(claims, self._secret, algorithm=self._algorithm), expires

    def decode_access_token(self, token: str) -> AccessTokenClaims:
        try:
            claims = jwt.decode(
                token,
                self._secret,
                algorithms=[self._algorithm],
                audience=self._audience,
                issuer=self._issuer,
                options={"require": ["exp", "iat", "nbf", "sub", "iss", "aud", "jti"]},
                leeway=5,
            )
        except jwt.ExpiredSignatureError as exc:
            raise TokenExpiredError() from exc
        except jwt.PyJWTError as exc:
            raise InvalidTokenError() from exc
        if claims.get("typ") != "access":
            raise InvalidTokenError()
        try:
            return AccessTokenClaims(
                user_id=uuid.UUID(str(claims["sub"])),
                tenant_id=uuid.UUID(str(claims["tid"])),
                session_id=uuid.UUID(str(claims["sid"])),
                token_id=str(claims["jti"]),
                expires_at=datetime.fromtimestamp(int(claims["exp"]), tz=UTC),
            )
        except (KeyError, ValueError) as exc:
            raise InvalidTokenError() from exc


def new_refresh_token() -> str:
    return secrets.token_urlsafe(REFRESH_TOKEN_BYTES)


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def utcnow() -> datetime:
    return datetime.now(tz=UTC)


def as_utc(value: datetime | None) -> datetime | None:
    """Normalise DB datetimes: SQLite returns naive values even for tz-aware columns."""
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
