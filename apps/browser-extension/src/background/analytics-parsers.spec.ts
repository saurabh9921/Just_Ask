import { allLeads } from '../testing/analysis-fixtures';
import { apiResponse, QUERY_ID } from '../testing/analytics-fixtures';
import {
  parseFeedback,
  parseHistoryDetail,
  parseHistoryPage,
  parseProgressEvent,
  parseQueryResponse,
} from './analytics-parsers';
import { ApiResponseError } from './api-client';

describe('analytics parsers', () => {
  it('parses the validated analysis, chart validation and data quality', () => {
    const r = allLeads();
    const analysis = r.analysis!;
    expect(analysis.title).toBe('Lead Analysis');
    expect(analysis.metrics[0]).toEqual({
      key: 'total',
      label: 'Total leads',
      value: 28,
      format: 'integer',
      source: 'query_result',
      query: 'total',
      validated: true,
      detail: null,
    });
    expect(analysis.unavailable[0]?.key).toBe('total_value');
    expect(analysis.dimensions[0]).toMatchObject({
      name: 'priority',
      nullCount: 28,
      usable: false,
    });
    expect(analysis.chart.validation).toEqual({ valid: true, reasons: [] });
    expect(analysis.validation.entityResolved).toBeNull();
    expect(analysis.queries[1]?.rowCount).toBe(3);
    expect(r.result!.chart.enabled).toBe(true);
  });

  it('reads older responses: no analysis, charts allowed', () => {
    const r = parseQueryResponse(apiResponse());
    expect(r.analysis).toBeNull();
    expect(r.result!.chart).toMatchObject({
      enabled: true,
      title: null,
      validation: { valid: true, reasons: [] },
    });
    expect(r.result!.quality).toEqual([]);
    expect(r.result!.columns[0]!.role).toBeNull();
  });

  it('rejects malformed analysis blocks', () => {
    const bad = (analysis: unknown) => () => parseQueryResponse(apiResponse({ analysis }));
    expect(bad({ intent: 'x' })).toThrow(ApiResponseError); // no validation block
    const base = {
      intent: 'lead_analysis',
      validation: { data_validated: true, chart_validated: false },
      chart: { enabled: false },
    };
    expect(bad(base)).not.toThrow();
    const metric = { key: 'k', label: 'l', value: 3, source: 'query_result', query: 'q' };
    expect(bad({ ...base, metrics: [{ ...metric, validated: true }] })).not.toThrow();
    // A figure must be a number, and must come from a query result.
    expect(bad({ ...base, metrics: [{ ...metric, validated: true, value: 'many' }] })).toThrow(
      ApiResponseError,
    );
    expect(bad({ ...base, metrics: [{ ...metric, validated: true, source: 'llm' }] })).toThrow(
      ApiResponseError,
    );
    expect(bad({ ...base, chart: { enabled: true, type: 'radar' } })).toThrow(ApiResponseError);
  });

  it('converts a query response to camelCase with validated shapes', () => {
    const r = parseQueryResponse(apiResponse());
    expect(r.queryId).toBe(QUERY_ID);
    expect(r.connection).toEqual({
      id: expect.any(String) as string,
      name: 'ERP prod',
      connectorType: 'postgresql',
    });
    expect(r.appliedFilters.dateWindow?.endExclusive).toBe('2026-09-16');
    expect(r.securityScope.rowFilterFields).toEqual(['tenant_code']);
    expect(r.result?.columns[1]?.format).toBe('currency');
    expect(r.result?.rows[0]).toEqual(['South', 1751966.79]);
    expect(r.result?.kpis[0]?.scope).toBe('total');
    expect(r.timings.totalMs).toBe(412.3);
    expect(r.steps[0]).toEqual({ step: 'RECEIVE_QUESTION', status: 'completed', durationMs: 0.4 });
  });

  it('accepts clarification/refusal responses without a result', () => {
    const r = parseQueryResponse(
      apiResponse({
        status: 'refused',
        result: null,
        explanation: null,
        refusal_reason: 'FORBIDDEN_FIELD',
      }),
    );
    expect(r.result).toBeNull();
    expect(r.refusalReason).toBe('FORBIDDEN_FIELD');
  });

  it.each([
    ['unknown status', { status: 'deleted' }],
    ['object cells', { result: { ...(apiResponse()['result'] as object), rows: [[{}, 1]] } }],
    ['ragged rows', { result: { ...(apiResponse()['result'] as object), rows: [['x']] } }],
    ['missing security scope', { security_scope: null }],
    ['oversized question', { question: 'x'.repeat(5000) }],
  ])('rejects %s', (_label, overrides) => {
    expect(() => parseQueryResponse(apiResponse(overrides))).toThrow(ApiResponseError);
  });

  it('parses progress events, history and feedback', () => {
    expect(
      parseProgressEvent({
        query_id: QUERY_ID,
        step: 'BUILD_QUERY_PLAN',
        status: 'started',
        message: 'Planning the query',
        elapsed_ms: 12,
      }),
    ).toEqual({
      queryId: QUERY_ID,
      step: 'BUILD_QUERY_PLAN',
      status: 'started',
      message: 'Planning the query',
      elapsedMs: 12,
    });
    const item = {
      query_id: QUERY_ID,
      question: 'q',
      status: 'succeeded',
      intent: null,
      connection_id: null,
      connection_name: 'ERP prod',
      row_count: 4,
      total_ms: 12,
      error_code: null,
      saved: true,
      feedback_rating: 1,
      created_at: '2026-09-23T08:00:00Z',
      completed_at: null,
    };
    const page = parseHistoryPage({ items: [item], total: 1, limit: 20, offset: 0 });
    expect(page.items[0]?.feedbackRating).toBe(1);
    const detail = parseHistoryDetail({
      ...item,
      response: apiResponse(),
      response_expired: false,
    });
    expect(detail.response?.rowsReturned).toBe(4);
    expect(
      parseFeedback({ query_id: QUERY_ID, rating: -1, categories: ['slow'], remembered: false }),
    ).toEqual({ queryId: QUERY_ID, rating: -1, categories: ['slow'], remembered: false });
    expect(() => parseFeedback({ query_id: QUERY_ID, rating: 5 })).toThrow(ApiResponseError);
  });
});
