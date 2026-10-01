/**
 * Validating parsers for analytics, history and feedback API bodies (snake_case JSON
 * → camelCase types). API bodies are untrusted input: shapes are checked, strings
 * and lists are bounded, and result cells must be JSON scalars. A mismatch throws
 * {@link ApiResponseError} instead of passing surprising data to the side panel.
 */
import type {
  Analysis,
  AnalysisChart,
  AnalysisEntity,
  AnalysisInsight,
  AnalysisMetric,
  AppliedFilter,
  ChartSpec,
  ChartType,
  ChartValidation,
  ConversationDetail,
  ConversationItem,
  ConversationPage,
  DateWindow,
  DimensionQuality,
  DimensionRole,
  ExportJob,
  ExportStatus,
  FeedbackResult,
  FormattedResult,
  HistoryDetail,
  HistoryItem,
  HistoryPage,
  KpiCard,
  OutcomeStatus,
  ProgressEvent,
  QueryErrorInfo,
  QueryResponse,
  ResultColumn,
  Scalar,
  StepRecord,
  ValueFormat,
} from '../app/core/messaging/analytics-types';
import { ApiResponseError } from './api-client';

const MAX_ROWS = 100_000;
const MAX_COLUMNS = 200;
const MAX_TEXT = 20_000;

type Json = Record<string, unknown>;

function isRecord(value: unknown): value is Json {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function obj(value: unknown, what: string): Json {
  if (!isRecord(value)) {
    throw new ApiResponseError(what);
  }
  return value;
}

function str(value: unknown, what: string, max = MAX_TEXT): string {
  if (typeof value !== 'string' || value.length > max) {
    throw new ApiResponseError(what);
  }
  return value;
}

function optStr(value: unknown, what: string, max = MAX_TEXT): string | null {
  return value === null || value === undefined ? null : str(value, what, max);
}

function num(value: unknown, what: string): number {
  if (typeof value !== 'number' || !Number.isFinite(value)) {
    throw new ApiResponseError(what);
  }
  return value;
}

function optNum(value: unknown, what: string): number | null {
  return value === null || value === undefined ? null : num(value, what);
}

function bool(value: unknown, what: string): boolean {
  if (typeof value !== 'boolean') {
    throw new ApiResponseError(what);
  }
  return value;
}

function list(value: unknown, what: string, max = 1000): readonly unknown[] {
  if (value === undefined || value === null) {
    return [];
  }
  if (!Array.isArray(value) || value.length > max) {
    throw new ApiResponseError(what);
  }
  return value;
}

function strings(value: unknown, what: string, max = 200): readonly string[] {
  return list(value, what, max).map((v) => str(v, what, 2000));
}

function scalar(value: unknown, what: string): Scalar {
  if (value === null || typeof value === 'boolean' || typeof value === 'string') {
    if (typeof value === 'string' && value.length > MAX_TEXT) {
      throw new ApiResponseError(what);
    }
    return value;
  }
  return num(value, what);
}

function oneOf<T extends string>(value: unknown, allowed: readonly T[], what: string): T {
  if (typeof value !== 'string' || !(allowed as readonly string[]).includes(value)) {
    throw new ApiResponseError(what);
  }
  return value as T;
}

const OUTCOMES: readonly OutcomeStatus[] = [
  'succeeded',
  'planned',
  'clarification',
  'refused',
  'failed',
  'cancelled',
];
const FORMATS: readonly ValueFormat[] = [
  'currency',
  'number',
  'integer',
  'percent',
  'date',
  'text',
];
const COLUMN_KINDS: readonly ResultColumn['kind'][] = [
  'dimension',
  'time',
  'measure',
  'previous',
  'change',
  'change_pct',
];
const CHART_TYPES: readonly ChartType[] = [
  'kpi',
  'bar',
  'grouped_bar',
  'line',
  'table',
  'pie',
  'scatter',
];
const ROLES: readonly DimensionRole[] = ['category', 'name', 'identifier', 'free_text', 'time'];
const MATCHES: readonly AnalysisEntity['match'][] = [
  'exact',
  'normalized',
  'id',
  'ambiguous',
  'suggestions',
  'not_found',
];
const ANALYSIS_CHARTS: readonly NonNullable<AnalysisChart['type']>[] = [
  'bar',
  'line',
  'pie',
  'scatter',
];

function format(value: unknown): ValueFormat | null {
  return typeof value === 'string' && (FORMATS as readonly string[]).includes(value)
    ? (value as ValueFormat)
    : null;
}

function parseColumn(value: unknown): ResultColumn {
  const c = obj(value, 'result column');
  return {
    name: str(c['name'], 'result column', 200),
    label: str(c['label'], 'result column', 300),
    kind: oneOf(c['kind'], COLUMN_KINDS, 'result column'),
    type: str(c['type'], 'result column', 32),
    format: format(c['format']),
    timeGrain: optStr(c['time_grain'], 'result column', 16),
    role: typeof c['role'] === 'string' ? oneOf(c['role'], ROLES, 'result column') : null,
  };
}

function parseKpi(value: unknown): KpiCard {
  const k = obj(value, 'KPI');
  return {
    name: str(k['name'], 'KPI', 200),
    label: str(k['label'], 'KPI', 300),
    value: scalar(k['value'], 'KPI'),
    format: format(k['format']),
    previous: scalar(k['previous'] ?? null, 'KPI'),
    change: scalar(k['change'] ?? null, 'KPI'),
    changePct: optNum(k['change_pct'], 'KPI'),
    scope: k['scope'] === 'total' ? 'total' : 'value',
  };
}

/** Older responses carry no validation: their charts were allowed. */
function parseChartValidation(value: unknown): ChartValidation {
  if (value === null || value === undefined) {
    return { valid: true, reasons: [] };
  }
  const v = obj(value, 'chart validation');
  return {
    valid: bool(v['valid'], 'chart validation'),
    reasons: strings(v['reasons'], 'chart validation', 20),
  };
}

function parseChart(value: unknown): ChartSpec {
  const c = obj(value, 'chart');
  return {
    type: oneOf(c['type'], CHART_TYPES, 'chart'),
    x: optStr(c['x'], 'chart', 200),
    y: strings(c['y'], 'chart', 50),
    series: optStr(c['series'], 'chart', 200),
    enabled: c['enabled'] !== false,
    title: optStr(c['title'], 'chart', 300),
    validation: parseChartValidation(c['validation']),
  };
}

function parseQuality(value: unknown): DimensionQuality {
  const q = obj(value, 'dimension quality');
  const what = 'dimension quality';
  return {
    name: str(q['name'], what, 200),
    label: str(q['label'], what, 300),
    role: oneOf(q['role'], ROLES, what),
    total: num(q['total'], what),
    nullCount: num(q['null_count'], what),
    blankCount: num(q['blank_count'], what),
    distinctValues: num(q['distinct_values'], what),
    meaningfulRatio: num(q['meaningful_ratio'], what),
    weighted: bool(q['weighted'], what),
    usable: bool(q['usable'], what),
    reason: optStr(q['reason'], what, 500),
  };
}

export function parseResult(value: unknown): FormattedResult {
  const r = obj(value, 'result');
  const columns = list(r['columns'], 'result', MAX_COLUMNS).map(parseColumn);
  const rows = list(r['rows'], 'result', MAX_ROWS).map((row) => {
    const cells = list(row, 'result row', MAX_COLUMNS);
    if (cells.length !== columns.length) {
      throw new ApiResponseError('result row');
    }
    return cells.map((cell) => scalar(cell, 'result cell'));
  });
  return {
    columns,
    rows,
    rowCount: num(r['row_count'], 'result'),
    truncated: bool(r['truncated'], 'result'),
    kpis: list(r['kpis'], 'result', 50).map(parseKpi),
    chart: parseChart(r['chart']),
    redactedColumns: strings(r['redacted_columns'], 'result'),
    quality: list(r['quality'], 'result', MAX_COLUMNS).map(parseQuality),
    warnings: strings(r['warnings'], 'result', 50),
  };
}

// ------------------------------------------------------------------ analysis

function parseEntity(value: unknown): AnalysisEntity | null {
  if (value === null || value === undefined) {
    return null;
  }
  const e = obj(value, 'analysis entity');
  const what = 'analysis entity';
  return {
    type: str(e['type'], what, 100),
    mention: str(e['mention'], what, 500),
    name: optStr(e['name'], what, 500),
    resolved: bool(e['resolved'], what),
    match: oneOf(e['match'], MATCHES, what),
    keys: list(e['keys'], what, 100).map((k) => scalar(k, what)),
    candidates: strings(e['candidates'], what, 50),
    filterField: optStr(e['filter_field'], what, 200),
  };
}

function parseMetric(value: unknown): AnalysisMetric {
  const m = obj(value, 'metric');
  return {
    key: str(m['key'], 'metric', 200),
    label: str(m['label'], 'metric', 300),
    value: optNum(m['value'], 'metric'),
    format: format(m['format']),
    source: oneOf(m['source'], ['query_result'], 'metric'),
    query: str(m['query'], 'metric', 300),
    validated: bool(m['validated'], 'metric'),
    detail: optStr(m['detail'], 'metric', 1000),
  };
}

function parseInsight(value: unknown): AnalysisInsight {
  const i = obj(value, 'insight');
  return {
    text: str(i['text'], 'insight', 2000),
    metrics: strings(i['metrics'], 'insight', 50),
    kind: oneOf(i['kind'], ['fact', 'observation'], 'insight'),
  };
}

function parseAnalysisChart(value: unknown): AnalysisChart {
  const c = obj(value, 'analysis chart');
  const what = 'analysis chart';
  return {
    enabled: bool(c['enabled'], what),
    type:
      c['type'] === null || c['type'] === undefined
        ? null
        : oneOf(c['type'], ANALYSIS_CHARTS, what),
    title: optStr(c['title'], what, 300),
    dimension: optStr(c['dimension'], what, 300),
    metric: optStr(c['metric'], what, 300),
    data: list(c['data'], what, 500).map((point) => {
      const p = obj(point, what);
      return { label: str(p['label'], what, 500), value: optNum(p['value'], what) };
    }),
    validation: parseChartValidation(c['validation']),
  };
}

function optBool(value: unknown, what: string): boolean | null {
  return value === null || value === undefined ? null : bool(value, what);
}

export function parseAnalysis(value: unknown): Analysis | null {
  if (value === null || value === undefined) {
    return null;
  }
  const a = obj(value, 'analysis');
  const v = obj(a['validation'], 'analysis');
  return {
    intent: str(a['intent'], 'analysis', 100),
    title: optStr(a['title'], 'analysis', 500),
    entity: parseEntity(a['entity']),
    metrics: list(a['metrics'], 'analysis', 200).map(parseMetric),
    unavailable: list(a['unavailable'], 'analysis', 100).map((u) => {
      const m = obj(u, 'unavailable metric');
      return {
        key: str(m['key'], 'unavailable metric', 200),
        label: str(m['label'], 'unavailable metric', 300),
        reason: str(m['reason'], 'unavailable metric', 1000),
      };
    }),
    dimensions: list(a['dimensions'], 'analysis', MAX_COLUMNS).map(parseQuality),
    insights: list(a['insights'], 'analysis', 100).map(parseInsight),
    warnings: strings(a['warnings'], 'analysis', 50),
    chart: parseAnalysisChart(a['chart']),
    validation: {
      dataValidated: bool(v['data_validated'], 'analysis'),
      entityResolved: optBool(v['entity_resolved'], 'analysis'),
      entityFilterApplied: optBool(v['entity_filter_applied'], 'analysis'),
      chartValidated: bool(v['chart_validated'], 'analysis'),
      unsupportedClaims: strings(v['unsupported_claims'], 'analysis', 50),
      warnings: strings(v['warnings'], 'analysis', 50),
    },
    queries: list(a['queries'], 'analysis', 50).map((q) => {
      const r = obj(q, 'analysis query');
      return {
        label: str(r['label'], 'analysis query', 300),
        sql: str(r['sql'], 'analysis query'),
        rowCount: num(r['row_count'], 'analysis query'),
        validated: bool(r['validated'], 'analysis query'),
      };
    }),
  };
}

function parseWindow(value: unknown): DateWindow | null {
  if (value === null || value === undefined) {
    return null;
  }
  const w = obj(value, 'date window');
  return {
    field: str(w['field'], 'date window', 200),
    start: str(w['start'], 'date window', 32),
    endExclusive: str(w['end_exclusive'], 'date window', 32),
    label: str(w['label'], 'date window', 200),
  };
}

function parseFilter(value: unknown): AppliedFilter {
  const f = obj(value, 'filter');
  return {
    field: str(f['field'], 'filter', 200),
    op: str(f['op'], 'filter', 32),
    value: f['value'],
  };
}

function parseStep(value: unknown): StepRecord {
  const s = obj(value, 'step');
  return {
    step: str(s['step'], 'step', 64),
    status: oneOf(s['status'], ['completed', 'failed', 'skipped'], 'step'),
    durationMs: num(s['duration_ms'], 'step'),
  };
}

function parseError(value: unknown): QueryErrorInfo | null {
  if (value === null || value === undefined) {
    return null;
  }
  const e = obj(value, 'query error');
  return {
    code: str(e['code'], 'query error', 64),
    message: str(e['message'], 'query error', 500),
    status: typeof e['status_code'] === 'number' ? e['status_code'] : 500,
  };
}

export function parseQueryResponse(body: unknown): QueryResponse {
  const b = obj(body, 'query');
  const connection = obj(b['connection'], 'query');
  const applied = isRecord(b['applied_filters']) ? b['applied_filters'] : {};
  const scope = obj(b['security_scope'], 'query');
  const validation = obj(b['validation'], 'query');
  const timings = isRecord(b['timings']) ? b['timings'] : {};
  const cost = b['cost'];
  const explanation = b['explanation'];
  const plan = b['plan'];
  return {
    queryId: str(b['query_id'], 'query', 64),
    status: oneOf(b['status'], OUTCOMES, 'query'),
    question: str(b['question'], 'query', 2000),
    intent: optStr(b['intent'], 'query', 64),
    connection: {
      id: str(connection['id'], 'query', 64),
      name: optStr(connection['name'], 'query', 200),
      connectorType: optStr(connection['connector_type'], 'query', 32),
    },
    semanticVersion: optNum(b['semantic_version'], 'query'),
    dataFreshness: optStr(b['data_freshness'], 'query', 64),
    plan: isRecord(plan) ? plan : null,
    description: optStr(b['description'], 'query', 2000),
    assumptions: strings(b['assumptions'], 'query', 50),
    sourceEntities: strings(b['source_entities'], 'query', 20),
    appliedFilters: {
      dateWindow: parseWindow(applied['date_window']),
      previousWindow: parseWindow(applied['previous_window']),
      filters: list(applied['filters'], 'query', 50).map(parseFilter),
    },
    securityScope: {
      tenant: str(scope['tenant'], 'query', 63),
      rowFilterFields: strings(scope['row_filter_fields'], 'query', 20),
      readOnly: scope['read_only'] !== false,
    },
    validation: {
      planValid: bool(validation['plan_valid'], 'query'),
      sqlValidated: bool(validation['sql_validated'], 'query'),
      corrections: typeof validation['corrections'] === 'number' ? validation['corrections'] : 0,
      confidence: optNum(validation['confidence'], 'query'),
    },
    sql: optStr(b['sql'], 'query'),
    previousSql: optStr(b['previous_sql'], 'query'),
    cost: isRecord(cost)
      ? {
          source: str(cost['source'], 'query', 32),
          estimatedCost: optNum(cost['estimated_cost'], 'query'),
          estimatedRows: optNum(cost['estimated_rows'], 'query'),
        }
      : null,
    result: b['result'] === null || b['result'] === undefined ? null : parseResult(b['result']),
    rowsReturned: optNum(b['rows_returned'], 'query'),
    explanation: isRecord(explanation)
      ? {
          summary: str(explanation['summary'], 'query', 2000),
          highlights: strings(explanation['highlights'], 'query', 10),
          source: explanation['source'] === 'llm' ? 'llm' : 'system',
        }
      : null,
    clarificationQuestion: optStr(b['clarification_question'], 'query', 1000),
    refusalReason: optStr(b['refusal_reason'], 'query', 64),
    error: parseError(b['error']),
    timings: {
      totalMs: optNum(timings['total_ms'], 'query'),
      executionMs: optNum(timings['execution_ms'], 'query'),
      llmMs: optNum(timings['llm_ms'], 'query'),
    },
    steps: list(b['steps'], 'query', 64).map(parseStep),
    replayed: b['replayed'] === true,
    cache: parseCache(b['cache']),
    analysis: parseAnalysis(b['analysis']),
  };
}

function parseCache(value: unknown): QueryResponse['cache'] {
  if (!isRecord(value)) {
    return { hit: false, ageSeconds: null };
  }
  return { hit: value['hit'] === true, ageSeconds: optNum(value['age_seconds'], 'cache') };
}

const EXPORT_STATUSES: readonly ExportStatus[] = [
  'pending',
  'running',
  'succeeded',
  'failed',
  'expired',
  'cancelled',
];

export function parseExportJob(body: unknown): ExportJob {
  const e = obj(body, 'export');
  const truncated = e['truncated'];
  return {
    exportId: str(e['export_id'], 'export', 64),
    queryId: str(e['query_id'], 'export', 64),
    format: oneOf(e['format'], ['csv', 'json'], 'export'),
    scope: oneOf(e['scope'], ['result', 'all_rows'], 'export'),
    status: oneOf(e['status'], EXPORT_STATUSES, 'export'),
    rowCount: optNum(e['row_count'], 'export'),
    byteSize: optNum(e['byte_size'], 'export'),
    truncated: typeof truncated === 'boolean' ? truncated : null,
    fileName: optStr(e['file_name'], 'export', 255),
    errorCode: optStr(e['error_code'], 'export', 64),
    createdAt: str(e['created_at'], 'export', 64),
    expiresAt: optStr(e['expires_at'], 'export', 64),
  };
}

/** Only same-API, token-download paths are accepted (never an arbitrary URL). */
const DOWNLOAD_PATH = /^\/api\/v1\/exports\/download\/[A-Za-z0-9_-]{20,128}$/;

export function parseDownloadToken(body: unknown): { readonly path: string } {
  const t = obj(body, 'download');
  const path = str(t['url'], 'download', 300);
  if (!DOWNLOAD_PATH.test(path)) {
    throw new ApiResponseError('download');
  }
  return { path };
}

export function parseProgressEvent(body: unknown): ProgressEvent {
  const e = obj(body, 'progress');
  return {
    queryId: str(e['query_id'], 'progress', 64),
    step: str(e['step'], 'progress', 64),
    status: oneOf(e['status'], ['started', 'completed', 'failed', 'skipped'], 'progress'),
    message: str(e['message'], 'progress', 300),
    elapsedMs: num(e['elapsed_ms'], 'progress'),
  };
}

function rating(value: unknown): -1 | 1 | null {
  return value === 1 || value === -1 ? value : null;
}

export function parseHistoryItem(body: unknown): HistoryItem {
  const h = obj(body, 'history');
  return {
    queryId: str(h['query_id'], 'history', 64),
    conversationId: optStr(h['conversation_id'], 'history', 64),
    question: str(h['question'], 'history', 2000),
    status: str(h['status'], 'history', 32),
    intent: optStr(h['intent'], 'history', 64),
    connectionId: optStr(h['connection_id'], 'history', 64),
    connectionName: optStr(h['connection_name'], 'history', 200),
    rowCount: optNum(h['row_count'], 'history'),
    totalMs: optNum(h['total_ms'], 'history'),
    errorCode: optStr(h['error_code'], 'history', 64),
    saved: bool(h['saved'], 'history'),
    feedbackRating: rating(h['feedback_rating']),
    createdAt: str(h['created_at'], 'history', 64),
    completedAt: optStr(h['completed_at'], 'history', 64),
  };
}

export function parseHistoryPage(body: unknown): HistoryPage {
  const p = obj(body, 'history page');
  return {
    items: list(p['items'], 'history page', 500).map(parseHistoryItem),
    total: num(p['total'], 'history page'),
    limit: num(p['limit'], 'history page'),
    offset: num(p['offset'], 'history page'),
  };
}

export function parseHistoryDetail(body: unknown): HistoryDetail {
  const d = obj(body, 'history');
  const response = d['response'];
  return {
    ...parseHistoryItem(d),
    response: response === null || response === undefined ? null : parseQueryResponse(response),
    responseExpired: d['response_expired'] === true,
  };
}

export function parseConversationItem(body: unknown): ConversationItem {
  const c = obj(body, 'conversation');
  return {
    conversationId: str(c['conversation_id'], 'conversation', 64),
    title: str(c['title'], 'conversation', 2000),
    questionCount: num(c['question_count'], 'conversation'),
    lastQuestion: str(c['last_question'], 'conversation', 2000),
    lastStatus: str(c['last_status'], 'conversation', 32),
    connectionId: optStr(c['connection_id'], 'conversation', 64),
    connectionName: optStr(c['connection_name'], 'conversation', 200),
    saved: bool(c['saved'], 'conversation'),
    startedAt: str(c['started_at'], 'conversation', 64),
    lastActivityAt: str(c['last_activity_at'], 'conversation', 64),
  };
}

export function parseConversationPage(body: unknown): ConversationPage {
  const p = obj(body, 'conversation page');
  return {
    items: list(p['items'], 'conversation page', 500).map(parseConversationItem),
    total: num(p['total'], 'conversation page'),
    limit: num(p['limit'], 'conversation page'),
    offset: num(p['offset'], 'conversation page'),
  };
}

export function parseConversationDetail(body: unknown): ConversationDetail {
  const d = obj(body, 'conversation');
  return {
    ...parseConversationItem(d),
    messages: list(d['messages'], 'conversation', 500).map(parseHistoryDetail),
  };
}

export function parseFeedback(body: unknown): FeedbackResult {
  const f = obj(body, 'feedback');
  const value = rating(f['rating']);
  if (value === null) {
    throw new ApiResponseError('feedback');
  }
  return {
    queryId: str(f['query_id'], 'feedback', 64),
    rating: value,
    categories: strings(f['categories'], 'feedback', 20),
    remembered: f['remembered'] === true,
  };
}
