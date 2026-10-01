"""Load and validate a semantic-layer directory.

Layout (all optional except ``entities/``)::

    entities/*.yaml   one entity per file, or ``entities: [...]``
    metrics/*.yaml    ``metrics: [...]``
    joins/*.yaml      ``joins: [...]``
    synonyms/*.yaml   ``terms: [...]``, ``abbreviations: [...]``
    examples/*.yaml   ``examples: [...]``
    policies/*.yaml   ``sensitive_fields: [...]``

YAML is parsed with ``safe_load`` only; files are size-capped. The bundle carries
a SHA-256 ``content_hash`` over its canonical JSON so ingestion can tell whether
anything changed.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import yaml
from pydantic import BaseModel, ValidationError

from app.semantic.models import (
    Abbreviation,
    Entity,
    EntityFile,
    ExampleFile,
    GlossaryFile,
    GlossaryTerm,
    Join,
    JoinFile,
    Metric,
    MetricFile,
    PolicyFile,
    QueryExample,
    SensitivePattern,
)
from app.semantic.policy import SensitiveFieldPolicy

MAX_FILE_BYTES: Final = 1_000_000


class SemanticLayerError(Exception):
    def __init__(self, issues: list[str]) -> None:
        super().__init__("; ".join(issues[:20]))
        self.issues = issues


@dataclass(frozen=True)
class SemanticBundle:
    entities: dict[str, Entity]
    metrics: list[Metric] = field(default_factory=list)
    joins: list[Join] = field(default_factory=list)
    terms: list[GlossaryTerm] = field(default_factory=list)
    abbreviations: list[Abbreviation] = field(default_factory=list)
    examples: list[QueryExample] = field(default_factory=list)
    sensitive_patterns: list[SensitivePattern] = field(default_factory=list)
    warnings: tuple[str, ...] = ()

    @property
    def policy(self) -> SensitiveFieldPolicy:
        return SensitiveFieldPolicy(self.sensitive_patterns)

    @property
    def content_hash(self) -> str:
        def dump(items: Sequence[BaseModel]) -> list[Any]:
            return [i.model_dump(mode="json", by_alias=True) for i in items]

        metrics: list[Metric] = sorted(self.metrics, key=lambda m: (m.entity, m.name))
        joins: list[Join] = sorted(self.joins, key=lambda j: j.name)
        terms: list[GlossaryTerm] = sorted(self.terms, key=lambda t: t.term.lower())
        abbreviations: list[Abbreviation] = sorted(self.abbreviations, key=lambda a: a.abbreviation)
        examples: list[QueryExample] = sorted(self.examples, key=lambda e: e.id)
        canonical = {
            "entities": dump([self.entities[k] for k in sorted(self.entities)]),
            "metrics": dump(metrics),
            "joins": dump(joins),
            "terms": dump(terms),
            "abbreviations": dump(abbreviations),
            "examples": dump(examples),
            "policies": dump(self.sensitive_patterns),
        }
        return hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()


def _read_yaml(path: Path, issues: list[str]) -> Any:
    if path.stat().st_size > MAX_FILE_BYTES:
        issues.append(f"{path.name}: file exceeds {MAX_FILE_BYTES} bytes")
        return None
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        issues.append(f"{path.name}: invalid YAML ({type(exc).__name__})")
        return None


def _files(root: Path, sub: str) -> list[Path]:
    folder = root / sub
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.iterdir() if p.suffix in {".yaml", ".yml"} and p.is_file())


def _parse[M: BaseModel](model: type[M], data: Any, where: str, issues: list[str]) -> M | None:
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        for err in exc.errors()[:20]:
            loc = ".".join(str(p) for p in err["loc"])
            issues.append(f"{where}: {loc}: {err['msg']}")
        return None


def load_semantic_layer(root: Path | str) -> SemanticBundle:
    """Load, validate and cross-check a semantic layer. Raises ``SemanticLayerError``."""
    base = Path(root)
    if not (base / "entities").is_dir():
        raise SemanticLayerError([f"{base}: missing entities/ directory"])
    issues: list[str] = []

    entities: dict[str, Entity] = {}
    for path in _files(base, "entities"):
        data = _read_yaml(path, issues)
        if data is None:
            continue
        if isinstance(data, dict) and "entity" in data:
            data = {"entities": [data]}
        parsed = _parse(EntityFile, data, f"entities/{path.name}", issues)
        for entity in parsed.entities if parsed else []:
            if entity.entity in entities:
                issues.append(f"entities/{path.name}: duplicate entity {entity.entity}")
            entities[entity.entity] = entity

    def collect[M: BaseModel](sub: str, model: type[M]) -> list[M]:
        out: list[M] = []
        for path in _files(base, sub):
            data = _read_yaml(path, issues)
            if data is None:
                continue
            parsed = _parse(model, data, f"{sub}/{path.name}", issues)
            if parsed is not None:
                out.append(parsed)
        return out

    metrics = [m for f in collect("metrics", MetricFile) for m in f.metrics]
    joins = [j for f in collect("joins", JoinFile) for j in f.joins]
    glossary = collect("synonyms", GlossaryFile)
    examples = [e for f in collect("examples", ExampleFile) for e in f.examples]
    policies = [p for f in collect("policies", PolicyFile) for p in f.sensitive_fields]

    bundle = SemanticBundle(
        entities=entities,
        metrics=metrics,
        joins=joins,
        terms=[t for g in glossary for t in g.terms],
        abbreviations=[a for g in glossary for a in g.abbreviations],
        examples=examples,
        sensitive_patterns=policies,
    )
    if issues:
        raise SemanticLayerError(issues)

    from app.semantic.validator import validate_bundle  # noqa: PLC0415 - avoids an import cycle

    report = validate_bundle(bundle)
    if report.errors:
        raise SemanticLayerError(report.errors)
    return SemanticBundle(**{**bundle.__dict__, "warnings": tuple(report.warnings)})
