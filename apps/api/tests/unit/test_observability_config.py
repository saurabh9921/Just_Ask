"""Alert rules and dashboards only reference metrics the application exports, and
every alert has a severity and a runbook section in docs/observability.md."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

from app.core.metrics import REGISTRY

ROOT = Path(__file__).resolve().parents[4]
ALERTS = ROOT / "infrastructure" / "prometheus" / "alerts.yml"
DASHBOARDS = sorted((ROOT / "infrastructure" / "grafana" / "dashboards").glob("*.json"))
RUNBOOK = ROOT / "docs" / "observability.md"
METRIC = re.compile(r"\berp_[a-z0-9_]+")


def exported_names() -> set[str]:
    names: set[str] = set()
    for family in REGISTRY.collect():
        names.add(family.name)
        if family.type == "counter":
            names.add(f"{family.name}_total")
        if family.type == "histogram":
            names |= {f"{family.name}_{s}" for s in ("bucket", "count", "sum")}
    return names


def anchors(markdown: str) -> set[str]:
    return {
        re.sub(r"[^a-z0-9 -]", "", h.lower()).strip().replace(" ", "-")
        for h in re.findall(r"^#+\s+(.+)$", markdown, flags=re.MULTILINE)
    }


def test_alert_rules_reference_exported_metrics() -> None:
    text = ALERTS.read_text(encoding="utf-8")
    unknown = set(METRIC.findall(text)) - exported_names()
    assert unknown == set()


def test_alert_rules_are_well_formed_and_have_runbooks() -> None:
    doc = yaml.safe_load(ALERTS.read_text(encoding="utf-8"))
    sections = anchors(RUNBOOK.read_text(encoding="utf-8"))
    seen: set[str] = set()
    for group in doc["groups"]:
        for rule in group["rules"]:
            name = rule["alert"]
            assert name not in seen, name
            seen.add(name)
            assert rule["expr"].strip()
            assert rule["labels"]["severity"] in {"critical", "warning"}, name
            runbook = rule["annotations"]["runbook"]
            assert runbook.startswith("docs/observability.md#"), name
            assert runbook.split("#", 1)[1] in sections, f"missing runbook section for {name}"
    assert len(seen) >= 10


@pytest.mark.parametrize("path", DASHBOARDS, ids=lambda p: p.name)
def test_dashboards_reference_exported_metrics(path: Path) -> None:
    dashboard = json.loads(path.read_text(encoding="utf-8"))
    exprs = [t["expr"] for p in dashboard["panels"] for t in p.get("targets", [])]
    assert exprs, path.name
    unknown = {m for e in exprs for m in METRIC.findall(e)} - exported_names()
    assert unknown == set()
    titles = [p["title"] for p in dashboard["panels"]]
    assert len(titles) == len(set(titles))
    assert all(p["datasource"]["uid"] == "prometheus" for p in dashboard["panels"])
