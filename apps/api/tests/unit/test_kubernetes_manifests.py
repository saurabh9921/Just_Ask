"""Hardening invariants of infrastructure/kubernetes (parsed, no cluster needed)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[4] / "infrastructure" / "kubernetes"
FILES = sorted(p for p in ROOT.rglob("*.yaml") if p.name != "kustomization.yaml")
WORKLOAD_KINDS = {"Deployment", "StatefulSet", "Job", "DaemonSet"}


def documents() -> list[tuple[str, dict[str, Any]]]:
    docs: list[tuple[str, dict[str, Any]]] = []
    for path in FILES:
        for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
            if doc:
                docs.append((path.name, doc))
    return docs


def workloads() -> list[tuple[str, dict[str, Any]]]:
    return [
        (f"{f}:{d['metadata']['name']}", d) for f, d in documents() if d["kind"] in WORKLOAD_KINDS
    ]


def pod_spec(workload: dict[str, Any]) -> dict[str, Any]:
    spec: dict[str, Any] = workload["spec"]["template"]["spec"]
    return spec


def test_kustomization_lists_every_base_file() -> None:
    kustomization = yaml.safe_load((ROOT / "kustomization.yaml").read_text(encoding="utf-8"))
    base = {p.name for p in ROOT.glob("*.yaml") if p.name != "kustomization.yaml"}
    assert set(kustomization["resources"]) == base


@pytest.mark.parametrize(
    ("name", "workload"), workloads(), ids=lambda v: v if isinstance(v, str) else ""
)
def test_workloads_run_restricted(name: str, workload: dict[str, Any]) -> None:
    spec = pod_spec(workload)
    pod_ctx = spec.get("securityContext", {})
    assert pod_ctx.get("runAsNonRoot") is True, name
    assert pod_ctx.get("seccompProfile", {}).get("type") == "RuntimeDefault", name
    assert spec.get("automountServiceAccountToken") is False, name
    for container in spec["containers"]:
        ctx = container.get("securityContext", {})
        assert ctx.get("allowPrivilegeEscalation") is False, name
        assert ctx.get("readOnlyRootFilesystem") is True, name
        assert ctx.get("capabilities", {}).get("drop") == ["ALL"], name
        limits = container.get("resources", {}).get("limits", {})
        assert "cpu" in limits and "memory" in limits, name
        image = container["image"]
        assert ":" in image and not image.endswith(":latest"), name
        if workload["kind"] != "Job":
            assert "livenessProbe" in container, name


@pytest.mark.parametrize(
    ("name", "workload"), workloads(), ids=lambda v: v if isinstance(v, str) else ""
)
def test_no_inline_secrets(name: str, workload: dict[str, Any]) -> None:
    sensitive = ("PASSWORD", "SECRET", "KEY", "TOKEN")
    for container in pod_spec(workload)["containers"]:
        for env in container.get("env", []):
            if any(word in env["name"] for word in sensitive):
                assert "valueFrom" in env and "value" not in env, f"{name}: {env['name']}"


def test_no_secret_objects_are_committed() -> None:
    assert not [f for f, d in documents() if d["kind"] == "Secret"]


def test_scheduler_is_a_singleton() -> None:
    scheduler = next(d for _, d in documents() if d["metadata"]["name"] == "scheduler")
    assert scheduler["spec"]["replicas"] == 1
    assert scheduler["spec"]["strategy"]["type"] == "Recreate"


def test_network_policies_default_deny_and_block_metadata_egress() -> None:
    policies = {d["metadata"]["name"]: d for _, d in documents() if d["kind"] == "NetworkPolicy"}
    deny = policies["default-deny-all"]["spec"]
    assert deny["podSelector"] == {} and set(deny["policyTypes"]) == {"Ingress", "Egress"}
    for policy in policies.values():
        for rule in policy["spec"].get("egress", []):
            for peer in rule.get("to", []):
                cidr = peer.get("ipBlock", {}).get("cidr", "")
                assert cidr not in {"0.0.0.0/0", "::/0"}, policy["metadata"]["name"]
                assert not cidr.startswith("169.254."), policy["metadata"]["name"]


def test_ingress_exposes_only_api_and_health_over_tls() -> None:
    ingress = next(d for _, d in documents() if d["kind"] == "Ingress")
    paths = {p["path"] for r in ingress["spec"]["rules"] for p in r["http"]["paths"]}
    assert paths == {"/api/", "/health/"}
    assert ingress["spec"]["tls"]


def test_namespace_enforces_restricted_pod_security() -> None:
    ns = next(d for _, d in documents() if d["kind"] == "Namespace")
    assert ns["metadata"]["labels"]["pod-security.kubernetes.io/enforce"] == "restricted"


def test_production_config_is_safe() -> None:
    config = next(
        d
        for _, d in documents()
        if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "erp-insight-config"
    )["data"]
    assert config["APP_ENV"] == "production"
    assert config["EXTERNAL_LLM_ENABLED"] == "false"
    assert config["SQLITE_CONNECTIONS_ENABLED"] == "false"
    assert config["ENABLE_FOUNDATION_CONNECTORS"] == "false"
    assert config["LLM_PROVIDER"] != "mock" and config["EMBEDDING_PROVIDER"] != "hashing"
    assert "169.254.0.0/16" in config["CONNECTION_HOST_DENYLIST"]
