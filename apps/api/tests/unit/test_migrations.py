"""Migration tests on SQLite (fast, no Docker).

PostgreSQL-specific behaviour (JSONB, append-only audit trigger) is covered by
tests/integration/test_postgres_migrations.py.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text

from app.core.permissions import PERMISSION_DESCRIPTIONS, ROLE_DESCRIPTIONS, ROLE_PERMISSIONS
from app.db.models import Base

API_ROOT = Path(__file__).resolve().parents[2]
VERSIONS = API_ROOT / "app" / "db" / "migrations" / "versions"


def alembic_config(async_url: str) -> Config:
    cfg = Config(str(API_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", "app:db/migrations")
    cfg.set_main_option("sqlalchemy.url", async_url)
    return cfg


def _load_initial_migration() -> ModuleType:
    path = next(VERSIONS.glob("*_0001_*.py"))
    spec = importlib.util.spec_from_file_location("initial_migration", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def db_file(tmp_path: Path) -> Path:
    return tmp_path / "migrate.db"


def test_upgrade_head_matches_models_exactly(db_file: Path) -> None:
    command.upgrade(alembic_config(f"sqlite+aiosqlite:///{db_file.as_posix()}"), "head")

    engine = create_engine(f"sqlite:///{db_file.as_posix()}")
    with engine.connect() as conn:
        diff = compare_metadata(
            MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata
        )
    engine.dispose()
    assert diff == [], f"models and migrations have drifted: {diff}"


def test_seeded_rbac_matches_application_catalog(db_file: Path) -> None:
    command.upgrade(alembic_config(f"sqlite+aiosqlite:///{db_file.as_posix()}"), "head")
    engine = create_engine(f"sqlite:///{db_file.as_posix()}")
    with engine.connect() as conn:
        roles: dict[str, str] = {
            r.name: r.description for r in conn.execute(text("SELECT name, description FROM roles"))
        }
        perms: dict[str, str] = {
            p.name: p.description
            for p in conn.execute(text("SELECT name, description FROM permissions"))
        }
        mapping_rows = conn.execute(
            text(
                "SELECT r.name, p.name FROM role_permissions rp "
                "JOIN roles r ON r.id = rp.role_id JOIN permissions p ON p.id = rp.permission_id"
            )
        ).all()
    engine.dispose()

    assert roles == {r.value: d for r, d in ROLE_DESCRIPTIONS.items()}
    assert perms == {p.value: d for p, d in PERMISSION_DESCRIPTIONS.items()}
    seeded: dict[str, set[str]] = {}
    for role, perm in mapping_rows:
        seeded.setdefault(role, set()).add(perm)
    assert seeded == {r.value: {p.value for p in ps} for r, ps in ROLE_PERMISSIONS.items()}


def test_migration_constants_match_catalog_without_database() -> None:
    module = _load_initial_migration()
    assert {name: set(perms) for name, perms in module.ROLE_PERMISSIONS.items()} == {
        r.value: {p.value for p in ps} for r, ps in ROLE_PERMISSIONS.items()
    }


def test_downgrade_to_base_removes_all_tables(db_file: Path) -> None:
    cfg = alembic_config(f"sqlite+aiosqlite:///{db_file.as_posix()}")
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    engine = create_engine(f"sqlite:///{db_file.as_posix()}")
    tables = set(inspect(engine).get_table_names()) - {"alembic_version"}
    engine.dispose()
    assert tables == set()


def test_single_linear_history() -> None:
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(alembic_config("sqlite+aiosqlite://"))
    assert len(script.get_heads()) == 1
