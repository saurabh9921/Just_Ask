"""Cases found against the live leads data: the question's own subject wins over the
planner's guess, counts of a joined entity are distinct, an analysis is only detected
when asked for, names resolve to companies first, and narratives skip blank rows."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from app.agents.analysis import detect_analysis
from app.agents.entities import Mention, find_mention, name_targets, resolve_mention
from app.agents.fields import dropped_filters, named_subject, retarget, select_fields
from app.query.plans import QueryPlan
from app.query.result_formatter import explainable_data
from app.semantic.loader import SemanticBundle
from app.semantic.models import Entity, Join
from tests.unit.test_validation_first import _formatted


def _entity(name: str, table: str, **spec: Any) -> Entity:
    return Entity.model_validate(
        {
            "entity": name,
            "description": name,
            "source": {"logical_name": table, "allowed_database_objects": [table]},
            **spec,
        }
    )


def _join(name: str, a: str, b: str, fk: str) -> Join:
    return Join.model_validate(
        {
            "name": name,
            "from": a,
            "to": b,
            "relationship": "many_to_one",
            "on": [{"from_field": fk, "to_field": "id"}],
        }
    )


COUNT = {"name": "record_count", "field": "id", "aggregation": "count", "type": "integer"}
LEADS = _entity(
    "Leads",
    "leads",
    dimensions=[
        {"name": "id", "field": "id", "type": "integer"},
        {"name": "companies_id", "field": "companies_id", "type": "integer"},
        {"name": "stage", "field": "stage", "synonyms": ["status", "lead status"]},
        {"name": "source", "field": "source"},
        {"name": "type", "field": "type"},
    ],
    measures=[COUNT, {"name": "score", "field": "score", "aggregation": "avg"}],
    time_fields=[{"name": "created", "field": "created", "type": "timestamp"}],
    default_time_field="created",
)
BIDS = _entity(
    "Bids",
    "bids",
    dimensions=[
        {"name": "id", "field": "id", "type": "integer"},
        {"name": "status", "field": "status"},
        {"name": "desk_status", "field": "desk_status"},
    ],
    measures=[COUNT],
)
CAMPAIGNS = _entity(
    "Campaigns",
    "campaigns",
    dimensions=[
        {"name": "id", "field": "id", "type": "integer"},
        {"name": "companies_id", "field": "companies_id", "type": "integer"},
        {"name": "name", "field": "name"},
    ],
    measures=[
        COUNT,
        {"name": "book_value", "field": "book_value", "aggregation": "sum"},
        {"name": "expected_value", "field": "expected_value", "aggregation": "sum"},
    ],
    time_fields=[{"name": "start_date", "field": "start_date"}],
    default_time_field="start_date",
)
LEAD_EVENTS = _entity(
    "LeadEvents",
    "lead_events",
    dimensions=[
        {"name": "id", "field": "id", "type": "integer"},
        {"name": "leads_id", "field": "leads_id", "type": "integer"},
        {"name": "event_type", "field": "event_type"},
        {"name": "to_stage", "field": "to_stage"},
    ],
    measures=[
        COUNT,
        {"name": "lead_count", "field": "leads_id", "aggregation": "count_distinct"},
    ],
)
LEAD_CANDIDATES = _entity(
    "LeadCandidates",
    "lead_candidates",
    synonyms=["lead candidates"],
    dimensions=[
        {"name": "id", "field": "id", "type": "integer"},
        {"name": "campaigns_id", "field": "campaigns_id", "type": "integer"},
        {"name": "companies_id", "field": "companies_id", "type": "integer"},
        {"name": "analysis_note", "field": "analysis_note"},
    ],
    measures=[COUNT],
)
COMPANIES = _entity(
    "Companies",
    "companies",
    dimensions=[
        {"name": "id", "field": "id", "type": "integer"},
        {"name": "name", "field": "name"},
    ],
    measures=[COUNT],
)
SALES = _entity(
    "SalesOrder",
    "sales_orders",
    dimensions=[
        {"name": "id", "field": "id", "type": "integer"},
        {"name": "customer", "field": "customer_name"},
        {"name": "region", "field": "region"},
    ],
    measures=[COUNT, {"name": "total_sales", "field": "amount", "aggregation": "sum"}],
)
CUSTOMERS = _entity(
    "Customer",
    "customers",
    dimensions=[
        {"name": "id", "field": "id", "type": "integer"},
        {"name": "customer", "field": "customer_name"},
    ],
    measures=[COUNT],
)
BUNDLE = SemanticBundle(
    entities={
        e.entity: e
        for e in (
            LEADS,
            BIDS,
            CAMPAIGNS,
            LEAD_EVENTS,
            LEAD_CANDIDATES,
            COMPANIES,
            SALES,
            CUSTOMERS,
        )
    },
    joins=[
        _join("lead_events_to_leads", "LeadEvents", "Leads", "leads_id"),
        _join("leads_to_companies", "Leads", "Companies", "companies_id"),
        _join("campaigns_to_companies", "Campaigns", "Companies", "companies_id"),
        # Declared before the company join, as in the live layer.
        _join("candidates_to_campaigns", "LeadCandidates", "Campaigns", "campaigns_id"),
        _join("candidates_to_companies", "LeadCandidates", "Companies", "companies_id"),
    ],
)
USABLE = frozenset(BUNDLE.entities)


# ------------------------------------------------------------------- subject
def test_the_named_subject_is_the_longest_entity_name_outside_by_phrases() -> None:
    assert named_subject("show leads by status", BUNDLE, USABLE) == "Leads"
    assert named_subject("show lead value by month", BUNDLE, USABLE) == "Leads"
    assert named_subject("show lead events by stage", BUNDLE, USABLE) == "LeadEvents"
    assert named_subject("lead candidates by analysis note", BUNDLE, USABLE) == "LeadCandidates"
    # "by bid" is a breakdown, not the subject.
    assert named_subject("show leads by bid", BUNDLE, USABLE) == "Leads"
    assert named_subject("compare bids and leads", BUNDLE, USABLE) is None  # two subjects
    assert named_subject("total sales by region", BUNDLE, USABLE) is None


def test_leads_by_status_is_answered_from_leads_not_bids() -> None:
    """Live case 3: the planner chose bids (they have a "status")."""
    question = "show leads by status"
    planned = QueryPlan(
        entity="Bids", dimensions=["status", "desk_status"], measures=["record_count"]
    )
    plan, reason = retarget(planned, question, BUNDLE, USABLE)
    assert reason == "the question names leads, not bids"
    assert (plan.entity, plan.dimensions, plan.measures) == ("Leads", [], ["record_count"])
    selection = select_fields(plan, LEADS, question, BUNDLE)
    assert selection.plan.dimensions == ["stage"]  # "status" is the stage's synonym
    assert selection.notes == ["“Status” was read as stage."]


def test_a_value_leads_do_not_have_is_never_taken_from_campaigns() -> None:
    """Live case 5: the planner summed campaign values for "lead value"."""
    question = "show lead value by month"
    planned = QueryPlan(
        entity="Campaigns", measures=["book_value", "expected_value"], time_grain="month"
    )
    plan, reason = retarget(planned, question, BUNDLE, USABLE)
    assert reason is not None and plan.entity == "Leads"
    assert plan.measures == ["record_count"] and plan.time_grain is not None
    selection = select_fields(plan, LEADS, question, BUNDLE)
    assert selection.clarification == (
        "The leads data has no value or amount field, so it cannot be calculated. "
        "Available measures: number of records, average score."
    )


def test_a_related_entity_that_answers_the_breakdown_is_kept() -> None:
    """ "number of leads by event type": only lead events have an event type."""
    question = "number of leads with events by event type"
    planned = QueryPlan(entity="LeadEvents", dimensions=["event_type"], measures=["record_count"])
    plan, reason = retarget(planned, question, BUNDLE, USABLE)
    assert reason is None and plan == planned


def test_a_plan_grouped_by_the_named_subject_is_kept() -> None:
    """ "top customers" is sales orders by customer - not the customer list."""
    planned = QueryPlan(
        entity="SalesOrder",
        dimensions=["customer"],
        measures=["total_sales"],
        sort={"by": "total_sales", "direction": "desc"},
        limit=5,
    )
    assert retarget(planned, "top customers", BUNDLE, USABLE) == (planned, None)


def test_a_field_merely_prefixed_with_the_subject_is_not_the_subject() -> None:
    """ "lead_source" on lead candidates does not make them "leads"."""
    candidates = LEAD_CANDIDATES.model_copy(
        update={
            "dimensions": [
                *LEAD_CANDIDATES.dimensions,
                LEAD_CANDIDATES.dimensions[-1].model_copy(
                    update={"name": "lead_source", "field": "lead_source"}
                ),
            ]
        }
    )
    bundle = SemanticBundle(
        entities={**BUNDLE.entities, "LeadCandidates": candidates}, joins=BUNDLE.joins
    )
    planned = QueryPlan(
        entity="LeadCandidates", dimensions=["lead_source"], measures=["record_count"]
    )
    plan, reason = retarget(planned, "show leads", bundle, USABLE)
    assert reason is not None and plan.entity == "Leads"


def test_a_filter_the_named_entity_cannot_take_is_reported() -> None:
    planned = QueryPlan(
        entity="Bids",
        dimensions=["status"],
        measures=["record_count"],
        filters=[{"field": "desk_status", "op": "eq", "value": "open"}],
    )
    plan, reason = retarget(planned, "show leads by status", BUNDLE, USABLE)
    assert reason is not None and plan.filters == []
    assert dropped_filters(planned, plan) == ["desk_status"]


def test_the_planned_entity_stays_when_the_question_names_it() -> None:
    planned = QueryPlan(entity="LeadEvents", dimensions=["to_stage"], measures=["record_count"])
    assert retarget(planned, "show lead events by stage", BUNDLE, USABLE) == (planned, None)


def test_a_company_name_is_not_read_as_the_subject() -> None:
    question = "show bids for Kiran Leads Pvt Ltd"
    mention = find_mention(question)
    assert mention is not None and mention.text == "Kiran Leads Pvt Ltd"
    planned = QueryPlan(entity="Leads", measures=["record_count"])
    plan, reason = retarget(planned, question, BUNDLE, USABLE, mention)
    assert reason == "the question names bids, not leads" and plan.entity == "Bids"
    # Without removing the name, "leads" in it would tie with "bids": nothing changes.
    assert retarget(planned, question, BUNDLE, USABLE)[1] is None


def test_a_field_named_in_the_question_beats_the_planners_guess() -> None:
    """Live case 6: "by analysis note" is that field (free text, named explicitly), not
    the ``analysis_source`` the planner chose."""
    candidates = LEAD_CANDIDATES.model_copy(
        update={
            "dimensions": [
                *LEAD_CANDIDATES.dimensions,
                LEAD_CANDIDATES.dimensions[-1].model_copy(
                    update={"name": "analysis_source", "field": "analysis_source"}
                ),
            ]
        }
    )
    planned = QueryPlan(
        entity="LeadCandidates", dimensions=["analysis_source"], measures=["record_count"]
    )
    question = "show lead candidates by analysis note"
    selection = select_fields(planned, candidates, question, BUNDLE)
    assert selection.plan.dimensions == ["analysis_note"] and selection.notes == []


def test_a_planner_mapping_is_kept_when_no_field_is_named_like_the_term() -> None:
    planned = QueryPlan(entity="Leads", dimensions=["source"], measures=["record_count"])
    selection = select_fields(planned, LEADS, "show leads by outlet", BUNDLE)
    assert selection.plan.dimensions == ["source"]
    assert selection.notes == ["“Outlet” was read as source."]


# -------------------------------------------------------------------- counts
def test_number_of_leads_over_events_counts_distinct_leads() -> None:
    """Live case 8: 12 events belong to 2 leads - the answer is 2, not 12."""
    question = "number of leads with events by event type"
    planned = QueryPlan(entity="LeadEvents", dimensions=["event_type"], measures=["record_count"])
    selection = select_fields(planned, LEAD_EVENTS, question, BUNDLE)
    assert selection.plan.measures == ["lead_count"]
    assert selection.notes == [
        "Counts distinct leads: a lead with several lead events counts once."
    ]


def test_events_are_counted_as_events_unless_another_entity_is_counted() -> None:
    planned = QueryPlan(entity="LeadEvents", dimensions=["event_type"], measures=["record_count"])
    selection = select_fields(planned, LEAD_EVENTS, "number of lead events by event type", BUNDLE)
    assert selection.plan.measures == ["record_count"] and selection.notes == []


def test_without_a_distinct_count_the_answer_says_what_is_counted() -> None:
    events = LEAD_EVENTS.model_copy(update={"measures": [LEAD_EVENTS.measures[0]]})
    bundle = SemanticBundle(entities={**BUNDLE.entities, "LeadEvents": events}, joins=BUNDLE.joins)
    planned = QueryPlan(entity="LeadEvents", dimensions=["event_type"], measures=["record_count"])
    selection = select_fields(planned, events, "how many leads by event type", bundle)
    assert selection.plan.measures == ["record_count"]
    assert selection.notes == [
        "The lead events data has no count of distinct leads, so lead events are counted."
    ]


# ------------------------------------------------------------------ analysis
def test_an_analysis_word_inside_a_field_name_is_not_an_analysis_request() -> None:
    """Live case 6: "by analysis note" asks for a breakdown by that field."""
    assert detect_analysis("show lead candidates by analysis note", BUNDLE, USABLE) is None
    request = detect_analysis("give me lead analysis for pending metals co", BUNDLE, USABLE)
    assert request is not None and request.subject == "Leads"


# ----------------------------------------------------------------- resolution
def test_companies_are_looked_up_before_campaigns_named_after_them() -> None:
    """Live case 2: campaign #2 and company #2 are both "Pending Metals Co"."""
    targets = name_targets(BUNDLE, LEAD_CANDIDATES, USABLE)
    assert [(t.entity, t.filter_dim) for t in targets] == [
        ("Companies", "companies_id"),
        ("Campaigns", "campaigns_id"),
    ]


async def test_suggestions_contain_the_whole_name_when_any_does() -> None:
    """Live case 10: "solar infra" suggests the two Solar Infra companies only."""
    names = {
        1050767: "Suryodaya Solar Infra Pvt Ltd",
        1050770: "Vidyut Solar Infra LLP",
        501: "SOLAR NEDERLAND B V",
        1259: "M/s. Clean Max Solar",
    }

    async def lookup(plan: QueryPlan, label: str) -> Sequence[Sequence[Any]]:
        [f] = plan.filters
        return [(k, v) for k, v in names.items() if str(f.value) in v.lower()]

    mention = Mention("solar infra", 0, 11)
    resolution = await resolve_mention(mention, name_targets(BUNDLE, LEADS, USABLE), lookup)
    assert resolution.status == "suggestions"
    assert resolution.clarification() == (
        "I couldn't find a company named exactly “solar infra”. Did you mean: "
        "Suryodaya Solar Infra Pvt Ltd (#1050767); Vidyut Solar Infra LLP (#1050770)?"
    )


# ---------------------------------------------------------------- narratives
def test_narratives_never_see_blank_categories() -> None:
    result = _formatted([("source", "x")], [(None, 5), ("Sales prospected", 3), ("databank", 2)])
    assert explainable_data(result) == (
        ["source", "record_count"],
        [["Sales prospected", 3], ["databank", 2]],
    )


def test_when_every_breakdown_is_rejected_only_the_figures_are_narrated() -> None:
    result = _formatted(
        [("analysis_note", "x"), ("analysis_source", "y")],
        [(None, None, 28)],
        roles={"analysis_note": "free_text"},
    )
    assert explainable_data(result) == (["record_count"], [[28]])
