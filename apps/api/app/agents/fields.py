"""Semantic field selection: check the planner's fields against the question.

A planner - a model, or the development stand-in - can pick a field only because its
name shares a word with the question ("lead *analysis*" → ``analysis_note``). Before
the plan is validated:

* free-text and identifier fields are dropped unless the question names them;
* every "by <term>" must be answered by a breakdown field - matched through names,
  synonyms and a few equivalences (status ↔ stage); a term the data cannot answer
  becomes a clarification, never a different breakdown;
* value terms ("lead value", "amount", "revenue") must map to a real measure - a metric
  the data does not have is reported as unavailable, never replaced by another;
* "number of leads" over lead events counts distinct leads (``count_distinct`` of the
  key), never event rows.

``retarget`` runs first: when the question names an entity ("show *leads* by status")
and the planner chose another one (bids, which also have a status), the plan is rebuilt
on the named entity.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Final

from app.agents.entities import Mention, plural_noun, singular
from app.query.plans import QueryPlan
from app.rag.text import tokenize
from app.semantic.loader import SemanticBundle
from app.semantic.models import Dimension, Entity, Measure
from app.semantic.roles import dimension_role

_GROUP_CUE: Final = re.compile(
    r"\b(?:by|per|across)\s+(?:each\s+|every\s+)?(?P<term>[a-z][a-z0-9 _-]{0,40}?)"
    r"(?=$|[?.!,;]|\s+(?:for|in|during|from|since|this|last|over|and|with|where|vs|versus"
    r"|of|compared|sorted|top|then)\b)",
    re.IGNORECASE,
)
_COUNT_CUE: Final = re.compile(
    r"\b(?:number\s+of|how\s+many|count\s+of)\s+(?:distinct\s+|unique\s+|the\s+)?"
    r"(?P<noun>[a-z][a-z ]{0,40}?)"
    r"(?=$|[?.!,;]|\s+(?:with|by|per|across|for|in|that|which|who|have|has|had|from|during"
    r"|this|last|over|and|where|are|were|is)\b)",
    re.IGNORECASE,
)
_TIME_TERMS: Final = frozenset(
    {
        "day",
        "daily",
        "week",
        "weekly",
        "month",
        "monthly",
        "quarter",
        "quarterly",
        "year",
        "yearly",
        "date",
        "time",
        "period",
    }
)
# Words people use for the same business field (both directions).
EQUIVALENTS: Final[dict[str, frozenset[str]]] = {
    "status": frozenset({"stage", "phase", "verdict", "decision"}),
    "stage": frozenset({"status", "phase", "funnel"}),
    "owner": frozenset({"salesperson", "assigned", "user", "rep", "executive", "agent"}),
    "salesperson": frozenset({"owner", "assigned", "user", "rep", "executive", "agent"}),
    "source": frozenset({"channel", "origin"}),
    "channel": frozenset({"source", "origin"}),
    "industry": frozenset({"sector", "segment", "nature"}),
    "sector": frozenset({"industry", "segment"}),
    "segment": frozenset({"sector", "industry"}),
    "region": frozenset({"zone", "area", "territory"}),
    "priority": frozenset({"tier", "urgency"}),
    "category": frozenset({"type", "kind", "class"}),
    "type": frozenset({"category", "kind"}),
}
VALUE_TERMS: Final = frozenset(
    {
        "value",
        "amount",
        "revenue",
        "worth",
        "price",
        "potential",
        "budget",
        "deal",
        "turnover",
        "margin",
        "profit",
        "cost",
    }
)
_VALUE_EQUIVALENTS: Final[dict[str, frozenset[str]]] = {
    "value": frozenset({"amount", "worth", "revenue", "potential"}),
    "amount": frozenset({"value", "worth"}),
    "revenue": frozenset({"sales", "turnover", "income"}),
    "worth": frozenset({"value", "amount"}),
}


@dataclass
class FieldSelection:
    plan: QueryPlan
    notes: list[str] = field(default_factory=list)  # shown with the answer
    clarification: str | None = None


def _singular_tokens(text: str) -> set[str]:
    return {singular(t) for t in tokenize(text)}


def _names(dim: Dimension | Measure) -> list[str]:
    return [dim.name, *dim.synonyms]


def explicitly_named(dim: Dimension, question: str) -> bool:
    """All words of the field's name (or a synonym) appear in the question."""
    q = _singular_tokens(question)
    return any((tokens := _singular_tokens(n)) and tokens <= q for n in _names(dim))


def _field_label(name: str) -> str:
    return name.replace("_", " ")


def _entity_label(entity: Entity) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", entity.entity)


def group_terms(question: str) -> list[str]:
    """ "by status", "per source" → ["status", "source"] (time grains excluded)."""
    terms: list[str] = []
    for match in _GROUP_CUE.finditer(question):
        term = match.group("term").strip().lower()
        words = term.split()
        if words and words[-1] in _TIME_TERMS:
            continue
        if term and term not in terms:
            terms.append(term)
    return terms


# Name matches at least this strong (the term is the field's name, or part of it)
# beat a planner's guess for the term; weaker ones (equivalent words) do not.
_NAMED: Final = 6


def match_dimension(term: str, entity: Entity) -> Dimension | None:
    """The breakdown field a word refers to ("status" → ``stage``), if any."""
    scored = _scored_dimension(term, entity)
    return scored[1] if scored else None


def _scored_dimension(term: str, entity: Entity) -> tuple[int, Dimension] | None:
    term_tokens = _singular_tokens(term)
    if not term_tokens:
        return None
    best: tuple[int, Dimension] | None = None
    for dim in entity.dimensions:
        if dim.sensitive:
            continue
        role = dimension_role(dim)
        score = 0
        for name in _names(dim):
            tokens = _singular_tokens(name)
            if tokens == term_tokens:
                score = max(score, 10)
            elif term_tokens <= tokens:
                score = max(score, 6)
            elif any(tokens & EQUIVALENTS.get(t, frozenset()) for t in term_tokens):
                score = max(score, 4)
        if score == 0:
            continue
        if role == "category":
            score += 1
        elif role in {"free_text", "identifier"} and score < 10:
            continue  # notes and keys only when named exactly
        if best is None or score > best[0]:
            best = (score, dim)
    return best


def _measure_matches(measure: Measure, term: str) -> bool:
    wanted = {term} | _VALUE_EQUIVALENTS.get(term, frozenset())
    return any(_singular_tokens(n) & wanted for n in _names(measure))


# ------------------------------------------------------------------ subject
def entity_phrases(entity: Entity) -> list[list[str]]:
    """What an entity is called, as singular words: "leads" → [lead], its synonyms."""
    phrases: list[list[str]] = []
    for phrase in (plural_noun(entity.entity), *entity.synonyms):
        words = [singular(t) for t in tokenize(phrase, keep_stopwords=True)]
        if words and words not in phrases:
            phrases.append(words)
    return phrases


def _contains(words: list[str], phrase: list[str]) -> bool:
    n = len(phrase)
    return any(words[i : i + n] == phrase for i in range(len(words) - n + 1))


def named_subject(question: str, bundle: SemanticBundle, usable: Iterable[str]) -> str | None:
    """The entity the question names ("show *leads* by status"): the longest entity name
    outside "by <field>" phrases ("lead candidates" beats "leads"). ``None`` when no
    entity, or several equally, are named."""
    words = [singular(t) for t in tokenize(_GROUP_CUE.sub(" ", question), keep_stopwords=True)]
    found: dict[str, int] = {}
    for name in sorted(usable):
        entity = bundle.entities.get(name)
        if entity is None:
            continue
        for phrase in entity_phrases(entity):
            if _contains(words, phrase):
                found[name] = max(found.get(name, 0), len(phrase))
    if not found:
        return None
    longest = max(found.values())
    winners = [name for name, n in found.items() if n == longest]
    return winners[0] if len(winners) == 1 else None


def _entity_called(words: list[str], bundle: SemanticBundle) -> Entity | None:
    matches = [e for e in bundle.entities.values() if words in entity_phrases(e)]
    return matches[0] if len(matches) == 1 else None


def _answers(entity: Entity, term: str) -> bool:
    """Can this entity answer "by <term>" (a breakdown, a time grain or a measure)?"""
    tokens = _singular_tokens(term)
    return (
        match_dimension(term, entity) is not None
        or any(tokens <= _singular_tokens(t.name) for t in entity.time_fields)
        or any(_measure_matches(m, singular(term)) for m in entity.measures)
    )


def _related(bundle: SemanticBundle, a: str, b: str) -> bool:
    return any({j.from_entity, j.to_entity} == {a, b} for j in bundle.joins)


def _without_mention(question: str, mention: Mention | None) -> str:
    """The question minus the business name (and its "company" word): "Kiran Cables" is
    not a request for a cables entity."""
    if mention is None:
        return question
    before = question[: mention.start].rstrip()
    if mention.kind and before.lower().endswith(mention.kind):
        before = before[: -len(mention.kind)]
    return f"{before} {question[mention.end :]}"


def retarget(
    plan: QueryPlan,
    question: str,
    bundle: SemanticBundle,
    usable: frozenset[str],
    mention: Mention | None = None,
) -> tuple[QueryPlan, str | None]:
    """Answer from the entity the question names when the planner chose another.

    "show leads by status" is answered from leads - not from bids because bids have a
    status. The planner's entity stays when the question names it; when the plan groups
    by the named subject ("top customers" → sales orders by customer); or when it is
    joined to the named entity and answers a "by <term>" the named one cannot ("number
    of leads by event type" → lead events). Returns the plan and why it was changed.
    """
    if not plan.is_data_plan or plan.entity is None or plan.entity not in bundle.entities:
        return plan, None
    text = _without_mention(question, mention)
    named = named_subject(text, bundle, usable)
    if named is None or named == plan.entity:
        return plan, None
    current, target = bundle.entities[plan.entity], bundle.entities[named]
    if _groups_by_subject(plan, current, target):
        return plan, None
    unanswered = [t for t in group_terms(text) if not _answers(target, t)]
    if (
        unanswered
        and _related(bundle, current.entity, target.entity)
        and all(_answers(current, t) for t in unanswered)
    ):
        return plan, None
    rebuilt = _rebuild(plan, current, target)
    if rebuilt is None:
        return plan, None
    reason = f"the question names {plural_noun(target.entity)}, not {plural_noun(current.entity)}"
    return rebuilt, reason


def _groups_by_subject(plan: QueryPlan, current: Entity, target: Entity) -> bool:
    """Does the plan break its facts down by the named subject itself ("customer" or
    "customer_name" for "top customers" - not "lead_source" for "leads"), directly or
    through a joined field of that entity?"""
    phrases = entity_phrases(target)
    for ref in plan.dimensions:
        if "." in ref:
            if ref.split(".", 1)[0] == target.entity:
                return True
            continue
        dim = next((d for d in current.dimensions if d.name == ref), None)
        if dim is None or dimension_role(dim) == "identifier":
            continue
        words = [singular(t) for t in tokenize(dim.name, keep_stopwords=True)]
        for phrase in phrases:
            if words[: len(phrase)] == phrase and words[len(phrase) :] in ([], ["name"], ["title"]):
                return True
    return False


def dropped_filters(original: QueryPlan, rebuilt: QueryPlan) -> list[str]:
    """Filters a re-targeted plan could not keep - said, never silently widened."""
    return list(dict.fromkeys(f.field for f in original.filters if f not in rebuilt.filters))


def _rebuild(plan: QueryPlan, current: Entity, target: Entity) -> QueryPlan | None:
    """The plan on the named entity: same-named fields, counts as its count measure;
    breakdowns are then derived from the question again (``select_fields``)."""
    dims = {d.name for d in target.dimensions}
    times = {t.name for t in target.time_fields}
    own = {m.name for m in target.measures}
    count = _count_measure(target)
    measures: list[str] = []
    for ref in plan.measures:
        if "." in ref:
            continue
        source = next((m for m in current.measures if m.name == ref), None)
        if ref in own:
            pick = ref
        elif source is not None and source.aggregation.value in {"count", "count_distinct"}:
            if count is None:
                continue
            pick = count.name
        else:
            continue
        if pick not in measures:
            measures.append(pick)
    kept = [r for r in plan.dimensions if r in dims or r in times]
    if not measures and not kept:
        if count is None:
            return None
        measures = [count.name]
    filters = [f for f in plan.filters if f.field in dims or f.field in times]
    date_range = plan.date_range
    if date_range is not None and date_range.field not in times:
        date_range = date_range.model_copy(update={"field": None})
    comparison = plan.comparison
    if comparison is not None:
        comparison = comparison.model_copy(
            update={
                side: getattr(comparison, side).model_copy(update={"field": None})
                for side in ("current", "previous")
                if getattr(comparison, side).field not in times
            }
        )
    sort = plan.sort if plan.sort is not None and plan.sort.by in {*measures, *kept} else None
    updated = plan.model_copy(
        update={
            "entity": target.entity,
            "dimensions": kept,
            "measures": measures,
            "filters": filters,
            "date_range": date_range,
            "comparison": comparison,
            "sort": sort,
        }
    )
    return QueryPlan.model_validate(updated.model_dump())


def _count_measure(entity: Entity) -> Measure | None:
    return next((m for m in entity.measures if m.aggregation.value == "count"), None)


def select_fields(
    plan: QueryPlan, entity: Entity, question: str, bundle: SemanticBundle
) -> FieldSelection:
    """Apply the three checks; may rewrite the plan or ask for clarification.

    Only fields this step judged *irrelevant* are removed. Anything unknown, forbidden
    or sensitive is left in place so the validator refuses it (fail closed, audited).
    """
    if not plan.is_data_plan or plan.entity != entity.entity:
        return FieldSelection(plan)
    notes: list[str] = []
    dims, dropped = _drop_unnamed(plan.dimensions, entity, question, bundle, notes)
    dims, missing = _answer_group_terms(dims, entity, question, bundle, notes)
    if missing is not None:
        return FieldSelection(plan, notes, clarification=missing)
    measures, missing = _require_value_measures(plan.measures, entity, question, bundle, notes)
    if missing is not None:
        return FieldSelection(plan, notes, clarification=missing)
    measures = _count_what_is_named(measures, entity, question, bundle, notes)
    sort = plan.sort if plan.sort is None or plan.sort.by not in dropped else None
    if not dims and not measures:
        count = next((m for m in entity.measures if m.aggregation.value == "count"), None)
        if count is None:
            return FieldSelection(plan, notes, clarification="Which measure should I show?")
        measures = [count.name]
    updated = plan.model_copy(
        update={"dimensions": dims[:8], "measures": measures[:10], "sort": sort}
    )
    return FieldSelection(QueryPlan.model_validate(updated.model_dump()), notes)


def _drop_unnamed(
    refs: list[str], entity: Entity, question: str, bundle: SemanticBundle, notes: list[str]
) -> tuple[list[str], set[str]]:
    """1. Free text and identifiers are not breakdowns unless the user named them."""
    kept: list[str] = []
    dropped: set[str] = set()
    for ref in refs:
        dim = _dimension(entity, ref, bundle)
        if dim is not None and not _restricted(dim, entity, bundle):
            role = dimension_role(dim)
            if role in {"free_text", "identifier"} and not explicitly_named(dim, question):
                kind = "free text" if role == "free_text" else "an identifier"
                notes.append(f"{_field_label(dim.name).capitalize()} was not used: it is {kind}.")
                dropped.add(ref)
                continue
        kept.append(ref)
    return kept, dropped


def _restricted(dim: Dimension, entity: Entity, bundle: SemanticBundle) -> bool:
    """Policy-restricted fields are the validator's to refuse, never quietly dropped."""
    return (
        dim.sensitive
        or dim.field in entity.security.forbidden_fields
        or bundle.policy.is_forbidden(dim.name)
        or bundle.policy.is_forbidden(dim.field)
    )


def _answer_group_terms(
    dims: list[str], entity: Entity, question: str, bundle: SemanticBundle, notes: list[str]
) -> tuple[list[str], str | None]:
    """2. Every "by <term>" is answered by a field - or the user is asked.

    A field named like the term wins ("by analysis note" → ``analysis_note``, even if
    the planner chose ``analysis_source``: the planner's misread is dropped). A planner
    may also map a word semantically ("outlet" → branch): a term no field is named like
    is answered by a breakdown the planner chose that no other term claims, and only
    then by an equivalent word (status ↔ verdict).
    """
    dims = list(dims)
    terms = group_terms(question)
    unclaimed = [
        ref
        for ref in dims
        if not any(_dimension_matches(entity, ref, t, bundle) for t in terms)
        and not _is_time(entity, ref)
    ]
    for term in terms:
        if any(_dimension_matches(entity, ref, term, bundle) for ref in dims):
            continue
        if any(_measure_matches(m, singular(term)) for m in entity.measures):
            continue  # "by revenue" orders by a measure
        time = next(
            (t for t in entity.time_fields if _singular_tokens(term) <= _singular_tokens(t.name)),
            None,
        )
        if time is not None:
            if time.name not in dims:
                dims.append(time.name)
            continue
        scored = _scored_dimension(term, entity)
        if scored is not None and scored[0] >= _NAMED:
            if unclaimed:  # the planner's breakdown for this term was another field
                dims.remove(unclaimed.pop(0))
            _add_dimension(dims, scored[1], term, notes)
            continue
        if unclaimed:
            ref = unclaimed.pop(0)
            notes.append(f"“{term.capitalize()}” was read as {_field_label(ref)}.")
            continue
        if scored is None:
            return dims, (
                f"The {_entity_label(entity).lower()} data has no “{term}” field, so I can't "
                f"break it down that way. Available breakdowns: {_breakdowns(entity)}."
            )
        _add_dimension(dims, scored[1], term, notes)
    return dims, None


def _add_dimension(dims: list[str], dim: Dimension, term: str, notes: list[str]) -> None:
    """Group by ``dim``; say so when the question used another word for it."""
    if dim.name in dims:
        return
    if not _singular_tokens(term) <= _singular_tokens(dim.name):
        notes.append(f"“{term.capitalize()}” was read as {_field_label(dim.name)}.")
    dims.append(dim.name)


def _is_time(entity: Entity, ref: str) -> bool:
    return any(t.name == ref for t in entity.time_fields)


def _require_value_measures(
    refs: list[str], entity: Entity, question: str, bundle: SemanticBundle, notes: list[str]
) -> tuple[list[str], str | None]:
    """3. A requested value must exist as a measure - never substituted by another.

    When some requested values exist ("sales and profit"), the answer shows those and
    says which are unavailable; when none does ("lead value"), the user is told so.
    """
    measures = list(refs)
    unavailable: list[str] = []
    for term in sorted(_singular_tokens(question) & VALUE_TERMS):
        if any(_measure_ref_matches(entity, ref, term, bundle) for ref in measures):
            continue
        found = next((m for m in entity.measures if _measure_matches(m, term)), None)
        if found is None:
            unavailable.append(term)
            continue
        measures = [found.name, *[m for m in measures if m != found.name]]
    if not unavailable:
        return measures, None
    label = _entity_label(entity).lower()
    asked = _singular_tokens(question)
    others = [
        m
        for m in entity.measures
        if m.name in {r.split(".", 1)[-1] for r in measures} and _singular_tokens(m.name) & asked
    ]
    missing = " or ".join(unavailable)
    if others:
        notes.append(f"The {label} data has no {missing} field, so it is not shown.")
        return measures, None
    return measures, (
        f"The {label} data has no {missing} or amount field, so it cannot be calculated. "
        f"Available measures: {_measure_list(entity)}."
    )


def _count_what_is_named(
    refs: list[str], entity: Entity, question: str, bundle: SemanticBundle, notes: list[str]
) -> list[str]:
    """4. "number of leads" over lead events counts distinct leads, not event rows: the
    distinct count of the joined key replaces the row count (COUNT(DISTINCT leads_id))."""
    measures = list(refs)
    for match in _COUNT_CUE.finditer(question):
        words = [singular(t) for t in tokenize(match.group("noun"), keep_stopwords=True)]
        counted = _entity_called(words, bundle)
        if counted is None or counted.entity == entity.entity:
            continue
        join = next(
            (
                j
                for j in bundle.joins
                if j.from_entity == entity.entity
                and j.to_entity == counted.entity
                and j.relationship in {"many_to_one", "one_to_one"}
                and len(j.on) == 1
            ),
            None,
        )
        if join is None:
            continue
        rows_label = plural_noun(entity.entity)
        wanted_label = plural_noun(counted.entity)
        distinct = next(
            (
                m
                for m in entity.measures
                if m.aggregation.value == "count_distinct" and m.field == join.on[0].from_field
            ),
            None,
        )
        if distinct is None:
            notes.append(
                f"The {rows_label} data has no count of distinct {wanted_label}, so "
                f"{rows_label} are counted."
            )
            continue
        plain = [r for r in measures if _is_row_count(entity, r)]
        if distinct.name in measures and not plain:
            continue
        measures = [distinct.name, *[r for r in measures if r != distinct.name and r not in plain]]
        notes.append(
            f"Counts distinct {wanted_label}: a {singular_noun_of(counted)} with several "
            f"{rows_label} counts once."
        )
    return measures


def _is_row_count(entity: Entity, ref: str) -> bool:
    measure = next((m for m in entity.measures if m.name == ref), None)
    return measure is not None and measure.aggregation.value == "count"


def singular_noun_of(entity: Entity) -> str:
    words = plural_noun(entity.entity).split()
    return " ".join([*words[:-1], singular(words[-1])])


def _dimension(entity: Entity, ref: str, bundle: SemanticBundle) -> Dimension | None:
    if "." in ref:
        other_name, name = ref.split(".", 1)
        other = bundle.entities.get(other_name)
        return next((d for d in other.dimensions if d.name == name), None) if other else None
    return next((d for d in entity.dimensions if d.name == ref), None)


def _dimension_matches(entity: Entity, ref: str, term: str, bundle: SemanticBundle) -> bool:
    dim = _dimension(entity, ref, bundle)
    if dim is None:
        time = next((t for t in entity.time_fields if t.name == ref), None)
        return time is not None and _singular_tokens(term) <= _singular_tokens(time.name)
    term_tokens = _singular_tokens(term)
    for name in _names(dim):
        tokens = _singular_tokens(name)
        if term_tokens <= tokens or any(
            tokens & EQUIVALENTS.get(t, frozenset()) for t in term_tokens
        ):
            return True
    return False


def _measure_ref_matches(entity: Entity, ref: str, term: str, bundle: SemanticBundle) -> bool:
    name = ref.split(".", 1)[-1]
    measure = next((m for m in entity.measures if m.name == name), None)
    if measure is not None:
        return _measure_matches(measure, term)
    metric = next((m for m in bundle.metrics if m.entity == entity.entity and m.name == name), None)
    return metric is not None and bool(
        _singular_tokens(metric.name) & ({term} | _VALUE_EQUIVALENTS.get(term, frozenset()))
    )


def _breakdowns(entity: Entity) -> str:
    names = [
        _field_label(d.name)
        for d in entity.dimensions
        if not d.sensitive and dimension_role(d) == "category"
    ]
    return ", ".join(names[:8]) or "none"


def _measure_list(entity: Entity) -> str:
    labels = []
    for m in entity.measures:
        label = _field_label(m.name)
        if m.aggregation.value in {"count", "count_distinct"}:
            label = "number of records" if m.name == "record_count" else label
        elif m.aggregation.value == "avg":
            label = f"average {label}"
        labels.append(label)
    return ", ".join(labels) or "none"
