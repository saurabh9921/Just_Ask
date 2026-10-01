"""Security regression tests for the Phase 1 platform surface.

Later phases add: SQL injection, prompt injection, cross-tenant retrieval/cache,
unauthorized connection access, forbidden tables/columns, token misuse.
"""

from __future__ import annotations

import json
import logging

import httpx
import pytest
from pydantic import BaseModel, Field, ValidationError

from app.config import Settings
from app.core.logging import JsonFormatter
from tests.conftest import AppFactory, client_for

pytestmark = pytest.mark.security

STRONG = "Z" * 48


# --------------------------------------------------------------------------
# Production configuration guards
# --------------------------------------------------------------------------
def _prod(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "app_env": "production",
        "jwt_secret": STRONG,
        "encryption_key": STRONG + "E",
        "llm_provider": "vllm",
        "embedding_provider": "openai_compatible",
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "overrides",
    [
        {"jwt_secret": "replace-in-development"},
        {"encryption_key": "replace-in-development"},
        {"jwt_secret": "short-but-unique"},
        {"cors_allowed_origins": ["*"]},
        {"llm_provider": "mock"},
        {"log_json": False},
    ],
)
def test_production_refuses_unsafe_configuration(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError, match="Unsafe production configuration"):
        _prod(**overrides)


def test_external_llm_disabled_by_default_in_every_environment() -> None:
    for env in ("development", "test", "production"):
        with pytest.raises(ValidationError, match="EXTERNAL_LLM_ENABLED"):
            _prod(app_env=env, llm_provider="external")


def test_jwt_none_algorithm_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, jwt_algorithm="none")


def test_production_disables_interactive_docs(app_factory: AppFactory) -> None:
    app = app_factory(settings_override=_prod())
    assert app.docs_url is None
    assert app.openapi_url is None


async def test_production_sends_hsts(app_factory: AppFactory) -> None:
    app = app_factory(settings_override=_prod())
    async with client_for(app) as client:
        response = await client.get("/health/live")
    assert response.headers["strict-transport-security"].startswith("max-age=")


# --------------------------------------------------------------------------
# Information leakage
# --------------------------------------------------------------------------
async def test_internal_errors_do_not_leak_details(app_factory: AppFactory) -> None:
    app = app_factory()

    @app.get("/_test/leak")
    async def leak() -> None:
        raise ValueError("SELECT secret_col FROM payroll; password=hunter2 /srv/app/main.py")

    async with client_for(app) as client:
        response = await client.get("/_test/leak")
    assert response.status_code == 500
    raw = response.text
    for fragment in ("payroll", "hunter2", "Traceback", "main.py", "ValueError"):
        assert fragment not in raw


class Login(BaseModel):
    email: str = Field(pattern=r"^[^@]+@[^@]+$")
    password: str = Field(min_length=12)


async def test_validation_errors_never_echo_submitted_values(app_factory: AppFactory) -> None:
    app = app_factory()

    @app.post("/_test/login")
    async def login(body: Login) -> dict[str, str]:
        return {"ok": "yes"}

    async with client_for(app) as client:
        response = await client.post(
            "/_test/login", json={"email": "<script>alert(1)</script>", "password": "tiny-secret"}
        )
    assert response.status_code == 422
    assert "tiny-secret" not in response.text
    assert "<script>" not in response.text


async def test_readiness_does_not_leak_dependency_errors(app_factory: AppFactory) -> None:
    from tests.conftest import FakeCheck

    app = app_factory(
        [
            FakeCheck(
                "postgres", fail=True, error="password authentication failed for user erp_app"
            ),
            FakeCheck("redis"),
            FakeCheck("qdrant"),
        ]
    )
    async with client_for(app) as client:
        response = await client.get("/health/ready")
    assert response.status_code == 503
    assert "password" not in response.text
    assert "erp_app" not in response.text


# --------------------------------------------------------------------------
# Header / log injection
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "malicious",
    [
        'x", "level": "CRITICAL',
        "abcdefgh\r\nSet-Cookie: session=1",
        "../../../../etc/passwd",
        "A" * 500,
    ],
)
async def test_untrusted_request_ids_are_replaced(
    client: httpx.AsyncClient, malicious: str
) -> None:
    try:
        response = await client.get("/health/live", headers={"X-Request-ID": malicious})
    except (httpx.LocalProtocolError, ValueError):
        return  # the HTTP client itself refused the header - also safe
    assert response.headers["x-request-id"].startswith("req_")
    assert malicious not in response.headers["x-request-id"]


def test_log_message_injection_stays_single_json_line() -> None:
    record = logging.getLogger("sec").makeRecord(
        "sec", logging.INFO, __file__, 1, 'user said: "}\n{"level":"CRITICAL"', (), None
    )
    rendered = JsonFormatter("svc").format(record)
    assert "\n" not in rendered
    assert json.loads(rendered)["level"] == "INFO"


def test_exception_text_in_logs_is_redacted() -> None:
    try:
        raise ConnectionError("could not connect to mysql://erp:TopSecret99@10.0.0.5/erp")
    except ConnectionError:
        import sys

        record = logging.getLogger("sec").makeRecord(
            "sec", logging.ERROR, __file__, 1, "db failure", (), sys.exc_info()
        )
    rendered = JsonFormatter("svc").format(record)
    assert "TopSecret99" not in rendered


# --------------------------------------------------------------------------
# CORS
# --------------------------------------------------------------------------
async def test_cors_rejects_unknown_origins(client: httpx.AsyncClient) -> None:
    for origin in (
        "https://evil.example",
        "chrome-extension://someotherextensionidxxxxxxxxxxx",
        "null",
    ):
        response = await client.options(
            "/health/live", headers={"Origin": origin, "Access-Control-Request-Method": "GET"}
        )
        assert "access-control-allow-origin" not in response.headers, origin


async def test_cors_simple_request_from_unknown_origin_gets_no_acao(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/health/live", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in response.headers
