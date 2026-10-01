"""Loader/validator error handling for the semantic layer."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from app.semantic.loader import MAX_FILE_BYTES, SemanticLayerError, load_semantic_layer

SALES = """
entity: SalesOrder
description: Customer sales orders
source:
  logical_name: analytics_sales_orders
  allowed_database_objects: [analytics_sales_orders]
dimensions:
  - {name: region, field: region_name, synonyms: [territory]}
  - {name: customer_id, field: customer_id, type: integer}
measures:
  - {name: total_sales, field: net_amount, aggregation: sum, format: currency}
  - {name: order_count, field: order_id, aggregation: count_distinct, type: integer}
time_fields:
  - {name: order_date, field: order_date, default: true}
security:
  required_filters: [tenant_code]
  forbidden_fields: [bank_account]
"""

CUSTOMER = """
entity: Customer
description: Customers
source:
  logical_name: analytics_customers
  allowed_database_objects: [analytics_customers]
dimensions:
  - {name: customer, field: customer_name}
measures:
  - {name: customer_count, field: customer_id, aggregation: count_distinct, type: integer}
"""


def write_layer(root: Path, files: dict[str, str]) -> Path:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(textwrap.dedent(content).encode("utf-8"))
    return root


def issues_for(tmp_path: Path, files: dict[str, str]) -> str:
    with pytest.raises(SemanticLayerError) as info:
        load_semantic_layer(write_layer(tmp_path, files))
    return "\n".join(info.value.issues)


def test_minimal_layer_loads_and_expands_filter_shorthand(tmp_path: Path) -> None:
    bundle = load_semantic_layer(write_layer(tmp_path, {"entities/sales.yaml": SALES}))
    rf = bundle.entities["SalesOrder"].security.required_filters
    assert [(f.field, f.value_from) for f in rf] == [("tenant_code", "tenant.slug")]
    assert bundle.entities["SalesOrder"].security.required_permissions == ["analytics.query"]


def test_missing_entities_directory(tmp_path: Path) -> None:
    with pytest.raises(SemanticLayerError, match="missing entities"):
        load_semantic_layer(tmp_path)


def test_duplicate_entity(tmp_path: Path) -> None:
    out = issues_for(tmp_path, {"entities/a.yaml": SALES, "entities/b.yaml": SALES})
    assert "duplicate entity SalesOrder" in out


def test_unknown_join_entity(tmp_path: Path) -> None:
    joins = """
    joins:
      - name: bad
        from: SalesOrder
        to: Ghost
        "on": [{from_field: customer_id, to_field: customer_id}]
    """
    out = issues_for(tmp_path, {"entities/s.yaml": SALES, "joins/j.yaml": joins})
    assert "unknown entity Ghost" in out


def test_join_on_forbidden_key(tmp_path: Path) -> None:
    joins = """
    joins:
      - name: leak
        from: SalesOrder
        to: Customer
        "on": [{from_field: bank_account, to_field: customer_id}]
    """
    out = issues_for(
        tmp_path, {"entities/s.yaml": SALES, "entities/c.yaml": CUSTOMER, "joins/j.yaml": joins}
    )
    assert "forbidden" in out


def test_forbidden_field_exposed(tmp_path: Path) -> None:
    bad = SALES.replace(
        "{name: customer_id, field: customer_id, type: integer}",
        "{name: account, field: bank_account}",
    )
    out = issues_for(tmp_path, {"entities/s.yaml": bad})
    assert "forbidden" in out


def test_policy_forbidden_field_exposed_without_listing(tmp_path: Path) -> None:
    bad = SALES.replace(
        "{name: customer_id, field: customer_id, type: integer}",
        "{name: iban, field: customer_iban}",
    )
    out = issues_for(tmp_path, {"entities/s.yaml": bad})
    assert "forbidden by policy" in out


def test_mask_field_must_be_marked_sensitive(tmp_path: Path) -> None:
    policy = """
    sensitive_fields:
      - {pattern: "(?i)e_?mail", classification: confidential, action: mask}
    """
    bad = SALES.replace(
        "{name: customer_id, field: customer_id, type: integer}",
        "{name: contact, field: customer_email}",
    )
    out = issues_for(tmp_path, {"entities/s.yaml": bad, "policies/p.yaml": policy})
    assert "must be marked sensitive" in out
    ok = bad.replace(
        "{name: contact, field: customer_email}",
        "{name: contact, field: customer_email, sensitive: true}",
    )
    (tmp_path / "entities/s.yaml").write_bytes(textwrap.dedent(ok).encode("utf-8"))
    assert "SalesOrder" in load_semantic_layer(tmp_path).entities


def test_metric_operand_unknown(tmp_path: Path) -> None:
    metrics = """
    metrics:
      - name: aov
        entity: SalesOrder
        description: avg
        expression: {op: divide, left: total_sales, right: nope}
    """
    out = issues_for(tmp_path, {"entities/s.yaml": SALES, "metrics/m.yaml": metrics})
    assert "operand 'nope'" in out


def test_metric_cycle(tmp_path: Path) -> None:
    metrics = """
    metrics:
      - {name: m1, entity: SalesOrder, description: a, expression: {op: add, left: m2, right: total_sales}}
      - {name: m2, entity: SalesOrder, description: b, expression: {op: add, left: m1, right: total_sales}}
    """
    out = issues_for(tmp_path, {"entities/s.yaml": SALES, "metrics/m.yaml": metrics})
    assert "circular" in out


def test_example_with_unknown_measure(tmp_path: Path) -> None:
    examples = """
    examples:
      - id: ex-1
        question: "What are total sales?"
        intent: aggregation
        entity: SalesOrder
        measures: [ghost_measure]
    """
    out = issues_for(tmp_path, {"entities/s.yaml": SALES, "examples/e.yaml": examples})
    assert "unknown measure SalesOrder.ghost_measure" in out


def test_glossary_term_with_unknown_target(tmp_path: Path) -> None:
    synonyms = """
    terms:
      - {term: revenue, definition: net sales, maps_to: {entity: SalesOrder, measure: revenue}}
    """
    out = issues_for(tmp_path, {"entities/s.yaml": SALES, "synonyms/g.yaml": synonyms})
    assert "unknown measure SalesOrder.revenue" in out


def test_invalid_yaml(tmp_path: Path) -> None:
    out = issues_for(tmp_path, {"entities/s.yaml": "entity: [unclosed"})
    assert "invalid YAML" in out


def test_yaml_is_parsed_safely(tmp_path: Path) -> None:
    out = issues_for(
        tmp_path, {"entities/s.yaml": "!!python/object/apply:os.system ['echo pwned']"}
    )
    assert "invalid YAML" in out


def test_oversize_file(tmp_path: Path) -> None:
    out = issues_for(tmp_path, {"entities/s.yaml": "#" * (MAX_FILE_BYTES + 1)})
    assert "exceeds" in out


def test_sum_on_non_numeric_type(tmp_path: Path) -> None:
    bad = SALES.replace(
        "{name: total_sales, field: net_amount, aggregation: sum, format: currency}",
        "{name: total_sales, field: net_amount, aggregation: sum, type: string}",
    )
    out = issues_for(tmp_path, {"entities/s.yaml": bad})
    assert "requires a numeric type" in out


def test_logical_name_must_be_allowed(tmp_path: Path) -> None:
    bad = SALES.replace(
        "allowed_database_objects: [analytics_sales_orders]",
        "allowed_database_objects: [other_view]",
    )
    out = issues_for(tmp_path, {"entities/s.yaml": bad})
    assert "logical_name must be one of allowed_database_objects" in out


@pytest.mark.parametrize("physical", ["region_name; DROP TABLE x", "a.b", '"quoted"', "has space"])
def test_physical_names_are_plain_identifiers(tmp_path: Path, physical: str) -> None:
    bad = SALES.replace("field: region_name", f"field: '{physical}'")
    assert "String should match pattern" in issues_for(tmp_path, {"entities/s.yaml": bad})


def test_unknown_keys_are_rejected(tmp_path: Path) -> None:
    bad = SALES + "\nsql: SELECT * FROM users\n"
    assert "Extra inputs are not permitted" in issues_for(tmp_path, {"entities/s.yaml": bad})


def test_content_hash_is_stable_and_sensitive_to_edits(tmp_path: Path) -> None:
    root = write_layer(tmp_path, {"entities/sales.yaml": SALES})
    first = load_semantic_layer(root).content_hash
    assert load_semantic_layer(root).content_hash == first
    (root / "entities/sales.yaml").write_bytes(
        textwrap.dedent(
            SALES.replace("synonyms: [territory]", "synonyms: [territory, zone]")
        ).encode("utf-8")
    )
    assert load_semantic_layer(root).content_hash != first
