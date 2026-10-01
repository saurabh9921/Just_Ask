"""The shipped production configuration is complete, safe and demo-free."""

from __future__ import annotations

import importlib.util
import re
import secrets
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.config import Settings
from app.semantic.loader import load_semantic_layer

ROOT = Path(__file__).resolve().parents[4]
PROD_TEMPLATE = ROOT / ".env.production.example"
COMPOSE = ROOT / "infrastructure" / "docker" / "docker-compose.yml"
PROD_COMPOSE = ROOT / "infrastructure" / "docker" / "docker-compose.prod.yml"
SETTINGS_KEYS = set(Settings.model_fields)


class ComposeLoader(yaml.SafeLoader):
    """Accepts Compose merge tags (``!reset``, ``!override``) as plain values."""


def _compose_tag(loader: ComposeLoader, _suffix: str, node: yaml.Node) -> Any:
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    return loader.construct_scalar(node)  # type: ignore[arg-type]


ComposeLoader.add_multi_constructor("!", _compose_tag)


def load_compose(path: Path) -> Any:
    return yaml.load(path.read_text(encoding="utf-8"), Loader=ComposeLoader)  # noqa: S506


def parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


def filled_template() -> dict[str, str]:
    """What an operator ends up with after bootstrap-prod + replacing REPLACE_* values."""
    env = parse_env(PROD_TEMPLATE)
    for key in ("JWT_SECRET", "ENCRYPTION_KEY", "POSTGRES_APP_PASSWORD", "REDIS_PASSWORD"):
        env[key] = secrets.token_urlsafe(48)
    return {
        k: v.replace("REPLACE_WITH_EXTENSION_ID", "abcdefghijklmnopabcdefghijklmnop").replace(
            "REPLACE_WITH_YOUR_HOST", "erp-insight.example.com"
        )
        for k, v in env.items()
    }


def test_production_template_passes_the_production_guard() -> None:
    env = filled_template()
    values: dict[str, Any] = {k.lower(): v for k, v in env.items() if k.lower() in SETTINGS_KEYS}
    values = {k: v for k, v in values.items() if v != ""}
    settings = Settings(_env_file=None, **values)
    assert settings.is_production
    assert settings.llm_provider.value != "mock"
    assert settings.embedding_provider.value != "hashing"
    assert settings.metadata_sync_mode == "celery"
    assert not settings.sqlite_allowed
    assert not settings.insecure_connection_tls_allowed


def test_production_template_leaves_only_documented_placeholders() -> None:
    placeholders = {k for k, v in parse_env(PROD_TEMPLATE).items() if "REPLACE" in v}
    assert placeholders == {"EXTENSION_API_ORIGIN", "CORS_ALLOWED_ORIGINS"}


def test_production_template_has_production_values() -> None:
    env = parse_env(PROD_TEMPLATE)
    assert env["APP_ENV"] == "production"
    assert env["EXTENSION_API_ORIGIN"].startswith("https://")
    assert env["TLS_CERT_DIR"] and env["REGISTRY"] == "local"


def test_no_demo_database_or_seed_ships() -> None:
    assert importlib.util.find_spec("app.demo") is None
    assert not (ROOT / "scripts" / "seed_demo_data.py").exists()
    assert not list((ROOT / "infrastructure" / "postgres" / "init").glob("*demo*"))
    for path in (ROOT / ".env.example", PROD_TEMPLATE, COMPOSE, PROD_COMPOSE):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"ERP_DEMO|erp_demo|seed_demo", text), path.name


def test_product_semantic_layer_is_empty_until_onboarding() -> None:
    bundle = load_semantic_layer(ROOT / "semantic-layer")
    assert bundle.entities == {}
    assert bundle.sensitive_patterns  # the policy ships and protects drafts


def test_production_http_only_redirects() -> None:
    redirect = (ROOT / "infrastructure" / "nginx" / "prod-http" / "redirect.conf").read_text(
        encoding="utf-8"
    )
    assert "return 301 https://" in redirect and "proxy_pass" not in redirect
    nginx = load_compose(PROD_COMPOSE)["services"]["nginx"]
    mounts = nginx["volumes"]
    assert any("prod-http:/etc/nginx/conf.d" in m for m in mounts)
    assert not any("nginx/conf.d:/etc/nginx/conf.d" in m for m in mounts)


@pytest.mark.parametrize("service", ["api", "worker"])
def test_semantic_layer_is_mounted_read_only(service: str) -> None:
    services = load_compose(COMPOSE)["services"]
    mounts = services[service]["volumes"]
    assert any(m.endswith(":/srv/semantic-layer:ro") for m in mounts)


def test_live_data_eval_template_loads_and_is_safety_only() -> None:
    from app.evals.dataset import SAFETY_CATEGORIES, load_dataset

    dataset = load_dataset(ROOT / "evals" / "questions.example.yaml")
    safety = [c for c in dataset.cases if c.category in SAFETY_CATEGORIES]
    assert len(safety) >= 5
    assert all("succeeded" not in c.expect.outcome for c in safety)
