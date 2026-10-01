"""Create a fresh application schema without Alembic (tests and the eval sandbox).

Production and development databases are always migrated with Alembic; this helper
exists for throwaway SQLite databases. The RBAC catalog is seeded from
``app.core.permissions`` exactly as migration ``0001`` seeds it (drift-tested).
"""

from __future__ import annotations

from app.config import Settings
from app.core.permissions import PERMISSION_DESCRIPTIONS, ROLE_DESCRIPTIONS, ROLE_PERMISSIONS
from app.db.models import Base
from app.db.models import Permission as PermissionModel
from app.db.models import Role as RoleModel
from app.db.session import Database


async def create_schema_with_rbac(settings: Settings) -> None:
    db = Database(settings)
    try:
        async with db.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with db.session_factory() as session:
            perms = {
                p: PermissionModel(id=i, name=p.value, description=d)
                for i, (p, d) in enumerate(PERMISSION_DESCRIPTIONS.items(), 1)
            }
            for i, (role, desc) in enumerate(ROLE_DESCRIPTIONS.items(), 1):
                model = RoleModel(id=i, name=role.value, description=desc)
                model.permissions = [perms[p] for p in ROLE_PERMISSIONS[role]]
                session.add(model)
            await session.commit()
    finally:
        await db.dispose()
