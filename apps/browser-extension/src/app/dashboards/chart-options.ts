import type {
  AnalysisChart,
  FormattedResult,
  ResultColumn,
  Scalar,
  ValueFormat,
} from '../core/messaging/analytics-types';
import { formatValue } from '../shared/format/value-format';

/** Categories beyond this are left to the table (a chart with hundreds of bars is noise). */
export const MAX_CATEGORIES = 40;
/** More slices than this make a pie unreadable: a bar chart is drawn instead. */
export const MAX_PIE_SLICES = 6;

/** A deliberately small, typed subset of the ECharts option model. */
export interface ChartOption {
  readonly aria: { readonly enabled: true; readonly label: { readonly description: string } };
  readonly color: readonly string[];
  readonly grid?: Record<string, number | string | boolean>;
  readonly tooltip: Record<string, unknown>;
  readonly legend?: Record<string, unknown>;
  readonly xAxis?: Record<string, unknown>;
  readonly yAxis?: Record<string, unknown>;
  readonly series: readonly Record<string, unknown>[];
}

export interface ChartModel {
  readonly option: ChartOption;
  readonly description: string;
  readonly truncatedCategories: boolean;
  /** The server's title for the chart ("Leads by status"), when it sent one. */
  readonly title: string | null;
}

const PALETTE = ['#2f6fd6', '#16a3a3', '#e08a1e', '#8a5cd1', '#d4466b', '#4f9a4a'];

/** Placeholders that mean "not recorded" (mirrors the API's data-quality check). */
const BLANK_MARKERS = new Set([
  '',
  'null',
  'n/a',
  'na',
  'none',
  'nil',
  '-',
  '--',
  'undefined',
  '(blank)',
]);

/** Missing data is reported in words, never plotted as a "(blank)" category. */
export function isBlank(value: Scalar | undefined): boolean {
  if (value === null || value === undefined) {
    return true;
  }
  return typeof value === 'string' && BLANK_MARKERS.has(value.trim().toLowerCase());
}

function label(value: Scalar, column: ResultColumn | undefined): string {
  return column === undefined ? String(value ?? '') : formatValue(value, column.format);
}

function numeric(value: Scalar | undefined): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

interface Series {
  readonly name: string;
  readonly data: readonly (number | null)[];
}

interface Slice {
  readonly name: string;
  readonly value: number;
}

/**
 * Builds a chart from the API's chart hint. Returns `null` - and the UI shows KPIs or
 * the table - when the server did not enable the chart (`enabled`/`validation`), for
 * `kpi`/`table` results, and when the client's own check fails: free text and
 * identifiers are never categories, blank categories are never plotted, and fewer
 * than two meaningful categories leave nothing to compare. Pure: tested without a DOM.
 */
export function buildChartModel(result: FormattedResult, palette = PALETTE): ChartModel | null {
  const spec = result.chart;
  if (!spec.enabled || !spec.validation.valid) {
    return null;
  }
  if (spec.type === 'kpi' || spec.type === 'table' || spec.x === null || spec.y.length === 0) {
    return null;
  }
  const index = new Map(result.columns.map((c, i) => [c.name, i]));
  const xIndex = index.get(spec.x);
  const yIndexes = spec.y
    .map((name) => index.get(name))
    .filter((i): i is number => i !== undefined);
  const xColumn = xIndex === undefined ? undefined : result.columns[xIndex];
  if (xIndex === undefined || xColumn === undefined || yIndexes.length !== spec.y.length) {
    return null;
  }
  if (xColumn.role === 'free_text' || xColumn.role === 'identifier') {
    return null;
  }
  const sIndex = spec.series === null ? undefined : index.get(spec.series);
  const rows = result.rows.filter(
    (row) => !isBlank(row[xIndex]) && (sIndex === undefined || !isBlank(row[sIndex])),
  );
  if (new Set(rows.map((row) => label(row[xIndex] ?? null, xColumn))).size < 2) {
    return null;
  }
  const yColumns = yIndexes
    .map((i) => result.columns[i])
    .filter((c): c is ResultColumn => c !== undefined);
  const valueFormat = yColumns[0]?.format ?? null;
  const firstY = yIndexes[0] ?? 0;
  const xLabel = xColumn.label;
  const measures = yColumns.map((c) => c.label).join(', ');

  if (spec.type === 'scatter' && yIndexes.length >= 2) {
    return scatterChart(rows, xIndex, xColumn, yIndexes, yColumns, spec.title, palette);
  }
  if (spec.type === 'pie') {
    const slices = rows.flatMap((row) => {
      const value = numeric(row[firstY]);
      return value === null ? [] : [{ name: label(row[xIndex] ?? null, xColumn), value }];
    });
    const pie = pieChart(
      slices,
      yColumns[0]?.label ?? '',
      xLabel,
      valueFormat,
      spec.title,
      palette,
    );
    if (pie !== null) {
      return pie;
    }
  }
  if (spec.type === 'grouped_bar' && sIndex !== undefined) {
    const cats: string[] = [];
    const groups = new Map<string, Map<string, number | null>>();
    for (const row of rows) {
      const cat = label(row[xIndex] ?? null, xColumn);
      const group = String(row[sIndex]);
      if (!cats.includes(cat)) {
        cats.push(cat);
      }
      const values = groups.get(group) ?? new Map<string, number | null>();
      groups.set(group, values);
      values.set(cat, numeric(row[firstY]));
    }
    const categories = cats.slice(0, MAX_CATEGORIES);
    const series = [...groups.entries()].slice(0, 12).map(([name, values]) => ({
      name,
      data: categories.map((c) => values.get(c) ?? null),
    }));
    return categoryChart('bar', categories, series, {
      measures,
      xLabel,
      valueFormat,
      truncated: cats.length > MAX_CATEGORIES,
      horizontal: false,
      title: spec.title,
      palette,
    });
  }
  const type = spec.type === 'line' ? 'line' : 'bar';
  const shown = rows.slice(0, MAX_CATEGORIES);
  const series = yColumns.map((column, i) => ({
    name: column.label,
    data: shown.map((row) => numeric(row[yIndexes[i] ?? firstY])),
  }));
  return categoryChart(
    type,
    shown.map((row) => label(row[xIndex] ?? null, xColumn)),
    series,
    {
      measures,
      xLabel,
      valueFormat,
      truncated: rows.length > MAX_CATEGORIES,
      horizontal: spec.type === 'bar',
      title: spec.title,
      palette,
    },
  );
}

/**
 * The chart of a business analysis: the server's validated points (meaningful
 * categories only). Disabled or invalid charts, blank labels and fewer than two points
 * are never drawn.
 */
export function buildAnalysisChartModel(
  chart: AnalysisChart,
  palette = PALETTE,
): ChartModel | null {
  if (!chart.enabled || !chart.validation.valid || chart.type === null) {
    return null;
  }
  if (chart.type === 'scatter') {
    return null; // a relationship needs pairs of values, which points do not carry
  }
  const points = chart.data.flatMap((p) =>
    isBlank(p.label) || p.value === null || !Number.isFinite(p.value)
      ? []
      : [{ name: p.label, value: p.value }],
  );
  if (new Set(points.map((p) => p.name)).size < 2) {
    return null;
  }
  const metric = chart.metric ?? 'Value';
  const dimension = chart.dimension ?? 'category';
  const valueFormat: ValueFormat | null = points.every((p) => Number.isInteger(p.value))
    ? 'integer'
    : null;
  if (chart.type === 'pie') {
    const pie = pieChart(points, metric, dimension, valueFormat, chart.title, palette);
    if (pie !== null) {
      return pie;
    }
  }
  const shown = points.slice(0, MAX_CATEGORIES);
  return categoryChart(
    chart.type === 'line' ? 'line' : 'bar',
    shown.map((p) => p.name),
    [{ name: metric, data: shown.map((p) => p.value) }],
    {
      measures: metric,
      xLabel: dimension,
      valueFormat,
      truncated: points.length > MAX_CATEGORIES,
      horizontal: chart.type !== 'line',
      title: chart.title,
      palette,
    },
  );
}

// ------------------------------------------------------------------ builders

interface CategoryOptions {
  /** What is measured, for the accessible description ("Total sales"). */
  readonly measures: string;
  readonly xLabel: string;
  readonly valueFormat: ValueFormat | null;
  readonly truncated: boolean;
  /** Bars may turn horizontal when their labels are long. */
  readonly horizontal: boolean;
  readonly title: string | null;
  readonly palette: readonly string[];
}

function categoryChart(
  type: 'bar' | 'line',
  categories: readonly string[],
  series: readonly Series[],
  options: CategoryOptions,
): ChartModel {
  const { valueFormat, truncated } = options;
  const horizontal = type === 'bar' && options.horizontal && categories.some((c) => c.length > 14);
  const categoryAxis = {
    type: 'category',
    data: categories,
    axisLabel: { hideOverlap: true, width: horizontal ? 110 : 80, overflow: 'truncate' },
    axisTick: { alignWithLabel: true },
  };
  const valueAxis = {
    type: 'value',
    axisLabel: { formatter: (v: number) => formatValue(v, valueFormat, { compact: true }) },
    splitLine: { lineStyle: { opacity: 0.35 } },
  };
  const description =
    `${type === 'line' ? 'Line' : 'Bar'} chart of ${options.measures} by ${options.xLabel}, ` +
    `${categories.length} ${categories.length === 1 ? 'category' : 'categories'}` +
    (truncated ? ` (first ${MAX_CATEGORIES} shown)` : '') +
    '.';
  const option: ChartOption = {
    aria: { enabled: true, label: { description } },
    color: options.palette,
    grid: { left: 8, right: 16, top: series.length > 1 ? 36 : 16, bottom: 8, containLabel: true },
    tooltip: {
      trigger: 'axis',
      valueFormatter: (v: unknown) => (typeof v === 'number' ? formatValue(v, valueFormat) : '—'),
    },
    ...(series.length > 1 ? { legend: { top: 0, type: 'scroll' } } : {}),
    xAxis: horizontal ? valueAxis : categoryAxis,
    yAxis: horizontal ? { ...categoryAxis, inverse: true } : valueAxis,
    series: series.map((s) => ({
      name: s.name,
      type,
      smooth: type === 'line' ? 0.25 : undefined,
      showSymbol: type === 'line' ? categories.length <= 24 : undefined,
      areaStyle: type === 'line' && series.length === 1 ? { opacity: 0.08 } : undefined,
      data: s.data,
    })),
  };
  return { option, description, truncatedCategories: truncated, title: options.title };
}

/** Shares of a whole: only for a few non-negative slices, else `null` (use bars). */
function pieChart(
  slices: readonly Slice[],
  measure: string,
  xLabel: string,
  valueFormat: ValueFormat | null,
  title: string | null,
  palette: readonly string[],
): ChartModel | null {
  if (slices.length < 2 || slices.length > MAX_PIE_SLICES || slices.some((s) => s.value < 0)) {
    return null;
  }
  const description = `Pie chart of ${measure} by ${xLabel}, ${slices.length} slices.`;
  const option: ChartOption = {
    aria: { enabled: true, label: { description } },
    color: palette,
    tooltip: {
      trigger: 'item',
      valueFormatter: (v: unknown) => (typeof v === 'number' ? formatValue(v, valueFormat) : '—'),
    },
    legend: { bottom: 0, type: 'scroll' },
    series: [
      {
        name: measure,
        type: 'pie',
        radius: ['38%', '68%'],
        center: ['50%', '45%'],
        label: { formatter: '{d}%' },
        data: slices.map((s) => ({ name: s.name, value: s.value })),
      },
    ],
  };
  return { option, description, truncatedCategories: false, title };
}

/** Two measures against each other, one point per category ("sales vs margin"). */
function scatterChart(
  rows: readonly (readonly Scalar[])[],
  xIndex: number,
  xColumn: ResultColumn,
  yIndexes: readonly number[],
  yColumns: readonly ResultColumn[],
  title: string | null,
  palette: readonly string[],
): ChartModel | null {
  const [first, second] = yIndexes;
  const [a, b] = yColumns;
  if (first === undefined || second === undefined || a === undefined || b === undefined) {
    return null;
  }
  const points = rows.slice(0, MAX_CATEGORIES * 5).flatMap((row) => {
    const x = numeric(row[first]);
    const y = numeric(row[second]);
    return x === null || y === null
      ? []
      : [{ name: label(row[xIndex] ?? null, xColumn), value: [x, y] }];
  });
  if (points.length < 2) {
    return null;
  }
  const axis = (column: ResultColumn) => ({
    type: 'value',
    name: column.label,
    nameLocation: 'middle',
    nameGap: 28,
    axisLabel: { formatter: (v: number) => formatValue(v, column.format, { compact: true }) },
    splitLine: { lineStyle: { opacity: 0.35 } },
  });
  const description =
    `Scatter chart of ${a.label} against ${b.label}, ` +
    `one point per ${xColumn.label} (${points.length} points).`;
  const option: ChartOption = {
    aria: { enabled: true, label: { description } },
    color: palette,
    grid: { left: 16, right: 16, top: 16, bottom: 28, containLabel: true },
    tooltip: {
      trigger: 'item',
      formatter: (p: { name?: string; value?: readonly number[] }) => {
        const [x, y] = p.value ?? [];
        return (
          `${p.name ?? ''}: ${a.label} ${formatValue(x ?? null, a.format)}, ` +
          `${b.label} ${formatValue(y ?? null, b.format)}`
        );
      },
    },
    xAxis: axis(a),
    yAxis: axis(b),
    series: [{ type: 'scatter', symbolSize: 10, data: points }],
  };
  return { option, description, truncatedCategories: rows.length > MAX_CATEGORIES * 5, title };
}
