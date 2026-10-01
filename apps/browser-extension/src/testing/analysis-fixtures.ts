/**
 * Business-analysis responses (snake_case JSON, as the API sends them), modelled on the
 * live "invento" leads data: `leads` → `companies`, no value/amount field, priority
 * never recorded, source missing for half of the leads.
 */
import type { QueryResponse } from '../app/core/messaging/analytics-types';
import { queryResponse } from './analytics-fixtures';

const NO_PERIOD = { date_window: null, previous_window: null, filters: [] };

function metric(key: string, label: string, value: number | null, query: string) {
  return {
    key,
    label,
    value,
    format: 'integer',
    source: 'query_result',
    query,
    validated: true,
    detail: null,
  };
}

function totalResult(value: number) {
  return {
    columns: [
      {
        name: 'lead_count',
        label: 'Lead count',
        kind: 'measure',
        type: 'integer',
        format: 'integer',
        role: null,
      },
    ],
    rows: [[value]],
    row_count: 1,
    truncated: false,
    kpis: [
      {
        name: 'lead_count',
        label: 'Lead count',
        value,
        format: 'integer',
        previous: null,
        change: null,
        change_pct: null,
        scope: 'value',
      },
    ],
    chart: {
      type: 'kpi',
      x: null,
      y: ['lead_count'],
      series: null,
      enabled: true,
      title: null,
      validation: { valid: true, reasons: [] },
    },
    redacted_columns: [],
    quality: [],
    warnings: [],
  };
}

/** "give me lead analysis for pending metals co": the company exists and has no leads. */
export function companyWithoutLeads(): QueryResponse {
  return queryResponse({
    question: 'give me lead analysis for pending metals co',
    intent: 'analysis',
    applied_filters: { ...NO_PERIOD, filters: [{ field: 'companies_id', op: 'in', value: [2] }] },
    result: totalResult(0),
    explanation: {
      summary: 'Lead Analysis — Pending Metals Co: Pending Metals Co has no leads.',
      highlights: [],
      source: 'system',
    },
    analysis: {
      intent: 'lead_analysis',
      title: 'Lead Analysis — Pending Metals Co',
      entity: {
        type: 'company',
        mention: 'pending metals co',
        name: 'Pending Metals Co',
        resolved: true,
        match: 'exact',
        keys: [2],
        candidates: [],
        filter_field: 'companies_id',
      },
      metrics: [metric('total', 'Total leads', 0, 'total')],
      unavailable: [],
      dimensions: [],
      insights: [{ text: 'Pending Metals Co has no leads.', metrics: ['total'], kind: 'fact' }],
      warnings: [],
      chart: {
        enabled: false,
        type: null,
        title: null,
        dimension: null,
        metric: null,
        data: [],
        validation: { valid: false, reasons: ['no records to chart'] },
      },
      validation: {
        data_validated: true,
        entity_resolved: true,
        entity_filter_applied: true,
        chart_validated: false,
        unsupported_claims: [],
        warnings: [],
      },
      queries: [
        {
          label: 'total',
          sql: 'SELECT count(DISTINCT t0.id) AS lead_count FROM leads AS t0 WHERE t0.companies_id IN (?)',
          row_count: 1,
          validated: true,
        },
      ],
    },
  });
}

/** "lead analysis": every lead, with a validated breakdown to chart. */
export function allLeads(chart: Record<string, unknown> = {}): QueryResponse {
  return queryResponse({
    question: 'lead analysis',
    intent: 'analysis',
    applied_filters: NO_PERIOD,
    result: totalResult(28),
    analysis: {
      intent: 'lead_analysis',
      title: 'Lead Analysis',
      entity: null,
      metrics: [
        metric('total', 'Total leads', 28, 'total'),
        metric('leads_by_stage', 'Leads by stage', 3, 'leads by stage'),
        metric(
          'recent',
          'Leads created in the last 30 days',
          4,
          'leads created in the last 30 days',
        ),
      ],
      unavailable: [
        {
          key: 'total_value',
          label: 'Total lead value',
          reason: 'The leads data has no value or amount field.',
        },
        {
          key: 'average_value',
          label: 'Average lead value',
          reason: 'The leads data has no value or amount field.',
        },
        {
          key: 'leads_by_priority',
          label: 'Leads by priority',
          reason: 'Priority is not recorded for any of the 28 leads.',
        },
      ],
      dimensions: [
        {
          name: 'priority',
          label: 'Priority',
          role: 'category',
          total: 28,
          null_count: 28,
          blank_count: 0,
          distinct_values: 0,
          meaningful_ratio: 0,
          weighted: true,
          usable: false,
          reason: 'only blank values',
        },
      ],
      insights: [
        { text: '28 leads in total.', metrics: ['total'], kind: 'fact' },
        {
          text: '50% of leads are “New” (14 of 28); then “Qualified” 8, “Won” 6.',
          metrics: ['leads_by_stage'],
          kind: 'fact',
        },
        { text: '4 leads created in the last 30 days.', metrics: ['recent'], kind: 'fact' },
      ],
      warnings: ['Priority is not recorded for any of the 28 leads.'],
      chart: {
        enabled: true,
        type: 'bar',
        title: 'Leads by stage',
        dimension: 'stage',
        metric: 'Number of leads',
        data: [
          { label: 'New', value: 14 },
          { label: 'Qualified', value: 8 },
          { label: 'Won', value: 6 },
        ],
        validation: { valid: true, reasons: [] },
        ...chart,
      },
      validation: {
        data_validated: true,
        entity_resolved: null,
        entity_filter_applied: null,
        chart_validated: true,
        unsupported_claims: [],
        warnings: ['Priority is not recorded for any of the 28 leads.'],
      },
      queries: [
        {
          label: 'total',
          sql: 'SELECT count(DISTINCT t0.id) AS lead_count FROM leads AS t0',
          row_count: 1,
          validated: true,
        },
        {
          label: 'leads by stage',
          sql: 'SELECT t0.stage AS stage, count(DISTINCT t0.id) AS lead_count FROM leads AS t0 GROUP BY t0.stage',
          row_count: 3,
          validated: true,
        },
        {
          label: 'leads created in the last 30 days',
          sql: 'SELECT count(DISTINCT t0.id) AS lead_count FROM leads AS t0 WHERE t0.created_at >= ?',
          row_count: 1,
          validated: true,
        },
      ],
    },
  });
}

/**
 * The reported answer, as the planner built it before this change: lead candidates
 * "by analysis note", a field that is NULL for all 28 records. The server now rejects
 * the chart; the response must never be shown as a "(blank)" category.
 */
export function blankBreakdown(): QueryResponse {
  const dim = (name: string, label: string) => ({
    name,
    label,
    kind: 'dimension',
    type: 'string',
    format: null,
    role: 'free_text',
  });
  const quality = (name: string, label: string) => ({
    name,
    label,
    role: 'free_text',
    total: 28,
    null_count: 28,
    blank_count: 0,
    distinct_values: 0,
    meaningful_ratio: 0,
    weighted: true,
    usable: false,
    reason: 'free text, not a business category',
  });
  const warnings = [
    'Analysis note is not recorded for any of the 28 lead candidates.',
    'Analysis note is free text, so it is not used as a chart category.',
    'Analysis source is not recorded for any of the 28 lead candidates.',
  ];
  return queryResponse({
    question: 'show lead candidates by analysis note',
    applied_filters: NO_PERIOD,
    result: {
      columns: [
        dim('analysis_note', 'Analysis note'),
        dim('analysis_source', 'Analysis source'),
        {
          name: 'record_count',
          label: 'Record count',
          kind: 'measure',
          type: 'integer',
          format: 'integer',
          role: null,
        },
      ],
      rows: [[null, null, 28]],
      row_count: 1,
      truncated: false,
      kpis: [
        {
          name: 'record_count',
          label: 'Record count',
          value: 28,
          format: 'integer',
          previous: null,
          change: null,
          change_pct: null,
          scope: 'total',
        },
      ],
      chart: {
        type: 'kpi',
        x: 'analysis_note',
        y: ['record_count'],
        series: 'analysis_source',
        enabled: false,
        title: 'Record count by analysis note',
        validation: {
          valid: false,
          reasons: [
            'Analysis note is free text, not a business category',
            'Analysis source is free text, not a business category',
          ],
        },
      },
      redacted_columns: [],
      quality: [
        quality('analysis_note', 'Analysis note'),
        quality('analysis_source', 'Analysis source'),
      ],
      warnings,
    },
    explanation: {
      summary: '(blank) – (blank): record count is 28.',
      highlights: [],
      source: 'llm',
    },
    analysis: {
      intent: 'aggregation',
      title: null,
      entity: null,
      metrics: [metric('record_count', 'Record count', 28, 'main')],
      unavailable: [],
      dimensions: [],
      insights: [],
      warnings,
      chart: {
        enabled: false,
        type: null,
        title: 'Record count by analysis note',
        dimension: 'Analysis note',
        metric: 'Record count',
        data: [],
        validation: {
          valid: false,
          reasons: ['Analysis note is free text, not a business category'],
        },
      },
      validation: {
        data_validated: false,
        entity_resolved: null,
        entity_filter_applied: null,
        chart_validated: false,
        unsupported_claims: [],
        warnings,
      },
      queries: [],
    },
  });
}
