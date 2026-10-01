"""Exported JSON Schema contracts must match the Pydantic models."""

from __future__ import annotations

import json
from pathlib import Path

from app.contracts import render_contracts

CONTRACTS_DIR = Path(__file__).resolve().parents[4] / "packages" / "contracts"


def test_committed_contracts_match_the_models() -> None:
    for name, schema in render_contracts().items():
        path = CONTRACTS_DIR / name
        assert path.exists(), f"{name} missing: run scripts/export_contracts.py"
        committed = json.loads(path.read_text(encoding="utf-8"))
        assert committed == schema, f"{name} is stale: run scripts/export_contracts.py"


def test_query_plan_contract_forbids_unknown_keys() -> None:
    schema = render_contracts()["query-plan.schema.json"]
    assert schema["additionalProperties"] is False
    assert "sql" not in schema["properties"]
