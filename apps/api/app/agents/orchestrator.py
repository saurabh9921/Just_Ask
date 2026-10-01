"""Analytics agent: a finite state machine over typed tools.

The run is a fixed sequence of states (see ``AgentStep``); the only loop is one
optional plan-correction pass (VALIDATE_QUERY_PLAN -> BUILD_QUERY_PLAN). The
model contributes three typed artefacts - intent, query plan, explanation - and
never chooses what happens next, never sees credentials or detail rows, and has
no database access. Everything that touches data is deterministic code:
validation, compilation, SQL safety checks, cost estimation, read-only execution.

Validation first. Between the model's plan and the answer, deterministic states
check that the answer is *about the right thing* and *supported by the data*:

* DETECT_ANALYSIS_INTENT - "lead analysis for X" is a business analysis, not a
  record count: RESOLVE_ENTITIES → RUN_ANALYSIS (``app.agents.analysis``);
* SELECT_FIELDS - free text/identifiers are not breakdowns, "by <term>" and value
  terms must exist (``app.agents.fields``), a named company is resolved to a filter
  (``app.agents.entities``) and status words are grounded in real values;
* VALIDATE_RESULT - every breakdown is profiled for NULL/blank values and the chart
  is only enabled when it is a truthful picture (``result_formatter.validate_chart``).

Every request ends with one structured trace log line (``analytics trace``).

Enforced limits: maximum steps, per-step timeout, total timeout, cancellation
between steps and during execution. Every run ends in STORE_AUDIT_EVENT.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal, NoReturn

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.analysis import (
    AnalysisOutcome,
    AnalysisRequest,
    BusinessAnalyzer,
    SubResult,
    detect_analysis,
)
from app.agents.entities import (
    Mention,
    Resolution,
    find_mention,
    name_targets,
    normalize_name,
    plural_noun,
    resolve_mention,
)
from app.agents.fields import dropped_filters, retarget, select_fields
from app.agents.guards import QuestionGuard
from app.agents.policies import AgentCancelledError, AgentLimitError, AgentPolicy
from app.agents.prompts.planning import explanation_messages
from app.agents.prompts.templates import intent_classification_messages
from app.agents.schemas import IntentClassification, ResultExplanation
from app.agents.state import (
    STEP_MESSAGES,
    AgentError,
    AgentStep,
    OutcomeStatus,
    ProgressEvent,
    StepRecord,
)
from app.agents.tools import (
    AllowedConnectionsOut,
    ClarificationOut,
    CompiledSqlOut,
    CostOut,
    ExecutionOut,
    PlanValidationOut,
    RetrievalOut,
    RetrievedDocOut,
    ToolRegistry,
    UserContextOut,
)
from app.config import Settings
from app.connectors.base import DatabaseAdapter
from app.connectors.common.errors import QueryCancelledError
from app.connectors.common.types import ObjectMetadata
from app.core.exceptions import AppError, NotFoundError
from app.core.logging import get_logger
from app.core.metrics import (
    AGENT_STEP_DURATION,
    QUERY_RESULT_ROWS,
    QUESTION_GUARD_REFUSALS,
    SQL_SAFETY_REJECTIONS,
)
from app.core.permissions import Permission
from app.core.tenant_context import TenantContext
from app.db.models import DatabaseConnection, MetadataObject, SemanticVersion, Tenant
from app.llm.base import ChatMessage, LLMOutputError, LLMProvider, LLMUnavailableError
from app.llm.usage import TokenUsage, track_llm_usage
from app.query.cost_estimator import estimate_cost
from app.query.planner import QueryPlanner, build_vocabulary, describe_plan
from app.query.plans import FilterOp, QueryFilter, QueryPlan, RefusalReason
from app.query.quality import is_blank
from app.query.result_cache import (
    CacheHit,
    CatalogCache,
    CatalogEntry,
    QueryResultCache,
    result_cache_key,
    scope_hash,
)
from app.query.result_formatter import (
    ChartHint,
    FormattedResult,
    explainable_data,
    format_result,
)
from app.query.sql_policy import build_sql_policy
from app.query.sql_validator import SqlPolicy, SqlSafetyError, validate_sql
from app.query.validator import PlanContext, PlanValidationError, ResolvedPlan, resolve_plan
from app.rag.collections import Collection
from app.rag.hybrid_retriever import HybridRetriever, RetrievedDocument
from app.rag.qdrant_client import RetrievalScope
from app.rag.text import tokenize
from app.schemas.insights import (
    AnalysisChartOut,
    AnalysisOut,
    AnalysisQueryOut,
    AnalysisValidationOut,
    EntityOut,
    MetricOut,
)
from app.semantic.loader import SemanticBundle
from app.semantic.models import Aggregation, Entity
from app.semantic.registry import SemanticLayerRegistry
from app.semantic.roles import is_status_like
from app.semantic.validator import EntityBinding, bind_to_catalog
from app.services.audit_service import AuditOutcome, AuditService
from app.services.connection_service import ConnectionService, config_version
from app.services.semantic_service import SemanticIndexNotReadyError

logger = get_logger(__name__)
trace_logger = get_logger("app.analytics.trace")

Emit = Callable[[ProgressEvent], Awaitable[None]]
# Status words: when the data has no such value the user is told, never ignored silently.
STRICT_STATUS_WORDS = frozenset(
    {
        "pending",
        "won",
        "lost",
        "qualified",
        "unqualified",
        "converted",
        "rejected",
        "approved",
        "failed",
        "passed",
        "overdue",
        "cancelled",
        "canceled",
        "closed",
        "duplicate",
        "hot",
        "warm",
        "cold",
        "paid",
        "unpaid",
        "confirmed",
        "draft",
    }
)
# Applied when they match a value, otherwise left alone ("new" also means "recent").
SOFT_STATUS_WORDS = frozenset({"new", "open", "active", "inactive", "completed", "complete"})
_SHARE = re.compile(r"\b(share|composition|proportion|percentage|split|mix)\b", re.IGNORECASE)
_RELATION = re.compile(
    r"\b(vs\.?|versus|against|relationship between|correlat\w*)\b", re.IGNORECASE
)
LLM_STEPS = frozenset(
    {AgentStep.CLASSIFY_INTENT, AgentStep.BUILD_QUERY_PLAN, AgentStep.GENERATE_EXPLANATION}
)
# States that run database queries get the execution timeout.
QUERY_STEPS = frozenset(
    {
        AgentStep.EXECUTE_READONLY_QUERY,
        AgentStep.RUN_ANALYSIS,
        AgentStep.RESOLVE_ENTITIES,
        AgentStep.SELECT_FIELDS,
    }
)


@dataclass
class OrchestratorDeps:
    settings: Settings
    session: AsyncSession
    ctx: TenantContext
    connections: ConnectionService
    registry: SemanticLayerRegistry
    retriever: HybridRetriever
    llm: LLMProvider
    audit: AuditService
    today: date
    result_cache: QueryResultCache | None = None
    catalog_cache: CatalogCache | None = None


@dataclass
class AgentRequest:
    query_id: uuid.UUID
    question: str
    connection_id: uuid.UUID
    mode: Literal["execute", "plan"] = "execute"
    previous_plan: dict[str, Any] | None = None
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    adapter_ready: Callable[[DatabaseAdapter], None] | None = None


@dataclass
class AgentOutcome:
    query_id: uuid.UUID
    status: OutcomeStatus = "failed"
    intent: IntentClassification | None = None
    plan: QueryPlan | None = None
    resolved: ResolvedPlan | None = None
    description: str | None = None
    assumptions: list[str] = field(default_factory=list)
    clarification_question: str | None = None
    refusal_reason: str | None = None
    sql: str | None = None
    previous_sql: str | None = None
    sql_hash: str | None = None
    cost: CostOut | None = None
    result: FormattedResult | None = None
    explanation: ResultExplanation | None = None
    explanation_source: Literal["llm", "system"] | None = None
    context_keys: list[str] = field(default_factory=list)
    error: AgentError | None = None
    steps: list[StepRecord] = field(default_factory=list)
    timings: dict[str, float] = field(default_factory=dict)
    connection_name: str | None = None
    connector_type: str | None = None
    semantic_version: int | None = None
    data_freshness: str | None = None
    row_filter_fields: list[str] = field(default_factory=list)
    corrections: int = 0
    cache_hit: bool = False
    cache_age_seconds: float | None = None
    llm_usage: TokenUsage = field(default_factory=TokenUsage)
    # Validation-first: structured analytics and what the checks changed or found.
    analysis: AnalysisOut | None = None
    entity: EntityOut | None = None
    notes: list[str] = field(default_factory=list)


class _Stop(Exception):  # noqa: N818 - control flow: terminal state reached
    pass


class AnalyticsOrchestrator:
    def __init__(
        self, deps: OrchestratorDeps, policy: AgentPolicy, emit: Emit | None = None
    ) -> None:
        self.deps = deps
        self.policy = policy
        self.emit = emit
        self.tools = ToolRegistry()
        self.planner = QueryPlanner(deps.llm)
        # per-run state
        self._req: AgentRequest
        self._out: AgentOutcome
        self._started = 0.0
        self._bundle: SemanticBundle | None = None
        self._bindings: dict[str, EntityBinding] = {}
        self._catalog: list[ObjectMetadata] = []
        self._row_estimates: dict[str, int] = {}
        self._adapter: DatabaseAdapter | None = None
        self._scope: RetrievalScope | None = None
        self._tenant_settings: dict[str, Any] = {}
        self._docs: list[RetrievedDocument] = []
        self._examples: list[RetrievedDocument] = []
        self._plan_messages: list[ChatMessage] = []
        self._validation: PlanValidationOut | None = None
        self._compiled: CompiledSqlOut | None = None
        self._execution: ExecutionOut | None = None
        self._cached: CacheHit | None = None
        self._config_version = ""
        self._analysis_request: AnalysisRequest | None = None
        self._analysis_outcome: AnalysisOutcome | None = None
        self._resolution: Resolution | None = None
        self._resolutions: dict[tuple[str, str], Resolution] = {}
        self._trace_queries: list[dict[str, Any]] = []
        self._retargeted: str | None = None
        self._register_tools()

    # ================================================================ tools
    def _register_tools(self) -> None:
        d = self.deps

        async def get_current_user_context() -> UserContextOut:
            return UserContextOut(
                tenant_id=str(d.ctx.tenant_id),
                tenant_slug=d.ctx.tenant_slug,
                user_id=str(d.ctx.user_id),
                permissions=sorted(p.value for p in d.ctx.permissions),
            )

        async def list_allowed_connections() -> AllowedConnectionsOut:
            return AllowedConnectionsOut(
                connection_ids=[str(c.id) for c in await d.connections.list_visible()]
            )

        async def _retrieve(query: str, collections: list[Collection]) -> RetrievalOut:
            assert self._scope is not None  # noqa: S101 - set in RESOLVE_DATA_SOURCE
            docs = await d.retriever.search(
                self._scope, query, collections=collections, top_k=d.settings.rag_top_k
            )
            return RetrievalOut(
                documents=[
                    RetrievedDocOut(
                        collection=x.collection,
                        doc_key=x.doc_key,
                        title=x.title,
                        text=x.text,
                        entity_name=x.entity_name,
                        score=x.score,
                        untrusted_flagged=x.untrusted_flagged,
                    )
                    for x in docs
                ]
            )

        async def retrieve_semantic_entities(query: str) -> RetrievalOut:
            return await _retrieve(
                query, [Collection.SEMANTIC_ENTITIES, Collection.DATABASE_METADATA]
            )

        async def retrieve_business_glossary(query: str) -> RetrievalOut:
            return await _retrieve(query, [Collection.BUSINESS_GLOSSARY])

        async def retrieve_similar_questions(query: str) -> RetrievalOut:
            # Curated examples plus answers users confirmed as correct (feedback memory).
            return await _retrieve(query, [Collection.QUERY_EXAMPLES, Collection.QUERY_FEEDBACK])

        async def build_query_plan(
            question: str, previous_plan: dict[str, Any] | None
        ) -> QueryPlan:
            assert self._bundle is not None  # noqa: S101
            vocab = build_vocabulary(
                self._bundle, self._bindings, frozenset(p.value for p in d.ctx.permissions)
            )
            plan, messages = await self.planner.build(
                question, [*self._examples, *self._docs], vocab, d.today, previous_plan
            )
            self._plan_messages = messages
            return plan

        async def validate_query_plan(plan: QueryPlan) -> PlanValidationOut:
            assert self._bundle is not None  # noqa: S101
            if not plan.is_data_plan:
                return PlanValidationOut(valid=True)
            try:
                resolved = resolve_plan(
                    plan,
                    bundle=self._bundle,
                    bindings=self._bindings,
                    catalog=self._catalog,
                    ctx=self._plan_context(),
                )
            except PlanValidationError as exc:
                return PlanValidationOut(
                    valid=False,
                    retryable=exc.retryable,
                    issues=[i.message for i in exc.issues],
                    issue_codes=[i.code.value for i in exc.issues],
                )
            return PlanValidationOut(valid=True, resolved=resolved)

        async def compile_sql(resolved: ResolvedPlan) -> CompiledSqlOut:
            assert self._adapter is not None  # noqa: S101
            compiled = self._adapter.compile_query_plan(resolved)
            dialect = self._adapter.dialect
            return CompiledSqlOut(
                sql=compiled.render(dialect),
                previous_sql=compiled.render(dialect, previous=True)
                if compiled.previous_statement is not None
                else None,
                compiled=compiled,
            )

        async def estimate_query_cost(compiled: CompiledSqlOut) -> CostOut:
            assert self._adapter is not None  # noqa: S101
            est = await estimate_cost(
                self._adapter,
                compiled.compiled,
                max_cost=d.settings.query_max_cost,
                object_row_estimates=self._row_estimates,
                max_scan_rows=d.settings.query_max_scan_rows,
            )
            return CostOut(
                source=est.source,
                estimated_cost=est.estimated_cost,
                estimated_rows=est.estimated_rows,
            )

        async def execute_readonly_query(
            compiled: CompiledSqlOut, resolved: ResolvedPlan
        ) -> ExecutionOut:
            assert self._adapter is not None  # noqa: S101
            started = time.perf_counter()
            qid = str(self._req.query_id)
            current = await self._adapter.execute_readonly(
                compiled.compiled.statement,
                max_rows=resolved.limit,
                timeout_seconds=d.settings.max_query_seconds,
                query_id=qid,
            )
            previous = None
            if compiled.compiled.previous_statement is not None:
                previous = await self._adapter.execute_readonly(
                    compiled.compiled.previous_statement,
                    max_rows=resolved.limit,
                    timeout_seconds=d.settings.max_query_seconds,
                    query_id=qid,
                )
            return ExecutionOut(
                current=current,
                previous=previous,
                execution_ms=round((time.perf_counter() - started) * 1000, 2),
            )

        async def format_result_tool(
            execution: ExecutionOut, resolved: ResolvedPlan, compiled: CompiledSqlOut
        ) -> FormattedResult:
            assert self._bundle is not None  # noqa: S101
            return format_result(
                resolved,
                compiled.compiled.output,
                execution.current,
                execution.previous,
                self._bundle.policy,
                max_blank_ratio=d.settings.result_max_blank_ratio,
                noun=plural_noun(resolved.entity),
                chart_hint=chart_hint(self._req.question),
            )

        async def request_clarification(question: str) -> ClarificationOut:
            return ClarificationOut(question=question)

        for name, output, fn in (
            ("get_current_user_context", UserContextOut, get_current_user_context),
            ("list_allowed_connections", AllowedConnectionsOut, list_allowed_connections),
            ("retrieve_semantic_entities", RetrievalOut, retrieve_semantic_entities),
            ("retrieve_business_glossary", RetrievalOut, retrieve_business_glossary),
            ("retrieve_similar_questions", RetrievalOut, retrieve_similar_questions),
            ("build_query_plan", QueryPlan, build_query_plan),
            ("validate_query_plan", PlanValidationOut, validate_query_plan),
            ("compile_sql", CompiledSqlOut, compile_sql),
            ("estimate_query_cost", CostOut, estimate_query_cost),
            ("execute_readonly_query", ExecutionOut, execute_readonly_query),
            ("format_result", FormattedResult, format_result_tool),
            ("request_clarification", ClarificationOut, request_clarification),
        ):
            self.tools.register(name, output, fn)

    # ============================================================== running
    async def _emit(
        self, step: AgentStep, status: Literal["started", "completed", "failed", "skipped"]
    ) -> None:
        if self.emit is None:
            return
        await self.emit(
            ProgressEvent(
                query_id=self._req.query_id,
                step=step,
                status=status,
                message=STEP_MESSAGES[step],
                elapsed_ms=round((time.perf_counter() - self._started) * 1000, 2),
            )
        )

    def _fail(self, error: AppError) -> None:
        self._out.status = "failed"
        self._out.error = AgentError(
            error.code, error.message, error.status_code, dict(error.details)
        )

    async def run(self, request: AgentRequest) -> AgentOutcome:
        self._req = request
        self._out = AgentOutcome(query_id=request.query_id)
        self._started = time.perf_counter()
        step: AgentStep = AgentStep.RECEIVE_QUESTION
        handlers: dict[AgentStep, Callable[[], Awaitable[AgentStep]]] = {
            AgentStep.RECEIVE_QUESTION: self._receive,
            AgentStep.CLASSIFY_INTENT: self._classify,
            AgentStep.RESOLVE_DATA_SOURCE: self._resolve_source,
            AgentStep.DETECT_ANALYSIS_INTENT: self._detect_analysis,
            AgentStep.RESOLVE_ENTITIES: self._resolve_entities,
            AgentStep.RUN_ANALYSIS: self._run_analysis,
            AgentStep.RETRIEVE_SEMANTIC_CONTEXT: self._retrieve_context,
            AgentStep.RETRIEVE_SIMILAR_EXAMPLES: self._retrieve_examples,
            AgentStep.BUILD_QUERY_PLAN: self._build_plan,
            AgentStep.SELECT_FIELDS: self._select_fields,
            AgentStep.VALIDATE_QUERY_PLAN: self._validate_plan,
            AgentStep.REQUEST_CLARIFICATION_IF_NEEDED: self._clarify,
            AgentStep.COMPILE_DIALECT_SQL: self._compile,
            AgentStep.VALIDATE_SQL: self._validate_sql,
            AgentStep.ESTIMATE_COST: self._estimate,
            AgentStep.EXECUTE_READONLY_QUERY: self._execute,
            AgentStep.FORMAT_RESULT: self._format,
            AgentStep.VALIDATE_RESULT: self._validate_result,
            AgentStep.GENERATE_EXPLANATION: self._explain,
        }
        with track_llm_usage() as usage:
            self._out.llm_usage = usage
            step = await self._loop(step, handlers)
        await self._run_step(AgentStep.STORE_AUDIT_EVENT, self._audit)
        self._out.timings["total_ms"] = round((time.perf_counter() - self._started) * 1000, 2)
        self._out.timings["llm_ms"] = round(
            sum(s.duration_ms for s in self._out.steps if s.step in LLM_STEPS), 2
        )
        await self._emit(AgentStep.COMPLETE, "completed")
        self._log_trace()
        return self._out

    async def _loop(
        self, step: AgentStep, handlers: dict[AgentStep, Callable[[], Awaitable[AgentStep]]]
    ) -> AgentStep:
        count = 0
        while step not in {AgentStep.STORE_AUDIT_EVENT, AgentStep.COMPLETE}:
            count += 1
            try:
                self._check_limits(count)
                step = await self._run_step(step, handlers[step])
            except _Stop:
                step = AgentStep.STORE_AUDIT_EVENT
            except (AgentCancelledError, QueryCancelledError):
                self._out.status = "cancelled"
                self._out.error = AgentError("QUERY_CANCELLED", "The query was cancelled.", 409)
                step = AgentStep.STORE_AUDIT_EVENT
            except AppError as exc:
                self._fail(exc)
                step = AgentStep.STORE_AUDIT_EVENT
            except Exception:
                logger.exception("analytics agent crashed", extra={"step": step.value})
                self._fail(AppError())
                step = AgentStep.STORE_AUDIT_EVENT
        return step

    def _check_limits(self, count: int) -> None:
        if count > self.policy.max_steps:
            raise AgentLimitError(details={"reason": "MAX_STEPS"})
        if time.perf_counter() - self._started > self.policy.total_timeout_seconds:
            raise AgentLimitError(details={"reason": "TOTAL_TIMEOUT"})
        if self._req.cancel.is_set():
            raise AgentCancelledError()

    async def _run_step(
        self, step: AgentStep, handler: Callable[[], Awaitable[AgentStep]]
    ) -> AgentStep:
        await self._emit(step, "started")
        started = time.perf_counter()
        timeout = (
            self.policy.execution_timeout_seconds * 2
            if step in QUERY_STEPS
            else self.policy.step_timeout_seconds
        )
        status: Literal["completed", "failed"] = "failed"
        try:
            async with asyncio.timeout(timeout):
                nxt = (
                    await handler()
                    if step is AgentStep.STORE_AUDIT_EVENT  # the audit record is never skipped
                    else await self._cancellable(handler())
                )
        except TimeoutError as exc:
            raise AgentLimitError(details={"reason": "STEP_TIMEOUT", "step": step.value}) from exc
        else:
            status = "completed"
            return nxt
        finally:
            duration = round((time.perf_counter() - started) * 1000, 2)
            AGENT_STEP_DURATION.labels(step.value, status).observe(duration / 1000)
            self._out.steps.append(StepRecord(step=step, status=status, duration_ms=duration))
            await self._emit(step, status)

    async def _cancellable(self, work: Awaitable[AgentStep]) -> AgentStep:
        """Run one state, abandoning it as soon as cancellation is requested."""
        task = asyncio.ensure_future(work)
        waiter = asyncio.ensure_future(self._req.cancel.wait())
        try:
            done, _ = await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            waiter.cancel()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        if task in done:
            return task.result()
        raise AgentCancelledError()

    # ============================================================= handlers
    async def _receive(self) -> AgentStep:
        question = self._req.question.strip()
        if not question:
            raise AppError("The question is empty.")
        self.deps.ctx.require(Permission.ANALYTICS_QUERY)
        verdict = QuestionGuard(self.deps.registry.get()).check(question)
        if verdict is not None:
            QUESTION_GUARD_REFUSALS.labels(rule=verdict.rule).inc()
            self._out.intent = IntentClassification(
                intent="unsupported", confidence=1.0, reason=f"policy guard: {verdict.rule}"
            )
            self._out.status = "refused"
            self._out.refusal_reason = verdict.reason.value
            raise _Stop
        return AgentStep.CLASSIFY_INTENT

    async def _classify(self) -> AgentStep:
        intent = await self.deps.llm.generate_structured(
            intent_classification_messages(self._req.question), IntentClassification
        )
        self._out.intent = intent
        if intent.intent == "unsupported":
            self._out.status = "refused"
            self._out.refusal_reason = RefusalReason.WRITE_NOT_ALLOWED.value
            raise _Stop
        return AgentStep.RESOLVE_DATA_SOURCE

    async def _resolve_source(self) -> AgentStep:
        d = self.deps
        await self.tools.call("get_current_user_context")
        allowed = await self.tools.call("list_allowed_connections")
        assert isinstance(allowed, AllowedConnectionsOut)  # noqa: S101
        conn: DatabaseConnection = await d.connections.get(
            self._req.connection_id
        )  # 404 if not visible
        if str(conn.id) not in allowed.connection_ids:  # pragma: no cover - defence in depth
            raise NotFoundError()
        active = await d.session.scalar(
            select(SemanticVersion).where(
                SemanticVersion.connection_id == conn.id,
                SemanticVersion.tenant_id == d.ctx.tenant_id,
                SemanticVersion.status == "active",
            )
        )
        if active is None or active.metadata_snapshot_id is None:
            raise SemanticIndexNotReadyError()
        tenant = await d.session.get(Tenant, d.ctx.tenant_id)
        self._tenant_settings = dict(tenant.settings) if tenant else {}
        await self._load_catalog(active.metadata_snapshot_id)
        self._config_version = config_version(conn)
        self._adapter = await d.connections.adapter_for(conn)
        if self._req.adapter_ready is not None:
            self._req.adapter_ready(self._adapter)
        self._scope = RetrievalScope(
            tenant_id=d.ctx.tenant_id,
            connection_id=conn.id,
            version=active.version,
            permissions=frozenset(p.value for p in d.ctx.permissions),
        )
        self._out.connection_name = conn.name
        self._out.connector_type = conn.connector_type
        self._out.semantic_version = active.version
        self._out.data_freshness = active.finished_at.isoformat() if active.finished_at else None
        return AgentStep.DETECT_ANALYSIS_INTENT

    async def _load_catalog(self, snapshot_id: uuid.UUID) -> None:
        """Parsed catalog + bindings for a snapshot (immutable, so cached per process)."""
        d = self.deps
        cache = d.catalog_cache
        entry = cache.get_catalog(d.ctx.tenant_id, snapshot_id) if cache else None
        if entry is None:
            rows = list(
                await d.session.scalars(
                    select(MetadataObject).where(
                        MetadataObject.snapshot_id == snapshot_id,
                        MetadataObject.tenant_id == d.ctx.tenant_id,
                    )
                )
            )
            estimates: dict[str, int] = {}
            for r in rows:
                if r.row_estimate is not None:
                    estimates[r.name.lower()] = r.row_estimate
                    estimates[f"{r.schema_name}.{r.name}".lower()] = r.row_estimate
            entry = CatalogEntry(
                [ObjectMetadata.model_validate(r.definition) for r in rows], estimates
            )
            if cache:
                cache.put_catalog(d.ctx.tenant_id, snapshot_id, entry)
        self._catalog = entry.catalog
        self._row_estimates = dict(entry.row_estimates)
        self._bundle = d.registry.get()
        bundle_hash = self._bundle.content_hash
        bindings = cache.get_bindings(d.ctx.tenant_id, snapshot_id, bundle_hash) if cache else None
        if bindings is None:
            bindings = bind_to_catalog(self._bundle, self._catalog)
            if cache:
                cache.put_bindings(d.ctx.tenant_id, snapshot_id, bundle_hash, bindings)
        self._bindings = bindings

    @staticmethod
    def _to_docs(out: Any) -> list[RetrievedDocument]:
        assert isinstance(out, RetrievalOut)  # noqa: S101
        return [
            RetrievedDocument(
                d.collection,
                d.doc_key,
                d.title,
                d.text,
                d.entity_name,
                d.score,
                None,
                None,
                d.untrusted_flagged,
            )
            for d in out.documents
        ]

    async def _retrieve_context(self) -> AgentStep:
        entities = self._to_docs(
            await self.tools.call("retrieve_semantic_entities", query=self._req.question)
        )
        glossary = self._to_docs(
            await self.tools.call("retrieve_business_glossary", query=self._req.question)
        )
        self._docs = entities + glossary
        return AgentStep.RETRIEVE_SIMILAR_EXAMPLES

    async def _retrieve_examples(self) -> AgentStep:
        self._examples = self._to_docs(
            await self.tools.call("retrieve_similar_questions", query=self._req.question)
        )
        self._out.context_keys = [d.doc_key for d in [*self._examples, *self._docs]]
        return AgentStep.BUILD_QUERY_PLAN

    async def _build_plan(self) -> AgentStep:
        if (
            self._validation is not None
            and not self._validation.valid
            and self._out.plan is not None
        ):
            self._out.corrections += 1
            plan = await self.planner.correct(
                self._plan_messages, self._out.plan, self._validation.issues
            )
        else:
            result = await self.tools.call(
                "build_query_plan",
                question=self._req.question,
                previous_plan=self._req.previous_plan,
            )
            assert isinstance(result, QueryPlan)  # noqa: S101
            plan = result
        self._out.plan = plan
        return AgentStep.SELECT_FIELDS

    async def _validate_plan(self) -> AgentStep:
        assert self._out.plan is not None  # noqa: S101
        result = await self.tools.call("validate_query_plan", plan=self._out.plan)
        assert isinstance(result, PlanValidationOut)  # noqa: S101
        self._validation = result
        if result.valid:
            self._out.resolved = result.resolved
            return AgentStep.REQUEST_CLARIFICATION_IF_NEEDED
        if result.retryable and self._out.corrections < self.policy.max_plan_corrections:
            return AgentStep.BUILD_QUERY_PLAN
        raise _plan_error(result, blocked=not result.retryable)

    async def _clarify(self) -> AgentStep:
        plan = self._out.plan
        assert plan is not None  # noqa: S101
        if plan.refuse:
            self._out.status = "refused"
            self._out.refusal_reason = (
                plan.reason.value if plan.reason else RefusalReason.OUT_OF_SCOPE.value
            )
            raise _Stop
        question: str | None = None
        if plan.clarification_required:
            question = plan.clarification_question
        elif plan.confidence < self.policy.min_confidence:
            question = (
                "I am not confident I understood the question. "
                "Could you rephrase it or name the measure and period?"
            )
        if question:
            out = await self.tools.call("request_clarification", question=question)
            assert isinstance(out, ClarificationOut)  # noqa: S101
            self._out.status = "clarification"
            self._out.clarification_question = out.question
            raise _Stop
        assert self._out.resolved is not None  # noqa: S101
        self._out.assumptions = list(self._out.resolved.assumptions)
        self._out.description = describe_plan(self._out.resolved)
        self._out.row_filter_fields = sorted({rf.field for rf in self._out.resolved.row_filters})
        return AgentStep.COMPILE_DIALECT_SQL

    async def _compile(self) -> AgentStep:
        compiled = await self.tools.call("compile_sql", resolved=self._out.resolved)
        assert isinstance(compiled, CompiledSqlOut)  # noqa: S101
        self._compiled = compiled
        self._out.sql = compiled.sql
        self._out.previous_sql = compiled.previous_sql
        return AgentStep.VALIDATE_SQL

    def _sql_policy(self) -> SqlPolicy:
        assert (  # noqa: S101 - set by earlier states
            self._bundle is not None
            and self._out.resolved is not None
            and self._adapter is not None
        )
        return build_sql_policy(
            self._bundle,
            self._out.resolved,
            self._adapter.dialect.name,
            max_limit=self.deps.settings.max_query_rows + 1,
        )

    async def _validate_sql(self) -> AgentStep:
        assert self._compiled is not None  # noqa: S101
        policy = self._sql_policy()
        try:
            validated = validate_sql(self._compiled.sql, policy)
            if self._compiled.previous_sql is not None:
                validate_sql(self._compiled.previous_sql, policy)
        except SqlSafetyError as exc:
            # Compiled SQL failing the independent check is a compiler/policy defect.
            SQL_SAFETY_REJECTIONS.labels(str(exc.details.get("reason", "unknown"))).inc()
            logger.warning(
                "compiled SQL rejected by safety validator",
                extra={"reason": exc.details.get("reason"), "query_id": str(self._req.query_id)},
            )
            raise
        self._out.sql_hash = validated.statement_hash
        return AgentStep.ESTIMATE_COST

    async def _estimate(self) -> AgentStep:
        cost = await self.tools.call("estimate_query_cost", compiled=self._compiled)
        assert isinstance(cost, CostOut)  # noqa: S101
        self._out.cost = cost
        if self._req.mode == "plan":
            self._out.status = "planned"
            raise _Stop
        return AgentStep.EXECUTE_READONLY_QUERY

    def _cache_key(self) -> str | None:
        resolved, cache = self._out.resolved, self.deps.result_cache
        if cache is None or not cache.enabled or resolved is None or self._out.sql_hash is None:
            return None
        return result_cache_key(
            tenant_id=self.deps.ctx.tenant_id,
            connection_id=self._req.connection_id,
            config_version=self._config_version,
            semantic_version=self._scope.version if self._scope else 0,
            scope=scope_hash(
                frozenset(p.value for p in self.deps.ctx.permissions),
                [(rf.source, rf.field, rf.value) for rf in resolved.row_filters],
            ),
            plan_hash=resolved.plan_hash,
            sql_hash=self._out.sql_hash,
        )

    async def _execute(self) -> AgentStep:
        key = self._cache_key()
        if key is not None and self.deps.result_cache is not None:
            hit = await self.deps.result_cache.get(key)
            if hit is not None:
                self._cached = hit
                self._out.cache_hit = True
                self._out.cache_age_seconds = round(hit.age_seconds, 1)
                self._out.timings["execution_ms"] = 0.0
                return AgentStep.FORMAT_RESULT
        execution = await self.tools.call(
            "execute_readonly_query", compiled=self._compiled, resolved=self._out.resolved
        )
        assert isinstance(execution, ExecutionOut)  # noqa: S101
        self._execution = execution
        self._out.timings["execution_ms"] = execution.execution_ms
        return AgentStep.FORMAT_RESULT

    async def _format(self) -> AgentStep:
        if self._cached is not None:
            self._out.result = self._cached.result
            return AgentStep.VALIDATE_RESULT
        result = await self.tools.call(
            "format_result",
            execution=self._execution,
            resolved=self._out.resolved,
            compiled=self._compiled,
        )
        assert isinstance(result, FormattedResult)  # noqa: S101
        self._out.result = result
        QUERY_RESULT_ROWS.labels(connector=self._out.connector_type or "unknown").observe(
            result.row_count
        )
        key = self._cache_key()
        if key is not None and self.deps.result_cache is not None:
            await self.deps.result_cache.set(
                key, result, self._execution.execution_ms if self._execution else None
            )
        return AgentStep.VALIDATE_RESULT

    async def _explain(self) -> AgentStep:
        if self._analysis_outcome is not None:
            # Analysis answers are stated from validated metrics only - no model narrative.
            outcome = self._analysis_outcome
            self._out.explanation = ResultExplanation(
                summary=outcome.summary[:600],
                highlights=[h[:300] for h in outcome.highlights[:5]],
            )
            self._out.explanation_source = "system"
            self._out.status = "succeeded"
            return AgentStep.STORE_AUDIT_EVENT
        result, description = self._out.result, self._out.description or ""
        assert result is not None  # noqa: S101
        # Only meaningful rows are narrated; missing data is stated by the system.
        columns, rows = explainable_data(result)
        notes = list(dict.fromkeys([*self._out.notes, *result.warnings]))
        try:
            explanation = await self.deps.llm.generate_structured(
                explanation_messages(
                    self._req.question,
                    description,
                    columns,
                    rows,
                    self._out.assumptions,
                    notes,
                ),
                ResultExplanation,
            )
            highlights = list(dict.fromkeys([*explanation.highlights, *(n[:300] for n in notes)]))
            self._out.explanation = explanation.model_copy(update={"highlights": highlights[:5]})
            self._out.explanation_source = "llm"
        except (LLMOutputError, LLMUnavailableError):
            # The answer is still valid without a narrative; never fail the query for it.
            self._out.explanation = ResultExplanation(
                summary=f"{result.row_count} row(s) returned for: {description}"[:600],
                highlights=[n[:300] for n in notes[:5]],
            )
            self._out.explanation_source = "system"
        self._out.status = "succeeded"
        return AgentStep.STORE_AUDIT_EVENT

    # ====================================================== validation-first
    def _plan_context(self) -> PlanContext:
        d = self.deps
        max_rows = min(
            d.settings.max_query_rows, self._adapter.config.max_rows if self._adapter else 1000
        )
        return PlanContext(
            tenant_id=d.ctx.tenant_id,
            tenant_slug=d.ctx.tenant_slug,
            tenant_settings=self._tenant_settings,
            connection_id=self._req.connection_id,
            semantic_version=self._scope.version if self._scope else 0,
            permissions=frozenset(p.value for p in d.ctx.permissions),
            today=d.today,
            max_rows=max_rows,
        )

    def _usable_entities(self) -> frozenset[str]:
        assert self._bundle is not None  # noqa: S101
        return frozenset(
            build_vocabulary(
                self._bundle, self._bindings, frozenset(p.value for p in self.deps.ctx.permissions)
            )
        )

    def _vocabulary_tokens(self) -> frozenset[str]:
        """Words of governed names - a "name" made only of these is not a company."""
        assert self._bundle is not None  # noqa: S101
        words: set[str] = {"record", "records", "data", "everything"}
        for name in self._usable_entities():
            entity = self._bundle.entities[name]
            words.update(tokenize(plural_noun(entity.entity)))
            names = [
                *(d.name for d in entity.dimensions),
                *(m.name for m in entity.measures),
                *(t.name for t in entity.time_fields),
            ]
            for item in names:
                words.update(tokenize(item))
            for synonym in entity.synonyms:
                words.update(tokenize(synonym))
        return frozenset(words | {w.rstrip("s") for w in words})

    async def _run_subplan(self, plan: QueryPlan, label: str) -> SubResult:
        """One governed sub-query: validate → compile → SQL safety → read-only execution."""
        assert self._bundle is not None and self._adapter is not None  # noqa: S101
        try:
            resolved = resolve_plan(
                plan,
                bundle=self._bundle,
                bindings=self._bindings,
                catalog=self._catalog,
                ctx=self._plan_context(),
            )
        except PlanValidationError as exc:
            raise PlanRejectedError(
                details={
                    "query": label,
                    "issues": [{"code": i.code.value, "message": i.message} for i in exc.issues],
                }
            ) from exc
        compiled = self._adapter.compile_query_plan(resolved)
        dialect = self._adapter.dialect
        sql = compiled.render(dialect)
        validate_sql(
            sql,
            build_sql_policy(
                self._bundle,
                resolved,
                dialect.name,
                max_limit=self.deps.settings.max_query_rows + 1,
            ),
        )
        result = await self._adapter.execute_readonly(
            compiled.statement,
            max_rows=resolved.limit,
            timeout_seconds=self.deps.settings.max_query_seconds,
            query_id=str(self._req.query_id),
        )
        wanted = self._resolution.filter if self._resolution is not None else None
        applied = (
            wanted is None
            or plan.entity != self._subject_entity()
            or any(f.ref == wanted.field for f in resolved.filters)
        )
        self._trace_queries.append(
            {
                "label": label,
                "entity": resolved.entity,
                "sql": sql,
                "params": compiled.parameters(dialect),
                "rows": result.row_count,
                "filter_applied": applied,
            }
        )
        return SubResult(label, plan, resolved, compiled, sql, result, applied)

    async def _rows(self, plan: QueryPlan, label: str) -> list[tuple[Any, ...]]:
        sub = await self._run_subplan(plan, label)
        return [tuple(r) for r in sub.result.rows]

    def _subject_entity(self) -> str | None:
        if self._analysis_request is not None:
            return self._analysis_request.subject
        return self._out.plan.entity if self._out.plan is not None else None

    def _clarify_with(self, question: str) -> NoReturn:
        self._out.status = "clarification"
        self._out.clarification_question = question[:300]
        raise _Stop

    async def _resolve(self, mention: Mention, subject: Entity) -> Resolution:
        """Resolve a named company (cached per run: plan corrections repeat this step)."""
        assert self._bundle is not None  # noqa: S101
        key = (mention.text.lower(), subject.entity)
        cached = self._resolutions.get(key)
        if cached is not None:
            return cached
        targets = name_targets(self._bundle, subject, self._usable_entities(), mention.kind)
        resolution = await resolve_mention(
            mention,
            targets,
            self._rows,
            max_candidates=self.deps.settings.entity_resolution_max_candidates,
        )
        self._resolutions[key] = resolution
        self._resolution = resolution
        chosen = resolution.chosen[0] if resolution.chosen else None
        self._out.entity = EntityOut(
            type=resolution.noun,
            mention=mention.text,
            name=chosen.name if chosen else None,
            resolved=resolution.resolved,
            match=resolution.match,
            keys=[chosen.key] if chosen and chosen.key is not None else [],
            candidates=[c.name for c in resolution.candidates[:6]],
            filter_field=resolution.filter_dim,
        )
        return resolution

    async def _detect_analysis(self) -> AgentStep:
        d = self.deps
        if not d.settings.analysis_enabled or self._req.mode != "execute":
            return AgentStep.RETRIEVE_SEMANTIC_CONTEXT
        if self._req.previous_plan is not None:  # follow-ups refine the previous plan
            return AgentStep.RETRIEVE_SEMANTIC_CONTEXT
        assert self._bundle is not None  # noqa: S101
        request = detect_analysis(self._req.question, self._bundle, self._usable_entities())
        if request is None:
            return AgentStep.RETRIEVE_SEMANTIC_CONTEXT
        subject = self._bundle.entities[request.subject]
        if not any(
            m.aggregation in {Aggregation.COUNT, Aggregation.COUNT_DISTINCT}
            for m in subject.measures
        ):
            return AgentStep.RETRIEVE_SEMANTIC_CONTEXT
        self._analysis_request = request
        return AgentStep.RESOLVE_ENTITIES

    async def _resolve_entities(self) -> AgentStep:
        assert self._bundle is not None and self._analysis_request is not None  # noqa: S101
        subject = self._bundle.entities[self._analysis_request.subject]
        mention = find_mention(self._req.question, self._vocabulary_tokens())
        if mention is not None:
            resolution = await self._resolve(mention, subject)
            if not resolution.resolved:
                self._clarify_with(resolution.clarification() or "Which record do you mean?")
        return AgentStep.RUN_ANALYSIS

    async def _run_analysis(self) -> AgentStep:
        d = self.deps
        request = self._analysis_request
        assert request is not None and self._bundle is not None  # noqa: S101
        subject = self._bundle.entities[request.subject]
        analyzer = BusinessAnalyzer(
            request,
            subject,
            entity=self._out.entity,
            entity_filter=self._resolution.filter if self._resolution else None,
            today=d.today,
            max_blank_ratio=d.settings.result_max_blank_ratio,
            run=self._run_subplan,
            chart_hint=chart_hint(self._req.question),
        )
        outcome = await analyzer.run()
        self._analysis_outcome = outcome
        main = outcome.main
        source = outcome.chart_source or main
        self._out.plan = main.plan
        self._out.resolved = main.resolved
        self._out.sql = main.sql
        self._out.sql_hash = hashlib.sha256(main.sql.encode()).hexdigest()
        self._out.assumptions = list(main.resolved.assumptions)
        self._out.row_filter_fields = sorted({rf.field for rf in main.resolved.row_filters})
        self._out.description = (
            f"{outcome.report.title}: {len(outcome.report.queries)} validated read-only queries "
            f"over {request.subject}"
        )
        self._out.result = format_result(
            source.resolved,
            source.compiled.output,
            source.result,
            None,
            self._bundle.policy,
            max_blank_ratio=d.settings.result_max_blank_ratio,
            noun=request.plural,
            chart_hint=chart_hint(self._req.question),
        )
        self._out.analysis = outcome.report
        self._out.timings["execution_ms"] = 0.0
        return AgentStep.VALIDATE_RESULT

    async def _select_fields(self) -> AgentStep:
        """Check the planner's fields against the question; resolve names and statuses."""
        plan = self._out.plan
        if plan is None or not plan.is_data_plan or plan.entity is None or self._bundle is None:
            return AgentStep.VALIDATE_QUERY_PLAN
        if plan.entity not in self._bundle.entities:
            return AgentStep.VALIDATE_QUERY_PLAN  # the validator refuses unknown entities
        question = self._req.question
        mention = find_mention(question, self._vocabulary_tokens())
        # "show leads by status" is answered from leads, whatever the planner picked.
        planned = plan
        plan, self._retargeted = retarget(
            plan, question, self._bundle, self._usable_entities(), mention
        )
        if self._retargeted is not None and (lost := dropped_filters(planned, plan)):
            subject = plural_noun(plan.entity or "")
            fields = ", ".join(f.replace("_", " ") for f in lost)
            self._out.notes.append(f"The filter on {fields} does not apply to {subject}.")
        self._out.plan = plan  # a clarification shows the plan it is about
        entity = self._bundle.entities[plan.entity or ""]
        selection = select_fields(plan, entity, question, self._bundle)
        self._out.notes = list(dict.fromkeys([*self._out.notes, *selection.notes]))
        if selection.clarification:
            self._clarify_with(selection.clarification)
        plan = selection.plan

        if mention is not None:
            resolution = await self._resolve(mention, entity)
            if not resolution.resolved or resolution.filter is None:
                self._clarify_with(resolution.clarification() or "Which record do you mean?")
            plan = _with_entity_filter(plan, resolution, mention)

        plan = await self._ground_statuses(plan, entity, mention)
        self._out.plan = QueryPlan.model_validate(plan.model_dump())
        return AgentStep.VALIDATE_QUERY_PLAN

    async def _ground_statuses(
        self, plan: QueryPlan, entity: Entity, mention: Mention | None
    ) -> QueryPlan:
        """ "pending leads": a status word must be a real value of a status-like field."""
        question = self._req.question
        if mention is not None:  # the company name's words are not statuses
            question = question[: mention.start] + " " + question[mention.end :]
        words = set(tokenize(question))
        strict, soft = words & STRICT_STATUS_WORDS, words & SOFT_STATUS_WORDS
        fields = [d for d in entity.dimensions if is_status_like(d) and not d.sensitive]
        count = next((m for m in entity.measures if m.aggregation is Aggregation.COUNT), None)
        if not (strict or soft) or not fields or count is None:
            return plan
        estimate = self._row_estimates.get(entity.source.logical_name.lower())
        if estimate is not None and estimate > self.deps.settings.value_grounding_max_rows:
            self._out.notes.extend(
                f"“{word}” could not be checked against the data (the table is too large)."
                for word in sorted(strict)
            )
            return plan
        values: dict[str, list[str]] = {}
        for dim in fields:
            rows = await self._rows(
                QueryPlan(
                    entity=entity.entity, dimensions=[dim.name], measures=[count.name], limit=60
                ),
                f"values of {dim.name}",
            )
            values[dim.name] = [str(r[0]).strip() for r in rows if not is_blank(r[0])]
        filters, unmatched = self._apply_status_words(plan, values, strict, soft)
        plan = plan.model_copy(update={"filters": filters})
        if unmatched:
            await self._unmatched_statuses(entity, count.name, values, unmatched)
        return plan

    def _apply_status_words(
        self,
        plan: QueryPlan,
        values: dict[str, list[str]],
        strict: set[str],
        soft: set[str],
    ) -> tuple[list[QueryFilter], list[str]]:
        filters = list(plan.filters)
        filtered = {f.field for f in filters}
        unmatched: list[str] = []
        for word in sorted(strict | soft):
            match = next(
                ((name, v) for name, vals in values.items() for v in vals if v.lower() == word),
                None,
            )
            if match is None:
                if word in strict:
                    unmatched.append(word)
                continue
            if match[0] not in filtered:
                filters.append(QueryFilter(field=match[0], op=FilterOp.EQ, value=match[1]))
                filtered.add(match[0])
                self._out.notes.append(
                    f"“{word}” was read as {match[0].replace('_', ' ')} = {match[1]}."
                )
        return filters, unmatched

    async def _unmatched_statuses(
        self, entity: Entity, count: str, values: dict[str, list[str]], unmatched: list[str]
    ) -> None:
        """Say the status does not exist. Answer anyway only when the result is empty
        regardless (the named company has no records at all); otherwise ask."""
        listing = "; ".join(
            f"{name.replace('_', ' ')}: {', '.join(vals[:6])}"
            for name, vals in values.items()
            if vals
        )
        plural = plural_noun(entity.entity)
        quoted = "”, “".join(unmatched)
        message = f"There is no “{quoted}” value for {plural}" + (
            f" ({listing})." if listing else "."
        )
        resolution = self._resolution
        if resolution is not None and resolution.resolved and resolution.filter is not None:
            total = await self._rows(
                QueryPlan(entity=entity.entity, measures=[count], filters=[resolution.filter]),
                f"{plural} for the named {resolution.noun}",
            )
            if not total or not total[0] or (total[0][0] or 0) == 0:
                name = resolution.chosen[0].name if resolution.chosen else resolution.mention.text
                self._out.notes.append(
                    f"{message} It was not applied - {name} has no {plural} at all."
                )
                return
        self._clarify_with(f"{message} Ask again with one of these values.")

    async def _validate_result(self) -> AgentStep:
        """The structured analytics: data quality, chart decision, validated metrics."""
        if self._analysis_outcome is not None:
            report = self._analysis_outcome.report
            if self._out.notes:
                merged = list(dict.fromkeys([*self._out.notes, *report.warnings]))
                report = report.model_copy(
                    update={
                        "warnings": merged,
                        "validation": report.validation.model_copy(update={"warnings": merged}),
                    }
                )
            self._out.analysis = report
            return AgentStep.GENERATE_EXPLANATION
        result, resolved = self._out.result, self._out.resolved
        assert result is not None and resolved is not None  # noqa: S101
        labels = {c.name: c.label for c in result.columns}
        chart = result.chart
        kind = "bar" if chart.type == "grouped_bar" else chart.type
        enabled = chart.enabled and kind in {"bar", "line", "pie", "scatter"}
        warnings = list(dict.fromkeys([*self._out.notes, *result.warnings]))
        rejected = [q for q in result.quality if not q.usable and q.role != "identifier"]
        wanted = self._resolution.filter if self._resolution else None
        self._out.analysis = AnalysisOut(
            intent=self._out.intent.intent if self._out.intent else "query",
            entity=self._out.entity,
            metrics=[
                MetricOut(
                    key=k.name,
                    label=k.label,
                    value=k.value if isinstance(k.value, int | float) else None,
                    format=k.format,
                    query="main",
                )
                for k in result.kpis
                if not isinstance(k.value, bool)
            ],
            dimensions=result.quality,
            warnings=warnings,
            chart=AnalysisChartOut(
                enabled=enabled,
                type=kind if enabled else None,  # type: ignore[arg-type]
                title=chart.title,
                dimension=labels.get(chart.x or ""),
                metric=labels.get(chart.y[0]) if chart.y else None,
                validation=chart.validation,
            ),
            validation=AnalysisValidationOut(
                data_validated=not rejected,
                entity_resolved=self._out.entity.resolved if self._out.entity else None,
                entity_filter_applied=(
                    any(f.ref == wanted.field for f in resolved.filters) if wanted else None
                ),
                chart_validated=enabled and chart.validation.valid,
                warnings=warnings,
            ),
            queries=[
                AnalysisQueryOut(label="main", sql=self._out.sql or "", row_count=result.row_count)
            ],
        )
        return AgentStep.GENERATE_EXPLANATION

    def _log_trace(self) -> None:
        """One structured line per request - why this answer, this chart, these numbers."""
        out, values = self._out, self.deps.settings.analytics_trace_values
        question = self._req.question
        result = out.result
        analysis = out.analysis
        trace: dict[str, Any] = {
            "query_id": str(out.query_id),
            "status": out.status,
            "question": question
            if values
            else {
                "chars": len(question),
                "sha256": hashlib.sha256(question.encode()).hexdigest()[:16],
            },
            "intent": out.intent.intent if out.intent else None,
            "analysis": self._analysis_request.kind if self._analysis_request else None,
            "entity": None
            if out.entity is None
            else {
                "type": out.entity.type,
                "match": out.entity.match,
                "resolved": out.entity.resolved,
                "candidates": len(out.entity.candidates),
                **({"mention": out.entity.mention, "name": out.entity.name} if values else {}),
            },
            "plan": out.plan.model_dump(mode="json", exclude_defaults=True) if out.plan else None,
            "retargeted": self._retargeted,
            "queries": [
                {k: v for k, v in q.items() if values or k != "params"} for q in self._trace_queries
            ],
            "main_sql": out.sql,
            "rows": result.row_count if result else None,
            "dimensions": [
                {
                    "name": q.name,
                    "role": q.role,
                    "total": q.total,
                    "null": q.null_count,
                    "blank": q.blank_count,
                    "distinct": q.distinct_values,
                    "usable": q.usable,
                    "reason": q.reason,
                }
                for q in (analysis.dimensions if analysis else [])
            ],
            "chart": None
            if analysis is None
            else {
                "type": analysis.chart.type,
                "enabled": analysis.chart.enabled,
                "valid": analysis.chart.validation.valid,
                "reasons": analysis.chart.validation.reasons,
            },
            "metrics": [
                {"key": m.key, **({"value": m.value} if values else {})}
                for m in (analysis.metrics if analysis else [])
            ],
            "unavailable": [u.key for u in analysis.unavailable] if analysis else [],
            "warnings": analysis.warnings if analysis else out.notes,
            "clarification": out.clarification_question
            if values
            else bool(out.clarification_question),
            "error": out.error.code if out.error else None,
        }
        trace_logger.info("analytics trace", extra={"trace": trace})

    async def _audit(self) -> AgentStep:
        out = self._out
        outcome = {
            "succeeded": AuditOutcome.SUCCESS,
            "planned": AuditOutcome.SUCCESS,
            "clarification": AuditOutcome.SUCCESS,
            "refused": AuditOutcome.DENIED,
        }.get(out.status, AuditOutcome.ERROR)
        if out.error and out.error.status_code == 403:
            outcome = AuditOutcome.DENIED
        await self.deps.audit.record(
            "analytics.query" if self._req.mode == "execute" else "analytics.explain",
            outcome,
            tenant_id=self.deps.ctx.tenant_id,
            actor_user_id=self.deps.ctx.user_id,
            resource_type="query",
            resource_id=str(out.query_id),
            details={
                "status": out.status,
                "connection_id": str(self._req.connection_id),
                "entity": out.resolved.entity if out.resolved else None,
                "plan_hash": out.resolved.plan_hash if out.resolved else None,
                "sql_hash": out.sql_hash,
                "row_count": out.result.row_count if out.result else None,
                "error_code": out.error.code if out.error else None,
                "refusal_reason": out.refusal_reason,
                "corrections": out.corrections,
                "cache_hit": out.cache_hit,
                "llm_calls": out.llm_usage.calls,
                "llm_tokens": out.llm_usage.total_tokens,
            },
        )
        return AgentStep.COMPLETE


class PlanRejectedError(AppError):
    """Terminal plan rejection (after the correction budget was used)."""

    status_code = 422
    code = "PLAN_INVALID"
    default_message = "The question could not be mapped to a valid query."


class PolicyBlockedError(AppError):
    status_code = 403
    code = "QUERY_NOT_ALLOWED"
    default_message = "The request is not permitted by data policy."


def _plan_error(result: PlanValidationOut, *, blocked: bool) -> AppError:
    details: dict[str, Any] = {
        "issues": [
            {"code": c, "message": m}
            for c, m in zip(result.issue_codes, result.issues, strict=True)
        ]
    }
    return PolicyBlockedError(details=details) if blocked else PlanRejectedError(details=details)


def chart_hint(question: str) -> ChartHint | None:
    """ "share of …" → pie candidate; "X vs Y" → scatter candidate (validation decides)."""
    if _SHARE.search(question):
        return "share"
    if _RELATION.search(question):
        return "relationship"
    return None


def _with_entity_filter(plan: QueryPlan, resolution: Resolution, mention: Mention) -> QueryPlan:
    """Replace any filter the planner guessed from the name with the resolved one."""
    assert resolution.filter is not None  # noqa: S101
    target = normalize_name(mention.text)
    kept = [
        f
        for f in plan.filters
        if f.field != resolution.filter.field
        and not (
            isinstance(f.value, str)
            and target
            and (target in normalize_name(f.value) or normalize_name(f.value) in target)
        )
    ]
    return plan.model_copy(update={"filters": [*kept, resolution.filter]})
