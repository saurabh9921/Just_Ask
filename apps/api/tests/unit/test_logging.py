from __future__ import annotations

import json
import logging

import pytest

from app.core.logging import REDACTED, JsonFormatter, redact, redact_text, request_id_var


def _format(
    record_kwargs: dict[str, object], extra: dict[str, object] | None = None
) -> dict[str, object]:
    logger = logging.getLogger("test.logging")
    record = logger.makeRecord(
        "test.logging",
        logging.INFO,
        __file__,
        1,
        record_kwargs.get("msg", "m"),
        (),
        None,
        extra=extra,
    )
    return json.loads(JsonFormatter("svc").format(record))  # type: ignore[no-any-return]


def test_json_formatter_emits_single_line_with_core_fields() -> None:
    token = request_id_var.set("req_abc")
    try:
        out = _format({"msg": "hello"}, extra={"route": "/x"})
    finally:
        request_id_var.reset(token)
    assert out["message"] == "hello"
    assert out["level"] == "INFO"
    assert out["service"] == "svc"
    assert out["request_id"] == "req_abc"
    assert out["extra"] == {"route": "/x"}


def test_sensitive_extra_keys_are_redacted() -> None:
    out = _format(
        {"msg": "login"},
        extra={"password": "p@ss", "access_token": "abc", "nested": {"api_key": "k", "ok": 1}},
    )
    extra = out["extra"]
    assert extra == {
        "password": REDACTED,
        "access_token": REDACTED,
        "nested": {"api_key": REDACTED, "ok": 1},
    }


@pytest.mark.parametrize(
    ("raw", "leak"),
    [
        ("connect postgresql://erp:S3cr3t!@db:5432/erp failed", "S3cr3t!"),
        ("Authorization: Bearer abc.def.ghi-123", "abc.def.ghi-123"),
        ("token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sig_part", "eyJzdWIiOiIxIn0"),
        ("password=hunter2 user=bob", "hunter2"),
        ('api_key: "sk-live-123"', "sk-live-123"),
    ],
)
def test_redact_text_scrubs_credentials(raw: str, leak: str) -> None:
    cleaned = redact_text(raw)
    assert leak not in cleaned
    assert REDACTED in cleaned


def test_redact_leaves_ordinary_text_alone() -> None:
    assert redact_text("Show total sales by region") == "Show total sales by region"


def test_redact_limits_depth() -> None:
    deep: dict[str, object] = {}
    cursor = deep
    for _ in range(20):
        nxt: dict[str, object] = {}
        cursor["a"] = nxt
        cursor = nxt
    rendered = json.dumps(redact(deep))
    assert "[TRUNCATED]" in rendered
