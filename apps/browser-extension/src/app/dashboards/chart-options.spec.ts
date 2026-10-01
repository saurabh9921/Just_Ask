import type {
  AnalysisChart,
  ChartSpec,
  FormattedResult,
  ResultColumn,
} from '../core/messaging/analytics-types';
import { allLeads } from '../../testing/analysis-fixtures';
import { queryResponse } from '../../testing/analytics-fixtures';
import {
  buildAnalysisChartModel,
  buildChartModel,
  isBlank,
  MAX_CATEGORIES,
  MAX_PIE_SLICES,
} from './chart-options';

function result(): FormattedResult {
  return queryResponse().result!;
}

function chart(
  type: ChartSpec['type'],
  x: string | null,
  y: readonly string[],
  extra: Partial<ChartSpec> = {},
): ChartSpec {
  return {
    type,
    x,
    y,
    series: null,
    enabled: true,
    title: null,
    validation: { valid: true, reasons: [] },
    ...extra,
  };
}

function column(name: string, label: string, extra: Partial<ResultColumn> = {}): ResultColumn {
  return {
    name,
    label,
    kind: 'dimension',
    type: 'string',
    format: null,
    timeGrain: null,
    role: 'category',
    ...extra,
  };
}

function analysisChart(): AnalysisChart {
  return allLeads().analysis!.chart;
}

describe('buildChartModel', () => {
  it('builds a bar chart with an accessible description', () => {
    const model = buildChartModel(result())!;
    expect(model.option.aria.enabled).toBe(true);
    expect(model.description).toBe('Bar chart of Total sales by Region, 4 categories.');
    expect(model.option.series[0]).toMatchObject({
      type: 'bar',
      data: [1751966.79, 1613876.98, 1375207.96, 1267586.6],
    });
    expect(model.option.xAxis).toMatchObject({
      type: 'category',
      data: ['South', 'North', 'East', 'West'],
    });
  });

  it('builds line charts for trends', () => {
    const base = result();
    const model = buildChartModel({
      ...base,
      columns: [
        column('month', 'Month', {
          kind: 'time',
          type: 'date',
          format: 'date',
          timeGrain: 'month',
          role: 'time',
        }),
        base.columns[1]!,
      ],
      rows: [
        ['2026-07-01', 10],
        ['2026-08-01', 12],
      ],
      chart: chart('line', 'month', ['total_sales']),
    })!;
    expect(model.option.series[0]).toMatchObject({ type: 'line', data: [10, 12] });
  });

  it('pivots grouped bars by the series column', () => {
    const base = result();
    const model = buildChartModel({
      ...base,
      columns: [column('region', 'Region'), column('channel', 'Channel'), base.columns[1]!],
      rows: [
        ['North', 'Web', 1],
        ['North', 'Store', 2],
        ['South', 'Web', 3],
      ],
      chart: chart('grouped_bar', 'region', ['total_sales'], { series: 'channel' }),
    })!;
    expect(model.option.series.map((s) => s['name'])).toEqual(['Web', 'Store']);
    expect(model.option.series[1]?.['data']).toEqual([2, null]);
    expect(model.description).toBe('Bar chart of Total sales by Region, 2 categories.');
  });

  it('caps categories and skips non-chartable results', () => {
    const base = result();
    const many = Array.from({ length: MAX_CATEGORIES + 5 }, (_, i) => [`R${i}`, i]);
    const model = buildChartModel({ ...base, rows: many })!;
    expect(model.truncatedCategories).toBe(true);
    expect((model.option.xAxis!['data'] as unknown[]).length).toBe(MAX_CATEGORIES);
    expect(buildChartModel({ ...base, chart: chart('kpi', null, []) })).toBeNull();
    expect(buildChartModel({ ...base, rows: [] })).toBeNull();
    expect(
      buildChartModel({ ...base, chart: chart('bar', 'missing', ['total_sales']) }),
    ).toBeNull();
  });

  it('never draws a chart the server disabled or found invalid', () => {
    const base = result();
    expect(buildChartModel({ ...base, chart: { ...base.chart, enabled: false } })).toBeNull();
    expect(
      buildChartModel({
        ...base,
        chart: { ...base.chart, validation: { valid: false, reasons: ['Region is blank'] } },
      }),
    ).toBeNull();
  });

  it('never plots blank categories; fewer than two meaningful ones is no chart', () => {
    const base = result();
    const model = buildChartModel({
      ...base,
      rows: [
        [null, 14],
        ['Sales prospected', 8],
        ['  ', 3],
        ['N/A', 2],
        ['databank', 6],
      ],
    })!;
    expect(model.option.yAxis).toMatchObject({ data: ['Sales prospected', 'databank'] });
    expect(JSON.stringify(model.option)).not.toContain('blank');

    // The reported case: one row, every category NULL.
    expect(buildChartModel({ ...base, rows: [[null, 28]] })).toBeNull();
    // A single meaningful category leaves nothing to compare.
    expect(
      buildChartModel({
        ...base,
        rows: [
          ['North', 5],
          [null, 3],
        ],
      }),
    ).toBeNull();
  });

  it('never uses free text or identifiers as categories', () => {
    const base = result();
    for (const role of ['free_text', 'identifier'] as const) {
      const model = buildChartModel({
        ...base,
        columns: [{ ...base.columns[0]!, role }, base.columns[1]!],
      });
      expect(model).toBeNull();
    }
  });

  it('draws shares as a pie, and as bars when there are too many slices', () => {
    const base = result();
    const pie = buildChartModel({ ...base, chart: chart('pie', 'region', ['total_sales']) })!;
    expect(pie.option.series[0]).toMatchObject({ type: 'pie' });
    expect(pie.option.series[0]?.['data']).toEqual([
      { name: 'South', value: 1751966.79 },
      { name: 'North', value: 1613876.98 },
      { name: 'East', value: 1375207.96 },
      { name: 'West', value: 1267586.6 },
    ]);
    expect(pie.option.xAxis).toBeUndefined();
    expect(pie.description).toBe('Pie chart of Total sales by Region, 4 slices.');

    const many = Array.from({ length: MAX_PIE_SLICES + 1 }, (_, i) => [`R${i}`, i + 1]);
    const bars = buildChartModel({
      ...base,
      rows: many,
      chart: chart('pie', 'region', ['total_sales']),
    })!;
    expect(bars.option.series[0]).toMatchObject({ type: 'bar' });
  });

  it('plots two measures against each other as a scatter chart', () => {
    const base = result();
    const margin: ResultColumn = { ...base.columns[1]!, name: 'margin', label: 'Margin' };
    const model = buildChartModel({
      ...base,
      columns: [base.columns[0]!, base.columns[1]!, margin],
      rows: [
        ['North', 100, 12],
        ['South', 80, 20],
        ['East', null, 5],
      ],
      chart: chart('scatter', 'region', ['total_sales', 'margin']),
    })!;
    expect(model.option.series[0]).toMatchObject({
      type: 'scatter',
      data: [
        { name: 'North', value: [100, 12] },
        { name: 'South', value: [80, 20] },
      ],
    });
    expect(model.option.xAxis).toMatchObject({ type: 'value', name: 'Total sales' });
    expect(model.option.yAxis).toMatchObject({ type: 'value', name: 'Margin' });
  });

  it('carries the server title', () => {
    const base = result();
    const model = buildChartModel({ ...base, chart: { ...base.chart, title: 'Sales by region' } });
    expect(model!.title).toBe('Sales by region');
  });
});

describe('buildAnalysisChartModel', () => {
  it("draws an analysis's validated points", () => {
    const model = buildAnalysisChartModel(analysisChart())!;
    expect(model.title).toBe('Leads by stage');
    expect(model.description).toBe('Bar chart of Number of leads by stage, 3 categories.');
    expect(model.option.series[0]).toMatchObject({ type: 'bar', data: [14, 8, 6] });
  });

  it('respects enabled and validation, and never plots blank labels', () => {
    const valid = analysisChart();
    expect(buildAnalysisChartModel({ ...valid, enabled: false })).toBeNull();
    expect(
      buildAnalysisChartModel({ ...valid, validation: { valid: false, reasons: ['x'] } }),
    ).toBeNull();
    expect(buildAnalysisChartModel({ ...valid, type: null })).toBeNull();
    expect(
      buildAnalysisChartModel({
        ...valid,
        data: [
          { label: '(blank)', value: 20 },
          { label: 'New', value: 8 },
        ],
      }),
    ).toBeNull();
  });

  it('draws a pie for shares', () => {
    const model = buildAnalysisChartModel({ ...analysisChart(), type: 'pie' })!;
    expect(model.option.series[0]).toMatchObject({ type: 'pie' });
  });
});

describe('isBlank', () => {
  it('treats NULL and placeholder text as missing', () => {
    for (const value of [null, '', '  ', 'NULL', 'n/a', 'N/A', 'none', '-', '(blank)']) {
      expect(isBlank(value)).toBe(true);
    }
    for (const value of ['New', 0, false, 'Nanaimo']) {
      expect(isBlank(value)).toBe(false);
    }
  });
});
