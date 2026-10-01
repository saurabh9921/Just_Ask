"""Prompts for query planning, plan correction and result explanation.

Same defenses as ``templates.py``: fixed system rules, delimited data blocks with
unforgeable delimiters, explicit governed vocabulary, JSON-only replies. The
explanation prompt receives only *aggregated* result rows (bounded) - never raw
detail rows beyond the limit and never sensitive columns (the plan validator
already excludes them).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import date
from typing import Any, Final

from app.agents.prompts.templates import SYSTEM_RULES, context_block, neutralise, question_block
from app.llm.base import ChatMessage
from app.query.plans import FilterOp, RelativePeriod
from app.rag.hybrid_retriever import RetrievedDocument

MAX_EXPLAIN_ROWS: Final = 20

PLAN_INSTRUCTIONS: Final = f"""\
Produce a QueryPlan for the question using ONLY the vocabulary below.
- entity: one entity name. measures: measure or metric names of that entity.
- dimensions: dimension or time-field names; a field of a related entity is written
  Entity.field and is only allowed when listed under "related".
- filters: {{"field", "op", "value"}} with op in {[o.value for o in FilterOp]}.
  Filter only on dimensions, never on measures.
- date_range: {{"field": <time field>, "relative": <period>}} with period in
  {[p.value for p in RelativePeriod]} ("n" is required for last_n_days/last_n_months),
  or {{"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}}.
- comparison: {{"current": <date_range>, "previous": <date_range>}} for period-over-period
  questions (then omit date_range). time_grain (day/week/month/quarter/year) for trends.
- sort: {{"by": <selected field>, "direction": "asc"|"desc"}}; limit: 1..1000 (top-N questions).
- confidence: 0..1 - how sure you are the plan answers the question.
- If the question is ambiguous, return {{"clarification_required": true,
  "clarification_question": "..."}}.
- If it asks for restricted personal/financial identifiers or to change data, return
  {{"refuse": true, "reason": <FORBIDDEN_FIELD | SENSITIVE_FIELD | WRITE_NOT_ALLOWED |
  OUT_OF_SCOPE>}}.
Examples of good plans may appear in the context (source="query_examples")."""


def vocabulary_text(vocabulary: dict[str, dict[str, Any]]) -> str:
    lines = ["VOCABULARY (the only names you may use):"]
    for entity, info in sorted(vocabulary.items()):
        lines.append(f"- {entity}: {neutralise(str(info.get('description', '')))[:200]}")
        for key in ("measures", "dimensions", "time_fields", "related"):
            values = info.get(key) or []
            if values:
                lines.append(f"    {key}: {', '.join(map(str, values))}")
    return "\n".join(lines)


def query_plan_messages(
    question: str,
    docs: Sequence[RetrievedDocument],
    vocabulary: dict[str, dict[str, Any]],
    today: date,
    previous_plan: dict[str, Any] | None = None,
) -> list[ChatMessage]:
    previous = ""
    if previous_plan is not None:
        previous = (
            "This is a FOLLOW-UP. The previous plan (data) was:\n<<<PREVIOUS_PLAN>>>\n"
            f"{neutralise(json.dumps(previous_plan, sort_keys=True, default=str))}\n"
            "<<<END PREVIOUS_PLAN>>>\n"
            "Modify it according to the new question; keep what the question does not change.\n"
        )
    return [
        {"role": "system", "content": SYSTEM_RULES},
        {
            "role": "user",
            "content": (
                f"{PLAN_INSTRUCTIONS}\nToday is {today.isoformat()}.\n{previous}"
                f"{vocabulary_text(vocabulary)}\n{context_block(docs)}\n{question_block(question)}"
            ),
        },
    ]


def plan_correction_messages(
    base: list[ChatMessage], rejected_plan: dict[str, Any], issues: list[str]
) -> list[ChatMessage]:
    return [
        *base,
        {
            "role": "assistant",
            "content": json.dumps(rejected_plan, sort_keys=True, default=str)[:4000],
        },
        {
            "role": "user",
            "content": (
                "That plan was rejected by the validator:\n<<<ISSUES>>>\n"
                + neutralise("\n".join(f"- {i}" for i in issues[:20]))
                + "\n<<<END ISSUES>>>\nReturn a corrected QueryPlan using only the vocabulary. "
                "If it cannot be answered, return a clarification."
            ),
        },
    ]


def data_block(columns: list[str], rows: list[list[Any]]) -> str:
    header = " | ".join(neutralise(c) for c in columns)
    body = [
        " | ".join(neutralise("" if v is None else str(v))[:60] for v in row)
        for row in rows[:MAX_EXPLAIN_ROWS]
    ]
    more = (
        f"\n({len(rows) - MAX_EXPLAIN_ROWS} more rows not shown)"
        if len(rows) > MAX_EXPLAIN_ROWS
        else ""
    )
    return "<<<DATA>>>\n" + "\n".join([header, *body]) + more + "\n<<<END DATA>>>"


def explanation_messages(
    question: str,
    description: str,
    columns: list[str],
    rows: list[list[Any]],
    assumptions: list[str],
    data_notes: Sequence[str] = (),
) -> list[ChatMessage]:
    notes = ("Assumptions: " + "; ".join(assumptions) + "\n") if assumptions else ""
    if data_notes:  # missing data is stated as missing - never as a "(blank)" category
        notes += (
            "Data notes (mention missing data as such; never describe a blank value as a "
            "category): " + "; ".join(neutralise(n) for n in data_notes) + "\n"
        )
    return [
        {"role": "system", "content": SYSTEM_RULES},
        {
            "role": "user",
            "content": (
                "Write a short, factual summary of the query result for a business user, plus up "
                "to 5 highlights. Use ONLY numbers that appear in the DATA block; do not speculate "
                "about causes that the data does not show.\n"
                f"Query: {neutralise(description)}\n{notes}{data_block(columns, rows)}\n"
                f"{question_block(question)}"
            ),
        },
    ]
