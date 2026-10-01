from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import AppEnv, LLMProviderName, Settings

STRONG = "k" * 48


def make(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


def test_defaults_are_development_and_mock_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APP_ENV", raising=False)
    s = make()
    assert s.app_env is AppEnv.DEVELOPMENT
    assert s.llm_provider is LLMProviderName.MOCK
    assert s.external_llm_enabled is False
    assert s.max_query_rows == 1000
    assert s.max_query_seconds == 30


def test_csv_env_values_are_split(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "chrome-extension://a, chrome-extension://b ,")
    monkeypatch.setenv("READINESS_REQUIRED_CHECKS", "postgres,redis")
    s = Settings(_env_file=None)
    assert s.cors_allowed_origins == ["chrome-extension://a", "chrome-extension://b"]
    assert s.readiness_required_checks == ["postgres", "redis"]


def test_secrets_are_not_rendered_in_repr() -> None:
    s = make(jwt_secret="super-secret-value-1234567890abcdef")
    assert "super-secret-value" not in repr(s)
    assert "super-secret-value" not in str(s.model_dump())


def test_broker_url_falls_back_to_redis_url() -> None:
    s = make(redis_url="redis://cache:6379/2")
    assert s.broker_url == "redis://cache:6379/2"
    assert (
        make(redis_url="redis://a/0", celery_broker_url="redis://b/1").broker_url == "redis://b/1"
    )


@pytest.mark.parametrize("value", ["TRACE", "verbose"])
def test_invalid_log_level_rejected(value: str) -> None:
    with pytest.raises(ValidationError):
        make(log_level=value)


def test_max_query_rows_is_bounded() -> None:
    with pytest.raises(ValidationError):
        make(max_query_rows=1_000_000)


def test_production_accepts_strong_configuration() -> None:
    s = make(
        app_env="production",
        jwt_secret=STRONG,
        encryption_key=STRONG + "x",
        llm_provider="vllm",
        embedding_provider="openai_compatible",
        cors_allowed_origins=["chrome-extension://abc"],
    )
    assert s.is_production


def test_empty_env_values_mean_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALLOW_INSECURE_CONNECTION_TLS", "")
    monkeypatch.setenv("SQLITE_CONNECTIONS_ENABLED", "")
    monkeypatch.setenv("QDRANT_API_KEY", "")
    monkeypatch.delenv("APP_ENV", raising=False)
    s = Settings(_env_file=None)
    assert s.allow_insecure_connection_tls is None
    assert s.insecure_connection_tls_allowed is True
    assert s.qdrant_api_key is None


def test_insecure_tls_and_sqlite_default_off_in_production() -> None:
    s = make(
        app_env="production",
        jwt_secret=STRONG,
        encryption_key=STRONG + "x",
        llm_provider="vllm",
        embedding_provider="openai_compatible",
    )
    assert s.insecure_connection_tls_allowed is False
    assert s.sqlite_allowed is False


def test_sqlite_cannot_be_enabled_in_production() -> None:
    with pytest.raises(ValidationError, match="SQLITE_CONNECTIONS_ENABLED"):
        make(
            app_env="production",
            jwt_secret=STRONG,
            encryption_key=STRONG + "x",
            llm_provider="vllm",
            embedding_provider="openai_compatible",
            sqlite_connections_enabled=True,
        )
