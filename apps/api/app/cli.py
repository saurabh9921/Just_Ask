"""Operator CLI for bootstrapping tenants and users.

    python -m app.cli create-tenant --slug demo --name "Demo Co"
    python -m app.cli create-user --tenant demo --email admin@demo.test \
        --display-name "Demo Admin" --role tenant_admin
    python -m app.cli set-password --tenant demo --email admin@demo.test
    python -m app.cli rotate-secrets   # re-encrypt local:// secrets with the current key

The password is read from ``ERP_BOOTSTRAP_PASSWORD`` if set (automation), otherwise
prompted without echo. It is never accepted as a command-line argument (which
would leak into shell history and process listings).
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
import uuid

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.config import get_settings
from app.core.exceptions import AppError
from app.core.permissions import Role
from app.core.security import PasswordService
from app.db.models import Role as RoleModel
from app.db.models import Tenant, User, UserRole
from app.db.session import Database
from app.schemas.common import TenantSlug
from app.services.admin_service import validate_password
from app.services.audit_service import AuditOutcome, AuditService
from app.services.secrets_service import SecretCipher, rotate_encrypted_secrets


def _fail(message: str) -> None:
    sys.stderr.write(f"error: {message}\n")
    raise SystemExit(1)


async def create_tenant(slug: str, name: str) -> uuid.UUID:
    try:
        slug = TypeAdapter(TenantSlug).validate_python(slug)
    except ValidationError:
        _fail("slug must be 2-63 chars: lowercase letters, digits and '-'")
    settings = get_settings()
    db = Database(settings)
    try:
        async with db.session_factory() as session:
            tenant = Tenant(
                id=uuid.uuid4(), slug=slug, name=name.strip(), status="active", settings={}
            )
            session.add(tenant)
            try:
                await session.commit()
            except IntegrityError:
                _fail(f"tenant '{slug}' already exists")
            await AuditService(db.session_factory).record(
                "cli.tenant_create",
                AuditOutcome.SUCCESS,
                tenant_id=tenant.id,
                resource_type="tenant",
                resource_id=str(tenant.id),
            )
            return tenant.id
    finally:
        await db.dispose()


async def create_user(
    tenant_slug: str, email: str, display_name: str, roles: list[Role], password: str
) -> uuid.UUID:
    settings = get_settings()
    email = email.strip().lower()
    validate_password(password, email=email, min_length=settings.password_min_length)
    db = Database(settings)
    try:
        async with db.session_factory() as session:
            tenant = await session.scalar(select(Tenant).where(Tenant.slug == tenant_slug))
            if tenant is None:
                _fail(f"tenant '{tenant_slug}' not found")
            assert tenant is not None  # noqa: S101 - _fail exits
            user = User(
                id=uuid.uuid4(),
                tenant_id=tenant.id,
                email=email,
                display_name=display_name.strip(),
                password_hash=PasswordService(settings).hash(password),
                is_active=True,
                failed_login_count=0,
            )
            session.add(user)
            try:
                await session.flush()
            except IntegrityError:
                _fail("a user with this email already exists in the tenant")
            role_ids = dict(
                (await session.execute(select(RoleModel.name, RoleModel.id))).tuples().all()
            )
            for role in dict.fromkeys(roles):
                session.add(
                    UserRole(user_id=user.id, role_id=role_ids[role.value], tenant_id=tenant.id)
                )
            await session.commit()
            await AuditService(db.session_factory).record(
                "cli.user_create",
                AuditOutcome.SUCCESS,
                tenant_id=tenant.id,
                resource_type="user",
                resource_id=str(user.id),
                details={"roles": [r.value for r in roles]},
            )
            return user.id
    finally:
        await db.dispose()


async def set_password(tenant_slug: str, email: str, password: str) -> uuid.UUID:
    """Replace an existing user's password hash, clearing any login lockout."""
    settings = get_settings()
    email = email.strip().lower()
    validate_password(password, email=email, min_length=settings.password_min_length)
    db = Database(settings)
    try:
        async with db.session_factory() as session:
            tenant = await session.scalar(select(Tenant).where(Tenant.slug == tenant_slug))
            if tenant is None:
                _fail(f"tenant '{tenant_slug}' not found")
            assert tenant is not None  # noqa: S101 - _fail exits
            user = await session.scalar(
                select(User).where(User.tenant_id == tenant.id, User.email == email)
            )
            if user is None:
                _fail(f"user '{email}' not found in tenant '{tenant_slug}'")
            assert user is not None  # noqa: S101 - _fail exits
            user.password_hash = PasswordService(settings).hash(password)
            user.failed_login_count = 0
            user.locked_until = None
            await session.commit()
            await AuditService(db.session_factory).record(
                "cli.user_password_set",
                AuditOutcome.SUCCESS,
                tenant_id=tenant.id,
                resource_type="user",
                resource_id=str(user.id),
            )
            return user.id
    finally:
        await db.dispose()


def _read_password() -> str:
    from_env = os.environ.get("ERP_BOOTSTRAP_PASSWORD")
    if from_env:
        return from_env
    first = getpass.getpass("Password: ")
    if first != getpass.getpass("Repeat password: "):
        _fail("passwords do not match")
    return first


async def rotate_secrets() -> int:
    """Re-encrypt every stored secret with ENCRYPTION_KEY (version ENCRYPTION_KEY_VERSION).

    Run after deploying the new key with ENCRYPTION_KEY_PREVIOUS set; afterwards the
    previous key can be removed. Safe to re-run.
    """
    settings = get_settings()
    db = Database(settings)
    try:
        async with db.session_factory() as session:
            count = await rotate_encrypted_secrets(session, SecretCipher(settings))
        await AuditService(db.session_factory).record(
            "cli.secrets_rotated",
            AuditOutcome.SUCCESS,
            details={"count": count, "key_version": settings.encryption_key_version},
        )
        return count
    finally:
        await db.dispose()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    t = sub.add_parser("create-tenant", help="create a tenant")
    t.add_argument("--slug", required=True)
    t.add_argument("--name", required=True)

    u = sub.add_parser("create-user", help="create a user in a tenant")
    u.add_argument("--tenant", required=True)
    u.add_argument("--email", required=True)
    u.add_argument("--display-name", required=True)
    u.add_argument("--role", action="append", required=True, choices=[r.value for r in Role])

    p = sub.add_parser("set-password", help="reset an existing user's password")
    p.add_argument("--tenant", required=True)
    p.add_argument("--email", required=True)

    sub.add_parser("rotate-secrets", help="re-encrypt stored secrets with the current key")

    args = parser.parse_args(argv)
    try:
        _dispatch(args)
    except AppError as exc:
        _fail(exc.message)


def _dispatch(args: argparse.Namespace) -> None:
    if args.command == "create-tenant":
        tenant_id = asyncio.run(create_tenant(args.slug, args.name))
        sys.stdout.write(f"tenant created: {args.slug} ({tenant_id})\n")
    elif args.command == "rotate-secrets":
        count = asyncio.run(rotate_secrets())
        sys.stdout.write(f"secrets re-encrypted: {count}\n")
    elif args.command == "set-password":
        user_id = asyncio.run(set_password(args.tenant, args.email, _read_password()))
        sys.stdout.write(f"password updated: {args.email.strip().lower()} ({user_id})\n")
    else:
        password = _read_password()
        user_id = asyncio.run(
            create_user(
                args.tenant, args.email, args.display_name, [Role(r) for r in args.role], password
            )
        )
        sys.stdout.write(f"user created: {args.email.strip().lower()} ({user_id})\n")


if __name__ == "__main__":
    main()
