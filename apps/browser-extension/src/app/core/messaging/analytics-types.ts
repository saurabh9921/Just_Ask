/**
 * Analytics, history and feedback types shared by the side panel and the background
 * worker (Phase 5). DOM- and Angular-free: imported by both bundles.
 *
 * These mirror the API contracts in `packages/contracts/` in camelCase. The background
 * worker validates every API body before converting it (`background/analytics-parsers.ts`),
 * so the side panel can trust these shapes.
 */

export type Scalar = string | number | boolean | null;

export type OutcomeStatus =
  'succeeded' | 'planned' | 'clarification' | 'refused' | 'failed' | 'cancelled';

export type ValueFormat = 'currency' | 'number' | 'integer' | 'percent' | 'date' | 'text';

/** What a breakdown column holds: only categories (and time) make chart categories. */
export type DimensionRole = 'category' | 'name' | 'identifier' | 'free_text' | 'time';

export interface ResultColumn {
  readonly name: string;
  readonly label: string;
  readonly kind: 'dimension' | 'time' | 'measure' | 'previous' | 'change' | 'change_pct';
  readonly type: string;
  readonly format: ValueFormat | null;
  readonly timeGrain: string | null;
  /** Dimensions only; `null` from older responses. */
  readonly role: DimensionRole | null;
}

export interface KpiCard {
  readonly name: string;
  readonly label: string;
  readonly value: Scalar;
  readonly format: ValueFormat | null;
  readonly previous: Scalar;
  readonly change: Scalar;
  readonly changePct: number | null;
  readonly scope: 'value' | 'total';
}

export type ChartType = 'kpi' | 'bar' | 'grouped_bar' | 'line' | 'table' | 'pie' | 'scatter';

/** Why the server did (not) allow a chart. */
export interface ChartValidation {
  readonly valid: boolean;
  readonly reasons: readonly string[];
}

export interface ChartSpec {
  readonly type: ChartType;
  readonly x: string | null;
  readonly y: readonly string[];
  readonly series: string | null;
  /** `false` when validation rejected the chart: `type` is then the fallback view. */
  readonly enabled: boolean;
  readonly title: string | null;
  readonly validation: ChartValidation;
}

/** Data quality of one breakdown field: how much of it is actually recorded. */
export interface DimensionQuality {
  readonly name: string;
  readonly label: string;
  readonly role: DimensionRole;
  readonly total: number;
  readonly nullCount: number;
  readonly blankCount: number;
  readonly distinctValues: number;
  readonly meaningfulRatio: number;
  readonly weighted: boolean;
  readonly usable: boolean;
  readonly reason: string | null;
}

export interface FormattedResult {
  readonly columns: readonly ResultColumn[];
  readonly rows: readonly (readonly Scalar[])[];
  readonly rowCount: number;
  readonly truncated: boolean;
  readonly kpis: readonly KpiCard[];
  readonly chart: ChartSpec;
  readonly redactedColumns: readonly string[];
  readonly quality: readonly DimensionQuality[];
  /** Plain-language data-quality notes ("Source is missing for 14 of 28 leads."). */
  readonly warnings: readonly string[];
}

// ------------------------------------------------------------------ analysis

/** A business entity named in the question ("pending metals co"). */
export interface AnalysisEntity {
  readonly type: string;
  readonly mention: string;
  readonly name: string | null;
  readonly resolved: boolean;
  readonly match: 'exact' | 'normalized' | 'id' | 'ambiguous' | 'suggestions' | 'not_found';
  readonly keys: readonly Scalar[];
  readonly candidates: readonly string[];
  readonly filterField: string | null;
}

/** A figure computed from a validated query result (`query` names the query). */
export interface AnalysisMetric {
  readonly key: string;
  readonly label: string;
  readonly value: number | null;
  readonly format: ValueFormat | null;
  readonly source: 'query_result';
  readonly query: string;
  readonly validated: boolean;
  readonly detail: string | null;
}

/** A figure that could not be computed, and why (never estimated). */
export interface UnavailableMetric {
  readonly key: string;
  readonly label: string;
  readonly reason: string;
}

export interface AnalysisInsight {
  readonly text: string;
  readonly metrics: readonly string[];
  readonly kind: 'fact' | 'observation';
}

export interface ChartPoint {
  readonly label: string;
  readonly value: number | null;
}

export interface AnalysisChart {
  readonly enabled: boolean;
  readonly type: 'bar' | 'line' | 'pie' | 'scatter' | null;
  readonly title: string | null;
  readonly dimension: string | null;
  readonly metric: string | null;
  /** The plotted points; empty when the chart is drawn from the result rows. */
  readonly data: readonly ChartPoint[];
  readonly validation: ChartValidation;
}

export interface AnalysisValidation {
  readonly dataValidated: boolean;
  /** `null`: the question names no entity. */
  readonly entityResolved: boolean | null;
  readonly entityFilterApplied: boolean | null;
  readonly chartValidated: boolean;
  readonly unsupportedClaims: readonly string[];
  readonly warnings: readonly string[];
}

export interface AnalysisQuery {
  readonly label: string;
  readonly sql: string;
  readonly rowCount: number;
  readonly validated: boolean;
}

/**
 * The structured, validated answer next to the result: built by the server from
 * validated query results (never by a model). `title` is set for business analyses
 * ("Lead Analysis — Pending Metals Co"), whose chart comes with its own `data`.
 */
export interface Analysis {
  readonly intent: string;
  readonly title: string | null;
  readonly entity: AnalysisEntity | null;
  readonly metrics: readonly AnalysisMetric[];
  readonly unavailable: readonly UnavailableMetric[];
  readonly dimensions: readonly DimensionQuality[];
  readonly insights: readonly AnalysisInsight[];
  readonly warnings: readonly string[];
  readonly chart: AnalysisChart;
  readonly validation: AnalysisValidation;
  readonly queries: readonly AnalysisQuery[];
}

export interface DateWindow {
  readonly field: string;
  readonly start: string;
  readonly endExclusive: string;
  readonly label: string;
}

export interface AppliedFilter {
  readonly field: string;
  readonly op: string;
  readonly value: unknown;
}

export interface StepRecord {
  readonly step: string;
  readonly status: 'completed' | 'failed' | 'skipped';
  readonly durationMs: number;
}

export interface QueryErrorInfo {
  readonly code: string;
  readonly message: string;
  readonly status: number;
}

export interface QueryResponse {
  readonly queryId: string;
  readonly status: OutcomeStatus;
  readonly question: string;
  readonly intent: string | null;
  readonly connection: {
    readonly id: string;
    readonly name: string | null;
    readonly connectorType: string | null;
  };
  readonly semanticVersion: number | null;
  readonly dataFreshness: string | null;
  /** The validated plan as returned by the API (displayed, never interpreted). */
  readonly plan: Readonly<Record<string, unknown>> | null;
  readonly description: string | null;
  readonly assumptions: readonly string[];
  readonly sourceEntities: readonly string[];
  readonly appliedFilters: {
    readonly dateWindow: DateWindow | null;
    readonly previousWindow: DateWindow | null;
    readonly filters: readonly AppliedFilter[];
  };
  readonly securityScope: {
    readonly tenant: string;
    readonly rowFilterFields: readonly string[];
    readonly readOnly: boolean;
  };
  readonly validation: {
    readonly planValid: boolean;
    readonly sqlValidated: boolean;
    readonly corrections: number;
    readonly confidence: number | null;
  };
  readonly sql: string | null;
  readonly previousSql: string | null;
  readonly cost: {
    readonly source: string;
    readonly estimatedCost: number | null;
    readonly estimatedRows: number | null;
  } | null;
  readonly result: FormattedResult | null;
  readonly rowsReturned: number | null;
  readonly explanation: {
    readonly summary: string;
    readonly highlights: readonly string[];
    readonly source: 'llm' | 'system';
  } | null;
  readonly clarificationQuestion: string | null;
  readonly refusalReason: string | null;
  readonly error: QueryErrorInfo | null;
  readonly timings: {
    readonly totalMs: number | null;
    readonly executionMs: number | null;
    readonly llmMs: number | null;
  };
  readonly steps: readonly StepRecord[];
  readonly replayed: boolean;
  /** Served from the tenant/scope-keyed result cache (Phase 6). */
  readonly cache: { readonly hit: boolean; readonly ageSeconds: number | null };
  /** Validated metrics, insights and chart decision; `null` from older responses. */
  readonly analysis: Analysis | null;
}

export interface ProgressEvent {
  readonly queryId: string;
  readonly step: string;
  readonly status: 'started' | 'completed' | 'failed' | 'skipped';
  readonly message: string;
  readonly elapsedMs: number;
}

export type QueryMode = 'execute' | 'plan';

export interface AskRequest {
  readonly question: string;
  readonly connectionId: string | null;
  /** Set for follow-ups: the earlier query whose plan is refined. */
  readonly parentQueryId: string | null;
  /** The chat the question belongs to; history groups questions by it. */
  readonly conversationId: string | null;
  readonly mode: QueryMode;
}

// ------------------------------------------------------------------ history

export interface HistoryItem {
  readonly queryId: string;
  readonly conversationId: string | null;
  readonly question: string;
  readonly status: string;
  readonly intent: string | null;
  readonly connectionId: string | null;
  readonly connectionName: string | null;
  readonly rowCount: number | null;
  readonly totalMs: number | null;
  readonly errorCode: string | null;
  readonly saved: boolean;
  readonly feedbackRating: -1 | 1 | null;
  readonly createdAt: string;
  readonly completedAt: string | null;
}

export interface HistoryPage {
  readonly items: readonly HistoryItem[];
  readonly total: number;
  readonly limit: number;
  readonly offset: number;
}

export interface HistoryQuery {
  readonly limit: number;
  readonly offset: number;
  readonly saved?: boolean;
  readonly search?: string;
}

export interface HistoryDetail extends HistoryItem {
  readonly response: QueryResponse | null;
  readonly responseExpired: boolean;
}

/** One chat in the history list; its first question is the title. */
export interface ConversationItem {
  readonly conversationId: string;
  readonly title: string;
  readonly questionCount: number;
  readonly lastQuestion: string;
  readonly lastStatus: string;
  readonly connectionId: string | null;
  readonly connectionName: string | null;
  /** Any of its questions is saved. */
  readonly saved: boolean;
  readonly startedAt: string;
  readonly lastActivityAt: string;
}

export interface ConversationPage {
  readonly items: readonly ConversationItem[];
  readonly total: number;
  readonly limit: number;
  readonly offset: number;
}

/** The whole chat: every question with its answer (while cached), oldest first. */
export interface ConversationDetail extends ConversationItem {
  readonly messages: readonly HistoryDetail[];
}

// ------------------------------------------------------------------ feedback

export const FEEDBACK_CATEGORIES = [
  'accurate',
  'helpful',
  'fast',
  'wrong_numbers',
  'wrong_data_source',
  'wrong_period',
  'wrong_filter',
  'missing_data',
  'confusing',
  'slow',
  'other',
] as const;

export type FeedbackCategory = (typeof FEEDBACK_CATEGORIES)[number];

export interface FeedbackInput {
  readonly queryId: string;
  readonly rating: -1 | 1;
  readonly categories: readonly FeedbackCategory[];
  readonly comment: string | null;
}

export interface FeedbackResult {
  readonly queryId: string;
  readonly rating: -1 | 1;
  readonly categories: readonly string[];
  readonly remembered: boolean;
}

// ------------------------------------------------------------------ streaming port

/** Long-lived `chrome.runtime.Port` name used for streamed questions. */
export const ANALYTICS_PORT = 'eia.analytics';

/** Side panel -> background over the analytics port. */
export type StreamCommand =
  | { readonly kind: 'ask'; readonly ref: string; readonly request: AskRequest }
  | { readonly kind: 'cancel'; readonly ref: string };

export interface StreamError {
  readonly code: string;
  readonly message: string;
  readonly status: number;
  readonly retryAfterSeconds?: number;
}

/** Background -> side panel over the analytics port. */
export type StreamEvent =
  | { readonly kind: 'accepted'; readonly ref: string; readonly queryId: string }
  | { readonly kind: 'progress'; readonly ref: string; readonly event: ProgressEvent }
  | { readonly kind: 'result'; readonly ref: string; readonly response: QueryResponse }
  | {
      readonly kind: 'error';
      readonly ref: string;
      readonly error: StreamError;
      readonly response: QueryResponse | null;
    }
  | { readonly kind: 'transport'; readonly ref: string; readonly transport: 'websocket' | 'http' };

export const STREAM_REF = /^[A-Za-z0-9_-]{1,64}$/;

// ------------------------------------------------------------ background exports

export type ExportFileFormat = 'csv' | 'json';
/** `result`: the rows of the answer; `all_rows`: the same query up to the platform cap. */
export type ExportScope = 'result' | 'all_rows';
export type ExportStatus = 'pending' | 'running' | 'succeeded' | 'failed' | 'expired' | 'cancelled';

export interface ExportRequest {
  readonly queryId: string;
  readonly format: ExportFileFormat;
  readonly scope: ExportScope;
}

export interface ExportJob {
  readonly exportId: string;
  readonly queryId: string;
  readonly format: ExportFileFormat;
  readonly scope: ExportScope;
  readonly status: ExportStatus;
  readonly rowCount: number | null;
  readonly byteSize: number | null;
  readonly truncated: boolean | null;
  readonly fileName: string | null;
  readonly errorCode: string | null;
  readonly createdAt: string;
  readonly expiresAt: string | null;
}

export const EXPORT_TERMINAL: readonly ExportStatus[] = [
  'succeeded',
  'failed',
  'expired',
  'cancelled',
];
