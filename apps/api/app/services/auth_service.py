"""Login, refresh-token rotation, logout.

Security properties (see tests/security/test_auth_security.py):

* Uniform ``INVALID_CREDENTIALS`` for unknown tenant, unknown user, wrong password,
  inactive or locked account; unknown users still pay the Argon2 cost (timing).
* Brute force: per-IP and per-account rate limits (Redis) plus a database lockout
  with exponential back-off that survives Redis outages.
* Sessions are refresh-token *families*. Each refresh rotates the token; the old
  one is revoked. Presenting a revoked token revokes the entire family (theft
  detection) and, because access tokens are bound to the family, ends the session
  immediately. Session lifetime is absolute (rotation does not extend it).
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core.exceptions import AuthenticationError
from app.core.logging import get_logger
from app.core.metrics import AUTH_EVENTS
from app.core.permissions import Role, permissions_for_roles
from app.core.rate_limit import RateLimiter
from app.core.security import (
    PasswordService,
    TokenService,
    as_utc,
    hash_refresh_token,
    new_refresh_token,
    utcnow,
)
from app.db.models import RefreshToken, Tenant, User, UserRole
from app.db.models import Role as RoleModel
from app.schemas.auth import MeResponse, TenantOut, TokenResponse
from app.services.audit_service import AuditOutcome, AuditService

logger = get_logger(__name__)


class InvalidCredentialsError(AuthenticationError):
    code = "INVALID_CREDENTIALS"
    default_message = "Invalid tenant, email or password."


class RefreshTokenInvalidError(AuthenticationError):
    code = "REFRESH_TOKEN_INVALID"
    default_message = "The session has expired. Please sign in again."


def _account_key(tenant_slug: str, email: str) -> str:
    return f"{tenant_slug}|{email}"


def _email_fingerprint(email: str) -> str:
    """Stable, non-reversible identifier for audit records about unknown accounts."""
    return hashlib.sha256(email.encode("utf-8")).hexdigest()[:16]


@dataclass(slots=True)
class AuthService:
    session: AsyncSession
    settings: Settings
    passwords: PasswordService
    tokens: TokenService
    rate_limiter: RateLimiter
    audit: AuditService

    # ----------------------------------------------------------------- login
    async def login(
        self, *, tenant_slug: str, email: str, password: str, client_ip: str
    ) -> TokenResponse:
        await self.rate_limiter.enforce(
            "login_ip", client_ip, limit=self.settings.login_rate_limit_per_ip_per_minute
        )
        await self.rate_limiter.enforce(
            "login_account",
            _account_key(tenant_slug, email),
            limit=self.settings.login_rate_limit_per_account_per_minute,
        )

        row = (
            await self.session.execute(
                select(User, Tenant)
                .join(Tenant, Tenant.id == User.tenant_id)
                .where(Tenant.slug == tenant_slug, User.email == email)
            )
        ).one_or_none()

        if row is None:
            self.passwords.burn(password)
            await self._deny("unknown_account", None, None, {"email_fp": _email_fingerprint(email)})
            raise InvalidCredentialsError()

        user, tenant = row
        now = utcnow()
        locked_until = as_utc(user.locked_until)
        if tenant.status != "active" or not user.is_active:
            self.passwords.burn(password)
            await self._deny("inactive", tenant.id, user.id)
            raise InvalidCredentialsError()
        if locked_until is not None and locked_until > now:
            self.passwords.burn(password)
            await self._deny("locked", tenant.id, user.id)
            raise InvalidCredentialsError()

        if not self.passwords.verify(user.password_hash, password):
            user.failed_login_count += 1
            locked = self._apply_lockout(user, now)
            await self.session.commit()
            await self._deny(
                "bad_password",
                tenant.id,
                user.id,
                {"failed_count": user.failed_login_count, "locked": locked},
            )
            raise InvalidCredentialsError()

        user.failed_login_count = 0
        user.locked_until = None
        user.last_login_at = now
        if user.password_hash and self.passwords.needs_rehash(user.password_hash):
            user.password_hash = self.passwords.hash(password)

        family_id = uuid.uuid4()
        refresh_plain, _ = await self._issue_refresh(
            user=user, family_id=family_id, expires_at=now + self.tokens.refresh_ttl
        )
        await self.session.commit()
        AUTH_EVENTS.labels(event="login", outcome="success").inc()
        await self.audit.record(
            "auth.login",
            AuditOutcome.SUCCESS,
            tenant_id=tenant.id,
            actor_user_id=user.id,
            resource_type="session",
            resource_id=str(family_id),
        )
        return await self._token_response(
            user, tenant, family_id, refresh_plain, now + self.tokens.refresh_ttl
        )

    def _apply_lockout(self, user: User, now: datetime) -> bool:
        threshold = self.settings.login_lockout_threshold
        if user.failed_login_count < threshold:
            return False
        exponent = min(user.failed_login_count - threshold, 16)
        seconds = min(
            self.settings.login_lockout_base_seconds * (2**exponent),
            self.settings.login_lockout_max_seconds,
        )
        user.locked_until = now + timedelta(seconds=seconds)
        return True

    async def _deny(
        self,
        reason: str,
        tenant_id: uuid.UUID | None,
        user_id: uuid.UUID | None,
        details: dict[str, object] | None = None,
    ) -> None:
        AUTH_EVENTS.labels(event="login", outcome=reason).inc()
        await self.audit.record(
            "auth.login",
            AuditOutcome.DENIED,
            tenant_id=tenant_id,
            actor_user_id=user_id,
            details={"reason": reason, **(details or {})},
        )

    # --------------------------------------------------------------- refresh
    async def refresh(self, refresh_token: str) -> TokenResponse:
        now = utcnow()
        token = (
            await self.session.execute(
                select(RefreshToken)
                .where(RefreshToken.token_hash == hash_refresh_token(refresh_token))
                .with_for_update()
            )
        ).scalar_one_or_none()
        if token is None:
            AUTH_EVENTS.labels(event="refresh", outcome="unknown").inc()
            raise RefreshTokenInvalidError()

        if token.revoked_at is not None:
            # Reuse of a rotated/revoked token: assume theft, kill the whole session.
            await self._revoke_family(token.family_id, now)
            await self.session.commit()
            AUTH_EVENTS.labels(event="refresh", outcome="reuse_detected").inc()
            await self.audit.record(
                "auth.refresh_reuse",
                AuditOutcome.DENIED,
                tenant_id=token.tenant_id,
                actor_user_id=token.user_id,
                resource_type="session",
                resource_id=str(token.family_id),
            )
            raise RefreshTokenInvalidError()

        expires_at = as_utc(token.expires_at)
        if expires_at is None or expires_at <= now:
            AUTH_EVENTS.labels(event="refresh", outcome="expired").inc()
            raise RefreshTokenInvalidError()

        row = (
            await self.session.execute(
                select(User, Tenant)
                .join(Tenant, Tenant.id == User.tenant_id)
                .where(User.id == token.user_id, User.tenant_id == token.tenant_id)
            )
        ).one_or_none()
        if row is None or not row[0].is_active or row[1].status != "active":
            await self._revoke_family(token.family_id, now)
            await self.session.commit()
            raise RefreshTokenInvalidError()
        user, tenant = row

        # Absolute session lifetime: the rotated token inherits the family expiry.
        new_plain, new_row = await self._issue_refresh(
            user=user, family_id=token.family_id, expires_at=expires_at
        )
        token.revoked_at = now
        token.replaced_by_id = new_row.id
        await self.session.commit()
        AUTH_EVENTS.labels(event="refresh", outcome="success").inc()
        return await self._token_response(user, tenant, token.family_id, new_plain, expires_at)

    # ---------------------------------------------------------------- logout
    async def logout(self, refresh_token: str) -> None:
        """Revoke the session. Always succeeds from the caller's view (no oracle)."""
        token = (
            await self.session.execute(
                select(RefreshToken).where(
                    RefreshToken.token_hash == hash_refresh_token(refresh_token)
                )
            )
        ).scalar_one_or_none()
        if token is None:
            return
        await self._revoke_family(token.family_id, utcnow())
        await self.session.commit()
        AUTH_EVENTS.labels(event="logout", outcome="success").inc()
        await self.audit.record(
            "auth.logout",
            AuditOutcome.SUCCESS,
            tenant_id=token.tenant_id,
            actor_user_id=token.user_id,
            resource_type="session",
            resource_id=str(token.family_id),
        )

    # --------------------------------------------------------------- helpers
    async def _issue_refresh(
        self, *, user: User, family_id: uuid.UUID, expires_at: datetime
    ) -> tuple[str, RefreshToken]:
        plain = new_refresh_token()
        row = RefreshToken(
            tenant_id=user.tenant_id,
            user_id=user.id,
            family_id=family_id,
            token_hash=hash_refresh_token(plain),
            expires_at=expires_at,
        )
        self.session.add(row)
        await self.session.flush()
        return plain, row

    async def _revoke_family(self, family_id: uuid.UUID, now: datetime) -> None:
        await self.session.execute(
            update(RefreshToken)
            .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=now)
        )

    async def _token_response(
        self,
        user: User,
        tenant: Tenant,
        family_id: uuid.UUID,
        refresh_plain: str,
        refresh_expires_at: datetime,
    ) -> TokenResponse:
        now = utcnow()
        access, access_expires = self.tokens.issue_access_token(
            user_id=user.id, tenant_id=tenant.id, session_id=family_id, now=now
        )
        return TokenResponse(
            access_token=access,
            expires_in=int((access_expires - now).total_seconds()),
            refresh_token=refresh_plain,
            refresh_expires_in=max(0, int((refresh_expires_at - now).total_seconds())),
            user=await build_me(self.session, user, tenant),
        )


async def user_roles(session: AsyncSession, user_id: uuid.UUID, tenant_id: uuid.UUID) -> list[str]:
    names = (
        await session.scalars(
            select(RoleModel.name)
            .join(UserRole, UserRole.role_id == RoleModel.id)
            .where(UserRole.user_id == user_id, UserRole.tenant_id == tenant_id)
            .order_by(RoleModel.id)
        )
    ).all()
    return list(names)


async def build_me(session: AsyncSession, user: User, tenant: Tenant) -> MeResponse:
    roles = await user_roles(session, user.id, tenant.id)
    known = {r.value for r in Role}
    perms = permissions_for_roles(Role(r) for r in roles if r in known)
    return MeResponse(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        tenant=TenantOut(id=tenant.id, slug=tenant.slug, name=tenant.name),
        roles=roles,
        permissions=sorted(p.value for p in perms),
    )
