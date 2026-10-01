"""Draft semantic-layer entities from a live database's discovered metadata.

Onboarding a real ERP needs a semantic layer that describes *its* tables. Writing that
by hand from scratch is slow, so this module turns metadata-sync results (tables/views,
columns, canonical types, keys, row estimates) into **draft** YAML entities that pass
validation immediately and that an administrator then reviews: rename fields to business
language, add descriptions and synonyms, fix aggregations/formats, and decide security.

Deterministic rules (no model involved):

* columns matching the sensitive-field policy (``forbid`` *or* ``mask``) are never
  exposed and are listed under ``security.forbidden_fields`` - safe by default;
* ``date``/``timestamp`` columns become time fields (a date-like name becomes the default);
* text, boolean, uuid and key-like integer columns (``*_id``, ``*_code``, ``*_no``,
  primary/foreign keys) become dimensions;
* other numeric columns become ``sum`` measures (``currency`` format for money-like
  names); every entity also gets a ``record_count`` measure;
* JSON/binary/unknown columns and names that are not plain identifiers are skipped;
* views/tables with more than ``LARGE_FACT_ROWS`` estimated rows are marked
  ``large_fact`` so questions without a date range get a default window;
* foreign keys between selected objects become ``many_to_one`` join drafts.

No row filter is added: a dedicated customer database needs none. For a database shared
by several tenants, add ``security.required_filters`` before ingesting.
"""

from __future__ import annotations

import re
import shutil
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import yaml

from app.connectors.common.types import CanonicalType
from app.semantic.loader import SemanticBundle, load_semantic_layer
from app.semantic.policy import SensitiveFieldPolicy

LARGE_FACT_ROWS: Final = 1_000_000
_PHYSICAL = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]{0,127}$")
_KEY_LIKE = re.compile(
    r"(^id$|_id$|_code$|^code$|_no$|_num$|_number$|_key$|_nr$|^year$|^month$|_year$|_month$)",
    re.IGNORECASE,
)
_CAMEL_ID = re.compile(r"[a-z0-9](Id|ID|Code|No|Key)$")  # CustomerID, OrderNo
_MONEY = re.compile(
    r"(amount|amt|price|value|cost|revenue|sales|turnover|total|net|gross|balance|payment|spend)",
    re.IGNORECASE,
)
# Per-unit and ratio columns are not additive (a summed price or percentage means
# nothing), so their draft measure averages; matched on the snake_case name.
_NON_ADDITIVE = re.compile(
    r"(^|_)(unit_?price|price_per|rate|ratio|percent(age)?|pct|score|rating|avg|average"
    r"|mean|latitude|longitude)(_|$)|^(price|lat|lng|lon)$"
)
_DATE_PREFERENCE = re.compile(r"(order|posting|document|invoice|transaction|created|date)", re.I)
_TEXTUAL = {CanonicalType.STRING, CanonicalType.BOOLEAN, CanonicalType.UUID}
_NUMERIC = {CanonicalType.INTEGER, CanonicalType.DECIMAL, CanonicalType.FLOAT}
_TIME = {CanonicalType.DATE, CanonicalType.TIMESTAMP}


# ------------------------------------------------------------------ inputs
@dataclass(frozen=True)
class DraftColumn:
    name: str
    canonical_type: CanonicalType
    comment: str | None = None


@dataclass(frozen=True)
class DraftForeignKey:
    columns: list[str]
    referred_table: str
    referred_columns: list[str]


@dataclass(frozen=True)
class DraftSource:
    """One discovered table or view (the shape of ``GET /connections/{id}/entities/{name}``)."""

    schema_name: str | None
    name: str
    kind: str
    columns: list[DraftColumn]
    row_estimate: int | None = None
    comment: str | None = None
    primary_key: list[str] = field(default_factory=list)
    foreign_keys: list[DraftForeignKey] = field(default_factory=list)

    @classmethod
    def from_api(cls, body: dict[str, Any]) -> DraftSource:
        return cls(
            schema_name=body.get("schema_name"),
            name=body["name"],
            kind=body.get("kind", "table"),
            row_estimate=body.get("row_estimate"),
            comment=body.get("comment"),
            primary_key=list(body.get("primary_key") or []),
            columns=[
                DraftColumn(c["name"], CanonicalType(c["canonical_type"]), c.get("comment"))
                for c in body.get("columns") or []
            ],
            foreign_keys=[
                DraftForeignKey(
                    list(fk["columns"]), fk["referred_table"], list(fk["referred_columns"])
                )
                for fk in body.get("foreign_keys") or []
            ],
        )


# ------------------------------------------------------------------ outputs
@dataclass
class EntityDraft:
    entity: str
    source: DraftSource
    document: dict[str, Any]
    skipped: list[str] = field(default_factory=list)  # "column: reason"

    @property
    def file_name(self) -> str:
        return f"{snake(self.entity)}.yaml"

    def to_yaml(self, connection_name: str) -> str:
        header = [
            f"# DRAFT generated {_today()} from connection '{connection_name}'",
            f"# ({self.source.kind} {qualified(self.source)}). REVIEW before ingesting:",
            "#  - rename entity/fields to business language, add descriptions and synonyms",
            "#  - check aggregations and formats; delete measures that make no sense to sum",
            "#  - keep sensitive columns in security.forbidden_fields",
            "#  - shared multi-tenant database? add security.required_filters",
        ]
        if self.skipped:
            header.append("# Skipped columns: " + "; ".join(self.skipped)[:900])
        body = yaml.safe_dump(self.document, sort_keys=False, allow_unicode=True, width=100)
        return "\n".join(header) + "\n" + body


@dataclass
class DraftSet:
    entities: list[EntityDraft]
    joins: list[dict[str, Any]]

    def joins_yaml(self, connection_name: str) -> str | None:
        if not self.joins:
            return None
        header = (
            f"# DRAFT joins generated {_today()} from foreign keys of "
            f"connection '{connection_name}'.\n"
            "# Only listed joins can ever be generated. Review each relationship.\n"
        )
        return header + yaml.safe_dump({"joins": self.joins}, sort_keys=False, width=100)


# ------------------------------------------------------------------ naming
def _today() -> str:
    return datetime.now(tz=UTC).date().isoformat()


def qualified(source: DraftSource) -> str:
    return f"{source.schema_name}.{source.name}" if source.schema_name else source.name


def _words(name: str) -> list[str]:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    return [w for w in re.split(r"[^A-Za-z0-9]+", spaced) if w]


def snake(name: str) -> str:
    out = "_".join(w.lower() for w in _words(name)) or "field"
    if not out[0].isalpha():
        out = f"f_{out}"
    return out[:63].rstrip("_")


def pascal(name: str) -> str:
    out = "".join(w[:1].upper() + w[1:].lower() for w in _words(name)) or "Entity"
    if not out[0].isalpha():
        out = f"T{out}"
    return out[:63]


def _unique(name: str, taken: set[str], limit: int = 63) -> str:
    candidate, n = name, 2
    while candidate in taken:
        suffix = f"_{n}" if name[:1].islower() else str(n)
        candidate = f"{name[: limit - len(suffix)]}{suffix}"
        n += 1
    taken.add(candidate)
    return candidate


# ------------------------------------------------------------------ drafting
def _is_key(column: DraftColumn, source: DraftSource, fk_columns: set[str]) -> bool:
    return (
        column.name in source.primary_key
        or column.name in fk_columns
        or bool(_KEY_LIKE.search(column.name))
        or bool(_CAMEL_ID.search(column.name))
    )


def draft_entity(
    source: DraftSource, policy: SensitiveFieldPolicy, taken_entities: set[str]
) -> EntityDraft:
    entity_name = _unique(pascal(source.name), taken_entities)
    fk_columns = {c for fk in source.foreign_keys for c in fk.columns}
    names: set[str] = set()
    dimensions: list[dict[str, Any]] = []
    measures: list[dict[str, Any]] = []
    time_fields: list[dict[str, Any]] = []
    forbidden: list[str] = []
    skipped: list[str] = []

    for column in source.columns:
        if not _PHYSICAL.fullmatch(column.name):
            skipped.append(f"{column.name}: not a plain identifier")
            continue
        if policy.classify(column.name) is not None:
            forbidden.append(column.name)
            continue
        described = {"description": column.comment.strip()[:500]} if column.comment else {}
        ctype = column.canonical_type
        if ctype in _TIME:
            time_fields.append(
                {
                    "name": _unique(snake(column.name), names),
                    "field": column.name,
                    "type": ctype.value,
                    **described,
                }
            )
        elif ctype in _TEXTUAL or (
            ctype is CanonicalType.INTEGER and _is_key(column, source, fk_columns)
        ):
            dimensions.append(
                {
                    "name": _unique(snake(column.name), names),
                    "field": column.name,
                    "type": ctype.value,
                    **described,
                }
            )
        elif ctype in _NUMERIC:
            money = bool(_MONEY.search(column.name)) and ctype is not CanonicalType.INTEGER
            average = bool(_NON_ADDITIVE.search(snake(column.name)))
            measures.append(
                {
                    "name": _unique(snake(column.name), names),
                    "field": column.name,
                    "aggregation": "avg" if average else "sum",
                    "type": ctype.value,
                    "format": "currency"
                    if money
                    else "integer"
                    if ctype is CanonicalType.INTEGER
                    else "decimal",
                    **(
                        described
                        or {"description": f"{'Average' if average else 'Sum'} of {column.name}."}
                    ),
                }
            )
        else:
            skipped.append(f"{column.name}: {ctype.value} columns are not queryable")

    count_field = next(
        (c for c in source.primary_key if c not in forbidden and _PHYSICAL.fullmatch(c)),
        None,
    ) or next((d["field"] for d in dimensions), None)
    if count_field is not None:
        measures.insert(
            0,
            {
                "name": _unique("record_count", names),
                "field": count_field,
                "aggregation": "count",
                "type": "integer",
                "format": "integer",
                "description": f"Number of records in {qualified(source)}.",
            },
        )

    default_time: str | None = None
    if time_fields:
        preferred = next(
            (t for t in time_fields if _DATE_PREFERENCE.search(t["field"])), time_fields[0]
        )
        preferred["default"] = True
        default_time = preferred["name"]

    source_block: dict[str, Any] = {
        "logical_name": source.name,
        "allowed_database_objects": [source.name],
    }
    if source.schema_name and _PHYSICAL.fullmatch(source.schema_name):
        source_block["schema_name"] = source.schema_name
    human = " ".join(_words(source.name)).lower()
    document: dict[str, Any] = {
        "entity": entity_name,
        "description": (source.comment or "").strip()[:1500]
        or f"DRAFT from {source.kind} {qualified(source)} - describe what one row represents.",
        "source": source_block,
        "synonyms": [human] if human and human != entity_name.lower() else [],
        "classification": "internal",
    }
    if default_time:
        document["default_time_field"] = default_time
    if (source.row_estimate or 0) >= LARGE_FACT_ROWS and time_fields:
        document["large_fact"] = True
    document["dimensions"] = dimensions
    document["measures"] = measures
    document["time_fields"] = time_fields
    document["security"] = {"required_filters": [], "forbidden_fields": sorted(set(forbidden))}
    return EntityDraft(entity_name, source, document, skipped)


def draft_joins(drafts: list[EntityDraft]) -> list[dict[str, Any]]:
    by_table = {d.source.name.lower(): d for d in drafts}
    joins: list[dict[str, Any]] = []
    names: set[str] = set()
    for draft in drafts:
        exposed_from = _exposed(draft)
        for fk in draft.source.foreign_keys:
            target = by_table.get(fk.referred_table.lower())
            if target is None or target is draft:
                continue
            pairs = list(zip(fk.columns, fk.referred_columns, strict=False))
            if not pairs or any(
                a not in exposed_from or b not in _exposed(target) for a, b in pairs
            ):
                continue
            name = _unique(f"{snake(draft.entity)}_to_{snake(target.entity)}"[:63], names)
            joins.append(
                {
                    "name": name,
                    "from": draft.entity,
                    "to": target.entity,
                    "relationship": "many_to_one",
                    "on": [{"from_field": a, "to_field": b} for a, b in pairs],
                }
            )
    return joins


def _exposed(draft: EntityDraft) -> set[str]:
    doc = draft.document
    return {item["field"] for key in ("dimensions", "time_fields") for item in doc.get(key, [])}


def draft_semantic_layer(
    sources: Iterable[DraftSource],
    policy: SensitiveFieldPolicy,
    *,
    existing_entities: Iterable[str] = (),
) -> DraftSet:
    taken = set(existing_entities)
    drafts = [draft_entity(s, policy, taken) for s in sources]
    return DraftSet(entities=drafts, joins=draft_joins(drafts))


# ------------------------------------------------------------------ checking
def check_with_layer(layer_root: Path, drafts: DraftSet, connection_name: str) -> SemanticBundle:
    """Load the existing semantic layer *plus* the drafts; raises ``SemanticLayerError``."""
    with tempfile.TemporaryDirectory(prefix="semantic-draft-") as tmp:
        target = Path(tmp) / "layer"
        if (layer_root / "entities").is_dir():
            shutil.copytree(layer_root, target, ignore=shutil.ignore_patterns(".git*"))
        else:
            (target / "entities").mkdir(parents=True)
        write_drafts(target, drafts, connection_name, overwrite=True)
        return load_semantic_layer(target)


def write_drafts(
    layer_root: Path, drafts: DraftSet, connection_name: str, *, overwrite: bool = False
) -> list[Path]:
    """Write entity drafts (and a joins draft); refuses to overwrite unless asked."""
    written: list[Path] = []
    entities_dir = layer_root / "entities"
    entities_dir.mkdir(parents=True, exist_ok=True)
    outputs = [(entities_dir / d.file_name, d.to_yaml(connection_name)) for d in drafts.entities]
    joins_text = drafts.joins_yaml(connection_name)
    if joins_text:
        slug = snake(connection_name) or "connection"
        outputs.append((layer_root / "joins" / f"{slug}-draft-joins.yaml", joins_text))
    clashes = [str(p) for p, _ in outputs if p.exists()]
    if clashes and not overwrite:
        raise FileExistsError(", ".join(clashes))
    for path, text in outputs:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        written.append(path)
    return written
