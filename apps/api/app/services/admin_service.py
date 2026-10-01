"""Tenant user administration (``admin.manage``).

Privilege-escalation guards:
* an administrator can only grant roles at or below their own highest role;
* ``platform_admin`` can only be granted by a platform admin;
* users ranked above the actor cannot be modified;
* administrators cannot deactivate themselves or remove their own ``admin.manage``.
Deactivation and role changes take effect on the next request (principals are
re-loaded per request).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Final

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.core.exceptions import BadRequestError, ConflictError, NotFoundError, PermissionDeniedError
from app.core.permissions import Permission, Role, permissions_for_roles
from app.core.security import PasswordService, as_utc, utcnow
from app.core.tenant_context import TenantContext
from app.db.models import Role as RoleModel
from app.db.models import User, UserRole
from app.schemas.admin import UserCreate, UserOut, UserUpdate
from app.services.audit_service import AuditOutcome, AuditService
from app.services.auth_service import user_roles

ROLE_RANK: Final[dict[Role, int]] = {
    Role.VIEWER: 1,
    Role.ANALYST: 2,
    Role.DATA_ADMIN: 3,
    Role.TENANT_ADMIN: 4,
    Role.PLATFORM_ADMIN: 5,
}


def _rank(roles: frozenset[Role] | list[Role]) -> int:
    return max((ROLE_RANK[r] for r in roles), default=0)


def validate_password(password: str, *, email: str, min_length: int) -> None:
    if len(password) < min_length:
        raise BadRequestError(
            f"Password must be at least {min_length} characters.",
            details={"reason": "PASSWORD_TOO_SHORT"},
        )
    local = email.split("@", maxsplit=1)[0].lower()
    if password.strip().lower() == email.lower() or (len(local) >= 4 and local in password.lower()):
        raise BadRequestError(
            "Password must not contain the email address.", details={"reason": "PASSWORD_WEAK"}
        )


@dataclass(slots=True)
class AdminService:
    session: AsyncSession
    settings: Settings
    ctx: TenantContext
    passwords: PasswordService
    audit: AuditService

    def _check_assignable(self, roles: list[Role]) -> None:
        if Role.PLATFORM_ADMIN in roles and not self.ctx.is_platform_admin:
            raise PermissionDeniedError(details={"reason": "CANNOT_GRANT_ROLE"})
        if _rank(roles) > _rank(self.ctx.roles):
            raise PermissionDeniedError(details={"reason": "CANNOT_GRANT_ROLE"})

    async def _to_out(self, user: User) -> UserOut:
        locked_until = as_utc(user.locked_until)
        return UserOut(
            id=user.id,
            email=user.email,
            display_name=user.display_name,
            is_active=user.is_active,
            roles=await user_roles(self.session, user.id, self.ctx.tenant_id),
            last_login_at=user.last_login_at,
            locked=locked_until is not None and locked_until > utcnow(),
            created_at=user.created_at,
        )

    async def _get(self, user_id: uuid.UUID) -> User:
        user = await self.session.scalar(
            select(User).where(User.id == user_id, User.tenant_id == self.ctx.tenant_id)
        )
        if user is None:
            raise NotFoundError()
        return user

    async def _set_roles(self, user: User, roles: list[Role]) -> None:
        rows = await self.session.execute(
            select(RoleModel.name, RoleModel.id).where(RoleModel.name.in_([r.value for r in roles]))
        )
        ids: dict[str, int] = {str(n): int(i) for n, i in rows}
        await self.session.execute(delete(UserRole).where(UserRole.user_id == user.id))
        for role in dict.fromkeys(roles):
            self.session.add(
                UserRole(user_id=user.id, role_id=ids[role.value], tenant_id=user.tenant_id)
            )

    async def list_users(self, *, limit: int, offset: int) -> tuple[list[UserOut], int]:
        self.ctx.require(Permission.ADMIN_MANAGE)
        total = await self.session.scalar(
            select(func.count()).select_from(User).where(User.tenant_id == self.ctx.tenant_id)
        )
        users = await self.session.scalars(
            select(User)
            .where(User.tenant_id == self.ctx.tenant_id)
            .order_by(User.email)
            .limit(limit)
            .offset(offset)
        )
        return [await self._to_out(u) for u in users], int(total or 0)

    async def create_user(self, body: UserCreate) -> UserOut:
        self.ctx.require(Permission.ADMIN_MANAGE)
        self._check_assignable(body.roles)
        password = body.password.get_secret_value()
        validate_password(password, email=body.email, min_length=self.settings.password_min_length)
        user = User(
            id=uuid.uuid4(),
            tenant_id=self.ctx.tenant_id,
            email=body.email,
            display_name=body.display_name,
            password_hash=self.passwords.hash(password),
            is_active=True,
            failed_login_count=0,
        )
        self.session.add(user)
        try:
            await self.session.flush()
        except IntegrityError as exc:
            await self.session.rollback()
            raise ConflictError("A user with this email already exists.") from exc
        await self._set_roles(user, body.roles)
        await self.session.commit()
        await self._audit("admin.user_create", user, {"roles": [r.value for r in body.roles]})
        return await self._to_out(user)

    async def update_user(self, user_id: uuid.UUID, body: UserUpdate) -> UserOut:
        self.ctx.require(Permission.ADMIN_MANAGE)
        user = await self._get(user_id)
        current = [Role(r) for r in await user_roles(self.session, user.id, self.ctx.tenant_id)]
        if _rank(current) > _rank(self.ctx.roles):
            raise PermissionDeniedError(details={"reason": "TARGET_OUTRANKS_ACTOR"})
        is_self = user.id == self.ctx.user_id
        if body.is_active is False and is_self:
            raise BadRequestError("You cannot deactivate your own account.")
        if body.roles is not None:
            self._check_assignable(body.roles)
            if is_self and Permission.ADMIN_MANAGE not in permissions_for_roles(body.roles):
                raise BadRequestError("You cannot remove your own administration rights.")
            await self._set_roles(user, body.roles)
        if body.display_name is not None:
            user.display_name = body.display_name
        if body.is_active is not None:
            user.is_active = body.is_active
            if body.is_active:
                user.failed_login_count = 0
                user.locked_until = None
        await self.session.commit()
        await self._audit(
            "admin.user_update",
            user,
            {"fields": sorted(body.model_dump(exclude_unset=True).keys())},
        )
        return await self._to_out(user)

    async def _audit(self, event: str, user: User, details: dict[str, object]) -> None:
        await self.audit.record(
            event,
            AuditOutcome.SUCCESS,
            tenant_id=self.ctx.tenant_id,
            actor_user_id=self.ctx.user_id,
            resource_type="user",
            resource_id=str(user.id),
            details=details,
        )
