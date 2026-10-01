"""Validation-first answers: field roles, data-quality profiles, chart validation, entity
resolution and semantic field selection (pure logic; the API tests are in
``test_analysis_api.py``)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest

from app.agents.analysis import detect_analysis
from app.agents.entities import (
    Mention,
    NameTarget,
    find_mention,
    match_level,
    name_targets,
    normalize_name,
    resolve_mention,
    search_terms,
)
from app.agents.fields import group_terms, match_dimension, select_fields
from app.connectors.common.types import CanonicalType, QueryResult, ResultColumn
from app.query.compiler import OutputColumn, compile_plan
from app.query.plans import QueryPlan
from app.query.quality import is_blank, profile_dimension
from app.query.result_formatter import format_result
from app.query.validator import ResolvedDimension, ResolvedMeasure, ResolvedPlan, ResolvedSource
from app.semantic.loader import SemanticBundle
from app.semantic.models import Entity, Join, ValueFormat
from app.semantic.roles import infer_role

# ------------------------------------------------------------------ fixtures
LEADS = Entity.model_validate(
    {
        "entity": "Leads",
        "description": "Sales leads",
        "source": {"logical_name": "leads", "allowed_database_objects": ["leads"]},
        "dimensions": [
            {"name": "id", "field": "id", "type": "integer"},
            {"name": "companies_id", "field": "companies_id", "type": "integer"},
            {"name": "company_name", "field": "company_name"},
            {"name": "stage", "field": "stage"},
            {"name": "source", "field": "source"},
            {"name": "priority", "field": "priority"},
            {"name": "analysis_note", "field": "analysis_note"},
            {"name": "bank_account", "field": "bank_account", "sensitive": True},
        ],
        "measures": [
            {"name": "record_count", "field": "id", "aggregation": "count", "type": "integer"},
            {"name": "score", "field": "score", "aggregation": "avg"},
        ],
        "time_fields": [{"name": "created", "field": "created", "type": "timestamp"}],
        "default_time_field": "created",
    }
)
COMPANIES = Entity.model_validate(
    {
        "entity": "Companies",
        "description": "Company master",
        "source": {"logical_name": "companies", "allowed_database_objects": ["companies"]},
        "dimensions": [
            {"name": "id", "field": "id", "type": "integer"},
            {"name": "name", "field": "name"},
        ],
        "measures": [{"name": "record_count", "field": "id", "aggregation": "count"}],
    }
)
BUNDLE = SemanticBundle(
    entities={"Leads": LEADS, "Companies": COMPANIES},
    joins=[
        Join.model_validate(
            {
                "name": "leads_to_companies",
                "from": "Leads",
                "to": "Companies",
                "relationship": "many_to_one",
                "on": [{"from_field": "companies_id", "to_field": "id"}],
            }
        )
    ],
)


# ---------------------------------------------------------------------- roles
@pytest.mark.parametrize(
    ("name", "role"),
    [
        ("analysis_note", "free_text"),
        ("reject_note", "free_text"),
        ("notes", "free_text"),
        ("description", "free_text"),
        ("companies_id", "identifier"),
        ("users_id", "identifier"),
        ("id", "identifier"),
        ("name_key", "identifier"),
        ("company_name", "name"),
        ("display_name", "name"),
        ("stage", "category"),
        ("prospect_status", "category"),
        ("analysis_source", "category"),
    ],
)
def test_field_roles_are_inferred_from_names(name: str, role: str) -> None:
    assert infer_role(name) == role


# -------------------------------------------------------------------- quality
def test_null_blank_and_placeholder_values_are_not_categories() -> None:
    for value in (None, "", "   ", "NULL", "n/a", "N/A", "-", "None"):
        assert is_blank(value), value
    for value in ("qualified", "Unknown", 0, False):
        assert not is_blank(value), value


def test_profile_is_weighted_by_record_counts() -> None:
    q = profile_dimension(
        name="source",
        label="Source",
        role="category",
        values=[None, "", " ", "N/A", "NULL", "databank"],
        weights=[10, 1, 1, 1, 1, 6],
        max_blank_ratio=0.5,
    )
    assert (q.total, q.null_count, q.blank_count, q.distinct_values) == (20, 10, 4, 1)
    assert q.meaningful_ratio == 0.3 and not q.usable
    assert q.reason == "mostly blank (70% missing)"


def test_half_missing_is_still_usable_and_reported() -> None:
    q = profile_dimension(
        name="source",
        label="Source",
        role="category",
        values=[None, "a", "b"],
        weights=[10, 6, 4],
        max_blank_ratio=0.5,
    )
    assert q.usable and q.missing == 10


# ---------------------------------------------------------- chart validation
def _formatted(
    dims: list[tuple[str, str]],
    rows: list[tuple[Any, ...]],
    *,
    roles: dict[str, str] | None = None,
) -> Any:
    """format_result over a hand-built grouped count result (dims..., record_count)."""
    roles = roles or {}
    resolved_dims = [
        ResolvedDimension(
            label=n,
            ref=n,
            source="t0",
            field=n,
            type=CanonicalType.STRING,
            role=roles.get(n, "category"),  # type: ignore[arg-type]
        )
        for n, _ in dims
    ]
    count = ResolvedMeasure(
        label="record_count",
        ref="record_count",
        format=ValueFormat.INTEGER,
        source="t0",
        field="id",
        aggregation="count",
        type=CanonicalType.INTEGER,
    )
    resolved = ResolvedPlan(
        plan=QueryPlan(entity="LeadCandidates", measures=["record_count"]),
        entity="LeadCandidates",
        sources=[ResolvedSource("t0", "LeadCandidates", "public", "lead_candidates", {})],
        dimensions=resolved_dims,
        measures=[count],
        filters=[],
        row_filters=[],
        time_filter=None,
        sort=[],
        limit=100,
    )
    output = [
        OutputColumn(
            name=n,
            ref=n,
            kind="dimension",
            type=CanonicalType.STRING,
            role=roles.get(n, "category"),
        )
        for n, _ in dims
    ] + [
        OutputColumn(
            name="record_count",
            ref="record_count",
            kind="measure",
            type=CanonicalType.INTEGER,
            format=ValueFormat.INTEGER,
            aggregation="count",
        )
    ]
    result = QueryResult(
        columns=[ResultColumn(name=c.name) for c in output],
        rows=rows,
        row_count=len(rows),
        truncated=False,
        elapsed_ms=1.0,
    )
    return format_result(
        resolved,
        output,
        result,
        None,
        BUNDLE.policy,
        max_blank_ratio=0.5,
        noun="lead candidates",
    )


def test_the_reported_blank_chart_is_rejected() -> None:
    """The production failure: analysis_note/analysis_source NULL for all 28 rows."""
    result = _formatted(
        [("analysis_note", "x"), ("analysis_source", "y")],
        [(None, None, 28)],
        roles={"analysis_note": "free_text"},
    )
    chart = result.chart
    assert chart.enabled is False and chart.type == "kpi"  # the total instead of a chart
    assert any("free text" in r for r in chart.validation.reasons)
    assert any("only blank values" in r for r in chart.validation.reasons)
    assert "Analysis note is not recorded for any of the 28 lead candidates." in result.warnings
    assert "Analysis source is not recorded for any of the 28 lead candidates." in result.warnings
    assert not any(q.usable for q in result.quality)


def test_a_single_category_is_not_a_chart() -> None:
    result = _formatted([("stage", "x")], [("qualified", 20)])
    assert result.chart.enabled is False
    assert result.chart.validation.reasons == [
        "Stage has a single value, so there is nothing to compare"
    ]


def test_a_meaningful_breakdown_is_charted_and_missing_values_reported() -> None:
    # As in the live data: source is missing for half of the records.
    result = _formatted([("source", "x")], [(None, 5), ("Sales prospected", 3), ("databank", 2)])
    assert result.chart.enabled is True and result.chart.type == "bar"
    assert result.chart.title == "Record count by source"
    assert result.warnings == ["Source is missing for 5 of 10 lead candidates."]


def test_mostly_blank_breakdowns_are_rejected() -> None:
    result = _formatted([("priority", "x")], [(None, 18), ("high", 1), ("low", 1)])
    assert result.chart.enabled is False
    assert result.chart.validation.reasons == ["Priority is mostly blank (90% missing)"]


def test_counts_over_joins_count_each_key_once() -> None:
    resolved = ResolvedPlan(
        plan=QueryPlan(entity="Leads", measures=["record_count"]),
        entity="Leads",
        sources=[
            ResolvedSource("t0", "Leads", None, "leads", {}),
            ResolvedSource("t1", "Companies", None, "companies", {}, (("companies_id", "id"),)),
        ],
        dimensions=[
            ResolvedDimension(
                label="companies__name",
                ref="Companies.name",
                source="t1",
                field="name",
                type=CanonicalType.STRING,
                role="name",
            )
        ],
        measures=[
            ResolvedMeasure(
                label="record_count",
                ref="record_count",
                format=ValueFormat.INTEGER,
                source="t0",
                field="id",
                aggregation="count",
                type=CanonicalType.INTEGER,
            )
        ],
        filters=[],
        row_filters=[],
        time_filter=None,
        sort=[],
        limit=10,
    )
    sql = str(compile_plan(resolved, "sqlite").statement.compile())
    assert "count(DISTINCT t0.id)" in sql
    single = ResolvedPlan(
        **{**resolved.__dict__, "sources": resolved.sources[:1], "dimensions": []}
    )
    assert "count(t0.id)" in str(compile_plan(single, "sqlite").statement.compile())


# ------------------------------------------------------------------- mentions
VOCAB = frozenset({"leads", "lead", "stage", "source", "region", "status", "month", "value"})


@pytest.mark.parametrize(
    ("question", "name"),
    [
        ("give me lead analysis for pending metals co", "pending metals co"),
        ("show pending leads for pending metals co", "pending metals co"),
        ("analysis of Kiran Cables & Conductors", "Kiran Cables & Conductors"),
        ("sales for last month for Acme Traders", "Acme Traders"),
        ("revenue for North region", "North"),
        ("lead analysis for “Pending Metals Co”?", "Pending Metals Co"),
        ("show leads by status", None),
        ("leads for each region", None),
        ("show total sales by region for the last six months", None),
        ("show lead value by month", None),
    ],
)
def test_the_named_business_entity_is_found(question: str, name: str | None) -> None:
    mention = find_mention(question, VOCAB)
    assert (mention.text if mention else None) == name


def test_a_company_can_be_named_by_its_id() -> None:
    mention = find_mention("lead analysis for company 1050769", VOCAB)
    assert mention is not None and mention.by_id == 1050769 and mention.kind == "company"


def test_names_compare_without_case_punctuation_or_legal_suffixes() -> None:
    assert normalize_name("RAMKRISHNA FORGINGS LIMITED") == normalize_name(
        "Ramkrishna Forgings Ltd"
    )
    assert normalize_name("Kiran Cables & Conductors") == "kiran cables and conductors"
    assert match_level("Pending Metals Co", "pending metals co") == "exact"
    assert match_level("Kiran Cables Private Limited", "kiran cables") == "normalized"
    assert match_level("Kiran Cables & Conductors", "kiran cables") == "partial"
    assert search_terms("pending metals co") == ["pending metals co", "pending metals"]


# ----------------------------------------------------------------- resolution
class FakeLookup:
    """Plays the database for a name lookup: rows (id, name) containing the term."""

    def __init__(self, names: dict[int, str]) -> None:
        self.names = names
        self.plans: list[QueryPlan] = []

    async def __call__(self, plan: QueryPlan, label: str) -> Sequence[Sequence[Any]]:
        self.plans.append(plan)
        [f] = plan.filters
        if f.op.value == "eq":
            return [(k, v) for k, v in self.names.items() if k == f.value]
        return [(k, v) for k, v in self.names.items() if str(f.value) in v.lower()]


TARGETS = name_targets(BUNDLE, LEADS, frozenset({"Leads", "Companies"}))


def test_name_targets_prefer_the_master_entity_and_filter_by_its_key() -> None:
    assert TARGETS[0] == NameTarget(
        entity="Companies",
        name_dim="name",
        key_dim="id",
        filter_dim="companies_id",
        via_key=True,
        noun="company",
    )
    assert TARGETS[-1].entity == "Leads" and TARGETS[-1].filter_dim == "company_name"


async def _resolve(text: str, names: dict[int, str], by_id: int | None = None) -> Any:
    return await resolve_mention(
        Mention(text, 0, len(text), by_id=by_id), TARGETS[:1], FakeLookup(names)
    )


async def test_an_exact_name_resolves_to_a_key_filter() -> None:
    r = await _resolve("pending metals co", {2: "Pending Metals Co", 9728: "Uttam Metals Copper"})
    assert r.resolved and r.match == "exact"
    assert r.filter is not None and r.filter.field == "companies_id" and r.filter.value == [2]


async def test_two_companies_with_the_same_name_are_ambiguous() -> None:
    r = await _resolve(
        "Kiran Cables & Conductors",
        {1050769: "Kiran Cables & Conductors", 1050772: "Kiran Cables & Conductors"},
    )
    assert r.status == "ambiguous" and r.filter is None
    assert "(#1050769)" in r.clarification() and "(#1050772)" in r.clarification()


async def test_a_normalized_match_with_look_alikes_is_ambiguous() -> None:
    r = await _resolve(
        "kiran cables",
        {1021399: "Kiran Cables Private Limited", 1050769: "Kiran Cables & Conductors"},
    )
    assert r.status == "ambiguous"


async def test_a_unique_normalized_match_resolves() -> None:
    r = await _resolve("ramkrishna forgings", {1584: "RAMKRISHNA FORGINGS LIMITED"})
    assert r.resolved and r.match == "normalized" and r.filter.value == [1584]  # type: ignore[union-attr]


async def test_partial_matches_are_only_suggestions() -> None:
    r = await _resolve(
        "solar infra", {1050767: "Suryodaya Solar Infra Pvt Ltd", 1050770: "Vidyut Solar Infra LLP"}
    )
    assert r.status == "suggestions" and r.filter is None
    assert "Did you mean" in (r.clarification() or "")


async def test_unknown_names_are_not_found() -> None:
    r = await _resolve("zyx nonexistent traders", {2: "Pending Metals Co"})
    assert r.status == "not_found"
    assert r.clarification() == (
        "I couldn't find a company named “zyx nonexistent traders”. "
        "Check the spelling, or try part of the name."
    )


async def test_a_company_id_resolves_directly() -> None:
    r = await _resolve("company 2", {2: "Pending Metals Co"}, by_id=2)
    assert r.resolved and r.match == "id" and r.chosen[0].name == "Pending Metals Co"


# ------------------------------------------------------------- field selection
def test_free_text_is_never_picked_just_because_it_shares_a_word() -> None:
    plan = QueryPlan(entity="Leads", measures=["record_count"], dimensions=["analysis_note"])
    selection = select_fields(plan, LEADS, "give me lead analysis", BUNDLE)
    assert selection.plan.dimensions == []
    assert selection.notes == ["Analysis note was not used: it is free text."]


def test_a_named_free_text_field_is_kept() -> None:
    plan = QueryPlan(entity="Leads", measures=["record_count"], dimensions=["analysis_note"])
    assert select_fields(plan, LEADS, "leads by analysis note", BUNDLE).plan.dimensions == [
        "analysis_note"
    ]


def test_by_status_means_the_stage_field() -> None:
    assert group_terms("show leads by status for this month") == ["status"]
    assert match_dimension("status", LEADS).name == "stage"  # type: ignore[union-attr]
    plan = QueryPlan(entity="Leads", measures=["record_count"])
    selection = select_fields(plan, LEADS, "show leads by status", BUNDLE)
    assert selection.plan.dimensions == ["stage"]
    assert selection.notes == ["“Status” was read as stage."]


def test_a_breakdown_the_data_cannot_answer_is_a_question() -> None:
    plan = QueryPlan(entity="Leads", measures=["record_count"])
    selection = select_fields(plan, LEADS, "show leads by salesperson", BUNDLE)
    assert selection.clarification is not None
    assert selection.clarification.startswith("The leads data has no “salesperson” field")


def test_a_missing_value_is_never_substituted() -> None:
    plan = QueryPlan(entity="Leads", measures=["record_count"], time_grain="month")
    selection = select_fields(plan, LEADS, "show lead value by month", BUNDLE)
    assert selection.clarification == (
        "The leads data has no value or amount field, so it cannot be calculated. "
        "Available measures: number of records, average score."
    )


def test_policy_violations_are_left_for_the_validator_to_refuse() -> None:
    plan = QueryPlan(
        entity="Leads",
        measures=["record_count"],
        dimensions=["bank_account"],
        sort={"by": "bank_account", "direction": "desc"},
    )
    selection = select_fields(plan, LEADS, "leads", BUNDLE)
    assert selection.plan.dimensions == ["bank_account"]  # refused (and audited) downstream
    assert selection.plan.sort is not None


# ------------------------------------------------------------------- analysis
@pytest.mark.parametrize(
    ("question", "subject"),
    [
        ("give me lead analysis for pending metals co", "Leads"),
        ("Analyze leads for Acme", "Leads"),
        ("company overview", "Companies"),
        ("show leads by status", None),
        ("lead value by month", None),
    ],
)
def test_analysis_requests_name_their_subject(question: str, subject: str | None) -> None:
    request = detect_analysis(question, BUNDLE, frozenset({"Leads", "Companies"}))
    assert (request.subject if request else None) == subject
    if request is not None and subject == "Leads":
        assert request.kind == "lead_analysis" and request.label == "Lead Analysis"
