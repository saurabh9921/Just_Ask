import type { QueryResponse, ValueFormat } from '../../core/messaging/analytics-types';
import { formatDate, formatValue } from '../../shared/format/value-format';
import { allLeads, blankBreakdown, companyWithoutLeads } from '../../../testing/analysis-fixtures';
import { queryResponse } from '../../../testing/analytics-fixtures';
import { summarizeAnswer } from './answer-summary';

/** Expected numbers are built with the same formatter, so tests pass in any locale. */
const c = (v: number, format: ValueFormat | null = 'currency') =>
  formatValue(v, format, { compact: true });
const exact = (v: number, format: ValueFormat | null = 'currency') => formatValue(v, format);
const month = (y: number, m: number) =>
  new Date(y, m - 1, 1).toLocaleDateString(undefined, { month: 'short', year: 'numeric' });

const SALES = {
  name: 'total_sales',
  label: 'Total sales',
  kind: 'measure',
  type: 'decimal',
  format: 'currency',
};
const REGION = { name: 'region', label: 'Region', kind: 'dimension', type: 'string', format: null };
const NO_PERIOD = { date_window: null, previous_window: null, filters: [] };

function totalKpi(value: number) {
  return {
    name: 'total_sales',
    label: 'Total sales',
    value,
    format: 'currency',
    previous: null,
    change: null,
    change_pct: null,
    scope: 'total',
  };
}

function withResult(result: Record<string, unknown>, extra: Record<string, unknown> = {}) {
  return queryResponse({
    result: { truncated: false, kpis: [], redacted_columns: [], ...result },
    applied_filters: NO_PERIOD,
    ...extra,
  });
}

function summary(response: QueryResponse): string {
  return summarizeAnswer(response)!.text;
}

describe('summarizeAnswer', () => {
  it('ranks categories with their share of the total and the period covered', () => {
    const result = summarizeAnswer(queryResponse())!;
    expect(result.text).toBe(
      `South has the highest total sales at ${c(1751966.79)} (29% of the total). ` +
        `North is next at ${c(1613876.98)}, and West is the lowest at ${c(1267586.6)}. ` +
        `Together they total ${c(6008638.33)}.`,
    );
    expect(result.period).toBe(`${formatDate('2026-03-16')} – ${formatDate('2026-09-15')}`);
  });

  it('never adds up a measure that has no server total (averages, truncated results)', () => {
    const text = summary(
      withResult({
        columns: [REGION, { ...SALES, name: 'avg_price', label: 'Average price' }],
        rows: [
          ['North', 120],
          ['South', 80],
          ['East', 100],
        ],
        row_count: 3,
        chart: { type: 'bar', x: 'region', y: ['avg_price'], series: null },
      }),
    );
    expect(text).toBe(
      `North has the highest average price at ${c(120)}. ` +
        `East is next at ${c(100)}, and South is the lowest at ${c(80)}.`,
    );
    expect(text).not.toContain('total');
  });

  it('states single figures with their exact value', () => {
    const kpi = (name: string, label: string, value: number, format: string | null) => ({
      name,
      label,
      value,
      format,
      previous: null,
      change: null,
      change_pct: null,
      scope: 'value',
    });
    const text = summary(
      withResult({
        columns: [
          { name: 'total', label: 'Total', kind: 'measure', type: 'decimal', format: 'currency' },
          { name: 'total_inr', label: 'Total inr', kind: 'measure', type: 'decimal' },
        ],
        rows: [[116802593676.63, 9753016572752.72]],
        row_count: 1,
        kpis: [
          kpi('total', 'Total', 116802593676.63, 'currency'),
          kpi('total_inr', 'Total inr', 9753016572752.72, null),
        ],
        chart: { type: 'kpi', x: null, y: ['total', 'total_inr'], series: null },
      }),
    );
    expect(text).toBe(
      `Total is ${c(116802593676.63)} (${exact(116802593676.63)}) and total inr is ` +
        `${c(9753016572752.72, null)} (${exact(9753016572752.72, null)}).`,
    );
  });

  it('explains the change against the previous period', () => {
    const text = summary(
      withResult({
        columns: [SALES],
        rows: [[1200000]],
        row_count: 1,
        kpis: [
          {
            name: 'total_sales',
            label: 'Total sales',
            value: 1200000,
            format: 'currency',
            previous: 1140000,
            change: 60000,
            change_pct: 5.26,
            scope: 'value',
          },
        ],
        chart: { type: 'kpi', x: null, y: ['total_sales'], series: null },
      }),
    );
    expect(text).toBe(
      `Total sales is ${c(1200000)} (${exact(1200000)}), up 5.3% from ${c(1140000)} ` +
        'in the previous period.',
    );
  });

  it('describes a trend over time in months, with its peak', () => {
    const text = summary(
      withResult({
        columns: [
          {
            name: 'order_month',
            label: 'Order month',
            kind: 'time',
            type: 'date',
            format: 'date',
            time_grain: 'month',
          },
          SALES,
        ],
        rows: [
          ['2026-03-01', 610000],
          ['2026-01-01', 400000],
          ['2026-04-01', 500000],
          ['2026-02-01', 450000],
        ],
        row_count: 4,
        kpis: [totalKpi(1960000)],
        chart: { type: 'line', x: 'order_month', y: ['total_sales'], series: null },
      }),
    );
    expect(text).toBe(
      `Total sales went from ${c(400000)} in ${month(2026, 1)} to ${c(500000)} in ` +
        `${month(2026, 4)} (up 25%). The peak was ${month(2026, 3)} at ${c(610000)}. ` +
        `In total: ${c(1960000)}.`,
    );
  });

  it('ranks the lines of a trend by their totals', () => {
    const text = summary(
      withResult({
        columns: [
          { name: 'q', label: 'Quarter', kind: 'time', type: 'date', time_grain: 'quarter' },
          REGION,
          SALES,
        ],
        rows: [
          ['2026-01-01', 'North', 100],
          ['2026-04-01', 'North', 200],
          ['2026-01-01', 'South', 50],
          ['2026-04-01', 'South', 50],
        ],
        row_count: 4,
        kpis: [totalKpi(400)],
        chart: { type: 'line', x: 'q', y: ['total_sales'], series: 'region' },
      }),
    );
    expect(text).toBe(
      `North contributed the most total sales (${c(300)}, 75% of the total). ` +
        `South contributed the least (${c(100)}). In total: ${c(400)}.`,
    );
  });

  it('summarises a comparison: overall change, biggest rise and fall', () => {
    const text = summary(
      withResult({
        columns: [
          REGION,
          SALES,
          {
            ...SALES,
            name: 'total_sales_previous',
            label: 'Total sales (previous)',
            kind: 'previous',
          },
          { ...SALES, name: 'total_sales_change', label: 'Total sales change', kind: 'change' },
          {
            name: 'total_sales_change_pct',
            label: 'Total sales change %',
            kind: 'change_pct',
            type: 'float',
            format: 'percent',
          },
        ],
        rows: [
          ['North', 1125, 1000, 125, 12.5],
          ['West', 968, 1000, -32, -3.2],
          ['East', 1000, 1000, 0, 0],
        ],
        row_count: 3,
        kpis: [totalKpi(3093)],
        chart: {
          type: 'grouped_bar',
          x: 'region',
          y: ['total_sales', 'total_sales_previous'],
          series: null,
        },
      }),
    );
    expect(text).toBe(
      `Overall, total sales is ${c(3093)}, up 3.1% from ${c(3000)} in the previous period. ` +
        'North grew the most, up 12.5%. West fell the most, down 3.2%.',
    );
  });

  it('counts the rows of a list without figures', () => {
    const text = summary(
      withResult({
        columns: [
          { name: 'company', label: 'Company', kind: 'dimension', type: 'string' },
          { name: 'city', label: 'City', kind: 'dimension', type: 'string' },
        ],
        rows: [
          ['Acme Traders', 'Pune'],
          ['Sun Exports', 'Mumbai'],
        ],
        row_count: 2,
        chart: { type: 'table', x: null, y: [], series: null },
      }),
    );
    expect(text).toBe('2 companies found.');
  });

  it('says when nothing matched, and when only part of the rows is shown', () => {
    expect(
      summary(
        withResult({
          columns: [REGION, SALES],
          rows: [],
          row_count: 0,
          chart: { type: 'table', x: null, y: [], series: null },
        }),
      ),
    ).toBe('No data matched this question.');

    const truncated = summary(
      withResult({
        columns: [REGION, SALES],
        rows: [
          ['North', 2],
          ['South', 1],
        ],
        row_count: 2,
        truncated: true,
        chart: { type: 'bar', x: 'region', y: ['total_sales'], series: null },
      }),
    );
    expect(truncated).toContain('Only the first 2 rows are shown.');
    expect(truncated).not.toContain('Together');
  });

  it('falls back to the model explanation (without repeats) for shapes it cannot describe', () => {
    const text = summary(
      withResult(
        {
          columns: [SALES],
          rows: [[5]],
          row_count: 1,
          kpis: [],
          chart: { type: 'kpi', x: null, y: ['total_sales'], series: null },
        },
        {
          explanation: {
            summary: 'Sales were 5.',
            highlights: ['Sales were 5', 'Small sample.'],
            source: 'llm',
          },
        },
      ),
    );
    expect(text).toBe('Sales were 5. Small sample.');
  });

  it('has nothing to summarise for "Plan only" answers', () => {
    expect(summarizeAnswer(queryResponse({ status: 'planned', result: null }))).toBeNull();
  });

  it('never states a blank category: the reported "(blank) – (blank)" answer', () => {
    const result = summarizeAnswer(blankBreakdown())!;
    expect(result.text).toBe('Record count is 28.');
    expect(result.notes).toEqual([
      'Analysis note is not recorded for any of the 28 lead candidates.',
      'Analysis note is free text, so it is not used as a chart category.',
      'Analysis source is not recorded for any of the 28 lead candidates.',
    ]);
    expect(JSON.stringify(result)).not.toContain('(blank)');
  });

  it('ranks only recorded values and reports the missing ones apart', () => {
    const SOURCE = { ...REGION, name: 'source', label: 'Source', role: 'category' };
    const COUNT = {
      name: 'record_count',
      label: 'Record count',
      kind: 'measure',
      type: 'integer',
      format: 'integer',
    };
    const result = summarizeAnswer(
      withResult({
        columns: [SOURCE, COUNT],
        rows: [
          [null, 14],
          ['Sales prospected', 8],
          ['databank', 6],
        ],
        row_count: 3,
        kpis: [{ ...totalKpi(28), name: 'record_count', label: 'Record count', format: 'integer' }],
        chart: { type: 'bar', x: 'source', y: ['record_count'], series: null },
        warnings: ['Source is missing for 14 of 28 leads.'],
      }),
    )!;
    expect(result.text).toBe(
      'Sales prospected has the highest record count at 8 (29% of the total). databank has 6. ' +
        'In total: 28, including 14 with no source.',
    );
    expect(result.notes).toEqual(['Source is missing for 14 of 28 leads.']);
  });

  it('says a field is not recorded instead of ranking blanks', () => {
    const text = summary(
      withResult({
        columns: [REGION, SALES],
        rows: [
          [null, 5],
          ['', 3],
        ],
        row_count: 2,
        chart: { type: 'bar', x: 'region', y: ['total_sales'], series: null },
      }),
    );
    expect(text).toBe('Region is not recorded for these rows.');
  });

  it("states a business analysis with its title, validated points and what's missing", () => {
    const result = summarizeAnswer(allLeads())!;
    expect(result.title).toBe('Lead Analysis');
    expect(result.points).toEqual([
      '28 leads in total.',
      '50% of leads are “New” (14 of 28); then “Qualified” 8, “Won” 6.',
      '4 leads created in the last 30 days.',
    ]);
    expect(result.notes).toEqual([
      'Total lead value and average lead value cannot be calculated: the leads data has no ' +
        'value or amount field.',
      // Stated once: the warning repeats why the priority breakdown is unavailable.
      'Leads by priority cannot be calculated: priority is not recorded for any of the 28 leads.',
    ]);
  });

  it('leaves restating a data gap to the notes', () => {
    const response = allLeads();
    const analysis = response.analysis!;
    const gap = {
      text: 'Key observation: total lead value, leads by priority cannot be tracked.',
      metrics: ['total_value', 'leads_by_priority'],
      kind: 'observation' as const,
    };
    const result = summarizeAnswer({
      ...response,
      analysis: { ...analysis, insights: [...analysis.insights, gap] },
    })!;
    expect(result.points).not.toContain(gap.text);
    expect(result.points).toHaveLength(3);
  });

  it('answers a company with no leads plainly', () => {
    const result = summarizeAnswer(companyWithoutLeads())!;
    expect(result.title).toBe('Lead Analysis — Pending Metals Co');
    expect(result.points).toEqual(['Pending Metals Co has no leads.']);
    expect(result.notes).toEqual([]);
  });
});
