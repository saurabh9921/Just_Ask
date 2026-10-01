/** Shared test data: an analytics API response (snake_case JSON) and its parsed form. */
import type { QueryResponse } from '../app/core/messaging/analytics-types';
import { parseQueryResponse } from '../background/analytics-parsers';

export const QUERY_ID = '5f0c2b4e-3d57-4b0e-9a3c-8f3a1c2d4e5f';
export const CONNECTION_ID = '0b8f7d8e-1c2d-4e5f-8a9b-0c1d2e3f4a5b';

export function apiResponse(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    query_id: QUERY_ID,
    status: 'succeeded',
    question: 'Show total sales by region for the last six months',
    intent: 'aggregation',
    connection: { id: CONNECTION_ID, name: 'ERP prod', connector_type: 'postgresql' },
    semantic_version: 3,
    data_freshness: '2026-09-23T06:59:23+00:00',
    plan: { entity: 'SalesOrder', measures: ['total_sales'], dimensions: ['region'] },
    description: 'total sales (sum of net_amount) by region from SalesOrder',
    assumptions: ['Region names come from the sales order.'],
    source_entities: ['SalesOrder'],
    applied_filters: {
      date_window: {
        field: 'order_date',
        start: '2026-03-16',
        end_exclusive: '2026-09-16',
        label: '2026-03-16 to 2026-09-15',
      },
      previous_window: null,
      filters: [{ field: 'region', op: 'in', value: ['North', 'South'] }],
    },
    security_scope: { tenant: 'acme', row_filter_fields: ['tenant_code'], read_only: true },
    validation: { plan_valid: true, sql_validated: true, corrections: 0, confidence: 0.9 },
    sql: 'SELECT t0.region_name AS region, sum(t0.net_amount) AS total_sales FROM analytics_sales_orders AS t0 WHERE t0.tenant_code = ? LIMIT ?',
    previous_sql: null,
    cost: { source: 'explain', estimated_cost: 12.5, estimated_rows: 4 },
    result: {
      columns: [
        { name: 'region', label: 'Region', kind: 'dimension', type: 'string', format: null },
        {
          name: 'total_sales',
          label: 'Total sales',
          kind: 'measure',
          type: 'decimal',
          format: 'currency',
        },
      ],
      rows: [
        ['South', 1751966.79],
        ['North', 1613876.98],
        ['East', 1375207.96],
        ['West', 1267586.6],
      ],
      row_count: 4,
      truncated: false,
      kpis: [
        {
          name: 'total_sales',
          label: 'Total sales',
          value: 6008638.33,
          format: 'currency',
          previous: null,
          change: null,
          change_pct: null,
          scope: 'total',
        },
      ],
      chart: { type: 'bar', x: 'region', y: ['total_sales'], series: null },
      redacted_columns: [],
    },
    rows_returned: 4,
    explanation: {
      summary: '4 regions; South is highest.',
      highlights: ['Highest: South (1,751,966.79).'],
      source: 'llm',
    },
    clarification_question: null,
    refusal_reason: null,
    error: null,
    timings: { total_ms: 412.3, execution_ms: 18.2, llm_ms: 250.1 },
    steps: [{ step: 'RECEIVE_QUESTION', status: 'completed', duration_ms: 0.4 }],
    replayed: false,
    cache: { hit: false, age_seconds: null },
    ...overrides,
  };
}

export function queryResponse(overrides: Record<string, unknown> = {}): QueryResponse {
  return parseQueryResponse(apiResponse(overrides));
}
