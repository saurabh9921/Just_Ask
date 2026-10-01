"""Deterministic mock handlers for query planning and explanation.

Development/test stand-in for a real model (real deployments use an LLM; the
validation-first states in the orchestrator check either). Strategy, in order:
1. refusal for write verbs or restricted-data keywords;
2. correction turn: drop names the validator rejected from the previous plan;
3. follow-up: adapt the previous plan (``by X`` → new dimension, new period);
4. few-shot: reuse the plan of the most similar retrieved example question
   (Jaccard over tokens >= 0.5), overriding period and top-N from the question;
5. heuristic: entity/measure/dimension token overlap with the vocabulary.
Output is emitted as JSON and parsed by the same strict validator as real output.
"""

from __future__ import annotations

import json
import re
from typing import Any, Final

from app.agents.schemas import ResultExplanation
from app.llm.base import ChatMessage
from app.rag.text import tokenize
from app.semantic.roles import infer_role

_QUESTION = re.compile(r"<<<QUESTION>>>\n(.*?)\n<<<END QUESTION>>>", re.DOTALL)
_PREVIOUS = re.compile(r"<<<PREVIOUS_PLAN>>>\n(.*?)\n<<<END PREVIOUS_PLAN>>>", re.DOTALL)
_EXAMPLE = re.compile(
    r'<<<CONTEXT id="\d+" source="(?:query_examples|query_feedback)" key="[^"]*"[^>]*>>>\n'
    r"(.*?)\n<<<END CONTEXT>>>",
    re.DOTALL,
)
_ENTITY_LINE = re.compile(r"^- (\w+): ", re.MULTILINE)
_DATA = re.compile(r"<<<DATA>>>\n(.*?)\n<<<END DATA>>>", re.DOTALL)
_NUMBER_WORDS: Final = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "twelve": 12,
    "twenty": 20,
    "fifty": 50,
}
_WRITE_VERBS: Final = {
    "delete",
    "update",
    "insert",
    "drop",
    "truncate",
    "alter",
    "grant",
    "remove",
    "modify",
}
_RESTRICTED: Final = {
    ("bank",): "FORBIDDEN_FIELD",
    ("iban",): "FORBIDDEN_FIELD",
    ("tax", "id"): "FORBIDDEN_FIELD",
    ("password",): "FORBIDDEN_FIELD",
    ("token",): "FORBIDDEN_FIELD",
    ("card", "number"): "FORBIDDEN_FIELD",
    ("email",): "SENSITIVE_FIELD",
    ("phone",): "SENSITIVE_FIELD",
}


def _question(content: str) -> str:
    match = _QUESTION.search(content)
    return match.group(1) if match else content


def _num(word: str) -> int | None:
    return int(word) if word.isdigit() else _NUMBER_WORDS.get(word)


def parse_period(question: str) -> dict[str, Any] | None:
    q = question.lower()
    m = re.search(r"last (\w+) (day|days|month|months)", q)
    if m and _num(m.group(1)):
        n = _num(m.group(1)) or 1
        if m.group(2).startswith("month"):
            return (
                {"relative": "last_6_months"} if n == 6 else {"relative": "last_n_months", "n": n}
            )
        return {"relative": "last_n_days", "n": n}
    table = [
        ("year to date", "year_to_date"),
        ("ytd", "year_to_date"),
        ("this year", "this_year"),
        ("last year", "last_year"),
        ("this quarter", "this_quarter"),
        ("current quarter", "this_quarter"),
        ("last quarter", "last_quarter"),
        ("this month", "this_month"),
        ("last month", "last_month"),
        ("this week", "this_week"),
        ("last week", "last_week"),
        ("yesterday", "yesterday"),
        ("today", "today"),
    ]
    for phrase, rel in table:
        if re.search(rf"\b{phrase}\b", q):
            return {"relative": rel}
    return None


def _top_n(question: str) -> int | None:
    m = re.search(r"\btop (\w+)", question.lower())
    return _num(m.group(1)) if m else None


def _vocab(content: str) -> dict[str, dict[str, list[str]]]:
    vocab: dict[str, dict[str, list[str]]] = {}
    for block in re.split(r"\n(?=- \w+: )", content.split("VOCABULARY", 1)[-1]):
        head = _ENTITY_LINE.search(block)
        if not head:
            continue
        info: dict[str, list[str]] = {}
        for key in ("measures", "dimensions", "time_fields", "related"):
            m = re.search(rf"^\s+{key}: (.*)$", block, re.MULTILINE)
            info[key] = [x.strip() for x in m.group(1).split(",")] if m else []
        vocab[head.group(1)] = info
    return vocab


def _singular(word: str) -> str:
    if word.endswith(("ss", "us", "is")) or len(word) <= 3:
        return word
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    return word[:-1] if word.endswith("s") else word


def _groupable(dimensions: list[str]) -> list[str]:
    return [d for d in dimensions if infer_role(d) in {"category", "name"}]


def _best_entity(vocab: dict[str, dict[str, list[str]]], q_tokens: set[str]) -> str | None:
    """The entity the question is about. "lead" names Leads, not LeadCandidates: entity
    words the question does not use count against it (singular and plural are one word)."""
    best: tuple[float, str] | None = None
    q_singular = {_singular(t) for t in q_tokens}
    for entity, info in vocab.items():
        words = {_singular(t) for t in tokenize(entity)}
        matched = len(words & q_singular)
        penalty = 2 * len(words - q_singular) if matched else 0
        fields = sum(
            1
            for name in info.get("measures", []) + _groupable(info.get("dimensions", []))
            if set(tokenize(name)) & q_tokens
        )
        score = matched * 2 - penalty + fields
        if best is None or score > best[0]:
            best = (score, entity)
    return best[1] if best is not None and best[0] > 0 else None


def _jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def _refusal(question: str) -> dict[str, Any] | None:
    words = tokenize(question, keep_stopwords=True)
    if words and words[0] in _WRITE_VERBS:
        return {"refuse": True, "reason": "WRITE_NOT_ALLOWED"}
    wordset = set(words)
    for keys, reason in _RESTRICTED.items():
        if set(keys) <= wordset:
            return {"refuse": True, "reason": reason}
    return None


def _apply_question(plan: dict[str, Any], question: str) -> dict[str, Any]:
    period = parse_period(question)
    if period is not None and "comparison" not in plan:
        field = (plan.get("date_range") or {}).get("field")
        plan["date_range"] = {**({"field": field} if field else {}), **period}
    n = _top_n(question)
    if n:
        plan["limit"] = n
    grain = _GRAIN.search(question.lower())
    if grain and not plan.get("time_grain"):
        plan["time_grain"] = _GRAINS[grain.group(1) or grain.group(2)]
        plan.pop("sort", None)  # a trend is ordered by time
    return plan


_GRAINS = {
    "day": "day",
    "daily": "day",
    "week": "week",
    "weekly": "week",
    "month": "month",
    "monthly": "month",
    "quarter": "quarter",
    "quarterly": "quarter",
    "year": "year",
    "yearly": "year",
    "annual": "year",
}
_GRAIN = re.compile(
    r"\b(?:by|per) (day|week|month|quarter|year)\b"
    r"|\b(daily|weekly|monthly|quarterly|yearly|annual)\b"
)


def plan_query(messages: list[ChatMessage]) -> dict[str, Any]:
    user_turns = [m for m in messages if m["role"] == "user"]
    content = user_turns[0]["content"]
    question = _question(content)
    q_tokens = set(tokenize(question))
    vocab = _vocab(content)

    refusal = _refusal(question)
    if refusal:
        return refusal

    # Correction turn: remove rejected names from the previous attempt.
    if len(user_turns) > 1 and "<<<ISSUES>>>" in user_turns[-1]["content"]:
        rejected = json.loads(
            next(m["content"] for m in reversed(messages) if m["role"] == "assistant")
        )
        issues = user_turns[-1]["content"]
        bad = set(re.findall(r"'([A-Za-z_.]+)'", issues))
        for key in ("dimensions", "measures"):
            rejected[key] = [x for x in rejected.get(key, []) if x not in bad]
        rejected["filters"] = [f for f in rejected.get("filters", []) if f.get("field") not in bad]
        if rejected.get("sort", {}).get("by") in bad:
            rejected.pop("sort")
        if not rejected.get("measures") and not rejected.get("dimensions"):
            return {
                "clarification_required": True,
                "clarification_question": "Which measure should I show?",
            }
        result: dict[str, Any] = rejected
        return result

    previous = _PREVIOUS.search(content)
    if previous:
        plan = json.loads(previous.group(1))
        info = vocab.get(plan.get("entity", ""), {})
        by = re.search(r"\bby (\w+)", question.lower())
        if by:
            dims = [d for d in info.get("dimensions", []) if by.group(1) in tokenize(d)]
            if dims:
                plan["dimensions"] = dims[:1]
                plan.pop("sort", None)
        return _apply_question(plan, question)

    examples = []
    for match in _EXAMPLE.finditer(content):
        text = match.group(1)
        q_line = re.search(r"^Question: (.*)$", text, re.MULTILINE)
        p_line = re.search(r"^Plan: (.*)$", text, re.MULTILINE)
        if q_line and p_line:
            examples.append(
                (_jaccard(q_tokens, set(tokenize(q_line.group(1)))), json.loads(p_line.group(1)))
            )
    if examples:
        score, plan = max(examples, key=lambda e: e[0])
        if score >= 0.5 and plan.get("entity", "") in vocab | {"": {}}:
            return _apply_question(dict(plan), question)

    entity = _best_entity(vocab, q_tokens)
    if entity is None:
        return {
            "clarification_required": True,
            "clarification_question": "Which measure and period would you like to see?",
        }
    info = vocab[entity]
    measures = [m for m in info["measures"] if set(tokenize(m)) & q_tokens][:3] or info["measures"][
        :1
    ]
    # A field is not a breakdown just because one of its words is in the question:
    # notes and keys ("analysis_note", "users_id") are never picked this way.
    dims = [
        d
        for d in _groupable(info["dimensions"]) + info["time_fields"]
        if set(tokenize(d)) & q_tokens
    ][:2]
    plan = {"entity": entity, "measures": measures, "dimensions": dims, "confidence": 0.6}
    if q_tokens & {"highest", "top", "best", "most", "largest"}:
        plan["sort"] = {"by": measures[0], "direction": "desc"}
        plan["limit"] = _top_n(question) or 10
    return _apply_question(plan, question)


def explain_result(messages: list[ChatMessage]) -> ResultExplanation:
    content = messages[-1]["content"]
    match = _DATA.search(content)
    lines = match.group(1).splitlines() if match else []
    if len(lines) < 2:
        return ResultExplanation(summary="The query returned no rows for the selected scope.")
    header = [h.strip() for h in lines[0].split("|")]
    rows = [
        [c.strip() for c in line.split("|")]
        for line in lines[1:]
        if "|" in line or len(header) == 1
    ]
    highlights = []
    numeric_col = next((i for i, _ in enumerate(header) if rows and _is_number(rows[0][i])), None)
    if numeric_col is not None:
        values = [(r, float(r[numeric_col])) for r in rows if _is_number(r[numeric_col])]
        top_row, top_value = max(values, key=lambda x: x[1])
        label = top_row[0] if numeric_col != 0 else header[numeric_col]
        if len(values) == 1 and numeric_col == 0:
            summary = f"{header[0]}: {top_value:,.2f}."
            if len(header) > 3 and _is_number(rows[0][-1]):  # comparison: value, prev, change, %
                summary = (
                    f"{header[0]}: {top_value:,.2f} vs {float(rows[0][1]):,.2f} in the previous "
                    f"period ({float(rows[0][-1]):+.2f}%)."
                )
            return ResultExplanation(summary=summary, highlights=[summary])
        highlights.append(f"Highest {header[numeric_col]}: {label} ({top_value:,.2f}).")
        if len(values) > 1:
            low_row, low_value = min(values, key=lambda x: x[1])
            highlights.append(f"Lowest {header[numeric_col]}: {low_row[0]} ({low_value:,.2f}).")
    hidden = re.search(r"\((\d+) more rows not shown\)", content)
    total_rows = len(rows) + (int(hidden.group(1)) if hidden else 0)
    summary = f"{total_rows} row(s) returned" + (
        f"; {highlights[0][:-1].lower()}." if highlights else "."
    )
    return ResultExplanation(summary=summary[:600], highlights=highlights[:5])


def _is_number(value: str) -> bool:
    try:
        float(value)
    except ValueError:
        return False
    return True
