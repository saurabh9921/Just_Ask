"""Run labelled questions against an ERP Insight AI API and score the answers.

A *target* asks one question as one of the evaluation users and returns the HTTP
status, the JSON body and the wall-clock latency. A *gold executor* (sandbox mode
only) runs a case's gold plan through the query engine so the API's rows can be
compared with the rows the correct plan produces.
"""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from app.evals.dataset import EvalCase, EvalDataset, Outcome
from app.evals.scoring import (
    clarification_ok,
    outcome_of,
    percentile,
    rows_match,
    sql_safety_issues,
)
from app.query.plans import QueryPlan
from app.semantic.loader import SemanticBundle

UserName = Literal["analyst", "ungranted"]


class EvalTarget(Protocol):
    async def ask(self, question: str, user: UserName) -> tuple[int, dict[str, Any]]: ...

    def has_user(self, user: UserName) -> bool: ...


# (column names, rows) of the gold plan's formatted result
GoldResult = tuple[list[str], list[list[Any]]]
GoldExecutor = Callable[[QueryPlan], Awaitable[GoldResult]]


class CaseResult(BaseModel):
    id: str
    category: str
    question: str
    safety_critical: bool
    skipped: str | None = None
    outcome: Outcome | None = None
    http_status: int | None = None
    intent: str | None = None
    entity: str | None = None
    refusal_reason: str | None = None
    clarification_question: str | None = None
    latency_ms: float | None = None
    llm_calls: int = 0
    tokens: int = 0
    checks: dict[str, bool] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.skipped is None and all(self.checks.values())


class Metric(BaseModel):
    name: str
    passed: int
    total: int
    safety: bool = False

    @property
    def rate(self) -> float | None:
        return round(self.passed / self.total, 4) if self.total else None


class EvalReport(BaseModel):
    started_at: datetime
    target: str
    llm_provider: str
    result_correctness_checked: bool
    cases: list[CaseResult]
    metrics: list[Metric]
    latency_ms: dict[str, float | None]
    tokens: dict[str, float]
    categories: dict[str, int]

    def metric(self, name: str) -> Metric:
        return next(m for m in self.metrics if m.name == name)

    @property
    def safety_ok(self) -> bool:
        return all(m.rate in {None, 1.0} for m in self.metrics if m.safety)


async def run_case(
    case: EvalCase,
    target: EvalTarget,
    bundle: SemanticBundle,
    gold: GoldExecutor | None,
) -> CaseResult:
    result = CaseResult(
        id=case.id,
        category=case.category,
        question=case.question,
        safety_critical=case.safety_critical,
    )
    if not target.has_user(case.as_user):
        result.skipped = f"no '{case.as_user}' user configured"
        return result
    started = time.perf_counter()
    status, body = await target.ask(case.question, case.as_user)
    result.latency_ms = round((time.perf_counter() - started) * 1000, 1)
    result.http_status = status
    outcome = outcome_of(status, body)
    result.outcome = outcome
    expect = case.expect
    result.checks["outcome"] = outcome in expect.outcome

    plan = body.get("plan") if isinstance(body.get("plan"), dict) else None
    result.intent = body.get("intent")
    result.entity = plan.get("entity") if plan else None
    result.refusal_reason = body.get("refusal_reason")
    result.clarification_question = body.get("clarification_question")
    usage = body.get("usage") or {}
    result.llm_calls = int(usage.get("llm_calls", 0))
    result.tokens = int(usage.get("prompt_tokens", 0)) + int(usage.get("completion_tokens", 0))

    if expect.intent and status == 200:
        result.checks["intent"] = result.intent in expect.intent
    if expect.entity and "succeeded" in expect.outcome and outcome == "succeeded":
        result.checks["entity"] = result.entity == expect.entity
    if expect.outcome == ["succeeded"]:
        validation = body.get("validation") or {}
        result.checks["plan_valid"] = outcome == "succeeded" and bool(validation.get("plan_valid"))
    if outcome == "refused" and expect.refusal:
        result.checks["refusal_reason"] = result.refusal_reason in expect.refusal
    if "clarification" in expect.outcome and expect.outcome == ["clarification"]:
        result.checks["clarification_quality"] = outcome == "clarification" and (
            clarification_ok(result.clarification_question, expect.clarify_terms)
        )
    connection = body.get("connection") or {}
    for key in ("sql", "previous_sql"):
        sql = body.get(key)
        if isinstance(sql, str) and sql:
            issues = sql_safety_issues(
                sql,
                question=case.question,
                connector_type=connection.get("connector_type"),
                bundle=bundle,
            )
            result.checks["sql_safe"] = result.checks.get("sql_safe", True) and not issues
            result.notes += [f"{key}: {i}" for i in issues]
    if expect.plan is not None and gold is not None:
        result.checks["result_correct"] = await _correct(case, outcome, body, gold, result)
    return result


async def _correct(
    case: EvalCase, outcome: Outcome, body: dict[str, Any], gold: GoldExecutor, out: CaseResult
) -> bool:
    if outcome != "succeeded":
        # A safe non-answer to a tenant-isolation probe is correct; anything else is not.
        return case.tenant_isolation and outcome in case.expect.outcome
    assert case.expect.plan is not None  # noqa: S101
    gold_columns, expected = await gold(case.expect.plan)
    result = body.get("result") or {}
    columns = [c.get("name") for c in result.get("columns") or []]
    missing = [c for c in gold_columns if c not in columns]
    if missing:
        out.notes.append(f"missing columns: {', '.join(missing)}")
        return False
    extra = [c for c in columns if c not in gold_columns]
    if extra:
        # Extra measures do not make an answer wrong, but they are worth seeing.
        out.notes.append(f"extra columns: {', '.join(str(c) for c in extra)}")
    index = [columns.index(c) for c in gold_columns]
    actual = [[row[i] for i in index] for row in result.get("rows") or []]
    ordered = case.expect.plan.sort is not None and case.expect.plan.limit is not None
    ok = rows_match(actual, expected, ordered=ordered)
    if not ok:
        out.notes.append(f"rows differ: got {len(actual)}, expected {len(expected)}")
    return ok


def summarize(
    results: list[CaseResult], *, target: str, provider: str, gold: bool, started: datetime
) -> EvalReport:
    ran = [r for r in results if r.skipped is None]

    def metric(name: str, check: str, *, safety: bool = False, only_safety: bool = False) -> Metric:
        rows = [r for r in ran if check in r.checks and (r.safety_critical or not only_safety)]
        return Metric(
            name=name,
            passed=sum(1 for r in rows if r.checks[check]),
            total=len(rows),
            safety=safety,
        )

    safety_cases = [r for r in ran if r.safety_critical]
    metrics = [
        metric("intent_accuracy", "intent"),
        metric("entity_selection_accuracy", "entity"),
        metric("query_plan_validity", "plan_valid"),
        metric("sql_safety", "sql_safe", safety=True),
        Metric(
            name="policy_compliance",
            passed=sum(1 for r in safety_cases if r.passed),
            total=len(safety_cases),
            safety=True,
        ),
        metric("result_correctness", "result_correct"),
        metric("clarification_quality", "clarification_quality"),
        Metric(name="overall_pass", passed=sum(1 for r in ran if r.passed), total=len(ran)),
    ]
    latencies = [r.latency_ms for r in ran if r.latency_ms is not None]
    tokens = [r.tokens for r in ran]
    return EvalReport(
        started_at=started,
        target=target,
        llm_provider=provider,
        result_correctness_checked=gold,
        cases=results,
        metrics=metrics,
        latency_ms={
            "p50": percentile(latencies, 50),
            "p95": percentile(latencies, 95),
            "max": max(latencies) if latencies else None,
        },
        tokens={
            "total": float(sum(tokens)),
            "mean_per_question": round(sum(tokens) / len(tokens), 1) if tokens else 0.0,
            "llm_calls": float(sum(r.llm_calls for r in ran)),
        },
        categories=dict(Counter(r.category for r in results)),
    )


async def run_evaluation(
    dataset: EvalDataset,
    target: EvalTarget,
    bundle: SemanticBundle,
    *,
    gold: GoldExecutor | None,
    target_name: str,
    provider: str,
) -> EvalReport:
    started = datetime.now(tz=UTC)
    results = [await run_case(case, target, bundle, gold) for case in dataset.cases]
    return summarize(
        results, target=target_name, provider=provider, gold=gold is not None, started=started
    )


def render_markdown(report: EvalReport) -> str:
    lines = [
        "# ERP Insight AI - evaluation report",
        "",
        f"- Started: {report.started_at.isoformat(timespec='seconds')}",
        f"- Target: {report.target} - LLM provider: `{report.llm_provider}`",
        f"- Result correctness checked: {'yes' if report.result_correctness_checked else 'no'}",
        f"- Safety gate: **{'PASS' if report.safety_ok else 'FAIL'}**",
        "",
        "| Metric | Passed | Total | Rate |",
        "|---|---:|---:|---:|",
    ]
    for m in report.metrics:
        rate = "n/a" if m.rate is None else f"{m.rate:.1%}"
        name = f"{m.name} (safety)" if m.safety else m.name
        lines.append(f"| {name} | {m.passed} | {m.total} | {rate} |")
    lat = report.latency_ms
    lines += [
        "",
        f"Latency (ms): p50 {lat['p50']} · p95 {lat['p95']} · max {lat['max']}",
        f"Tokens: total {report.tokens['total']:.0f} · mean/question "
        f"{report.tokens['mean_per_question']} · LLM calls {report.tokens['llm_calls']:.0f}",
        "",
        "| Case | Category | Outcome | Checks | Latency ms | Notes |",
        "|---|---|---|---|---:|---|",
    ]
    for r in report.cases:
        if r.skipped:
            lines.append(f"| {r.id} | {r.category} | skipped | - | - | {r.skipped} |")
            continue
        failed = [k for k, ok in r.checks.items() if not ok]
        checks = "ok" if not failed else "FAIL: " + ", ".join(failed)
        notes = "; ".join(r.notes).replace("|", "/")
        lines.append(
            f"| {r.id} | {r.category} | {r.outcome} | {checks} | {r.latency_ms} | {notes} |"
        )
    return "\n".join(lines) + "\n"
