import type {
  Analysis,
  DateWindow,
  FormattedResult,
  KpiCard,
  QueryResponse,
  ResultColumn,
  Scalar,
} from '../../core/messaging/analytics-types';
import { isBlank } from '../../dashboards/chart-options';
import { formatDate, formatValue } from '../../shared/format/value-format';

/** The plain-language summary shown after an answer's visual. */
export interface AnswerSummary {
  /** A business analysis's title ("Lead Analysis — Pending Metals Co"). */
  readonly title: string | null;
  /** The answer in plain sentences. */
  readonly text: string;
  /** A business analysis's validated statements, one per bullet (else empty). */
  readonly points: readonly string[];
  /** Missing data and figures the data cannot give - stated apart, never charted. */
  readonly notes: readonly string[];
  /** The date range the figures cover, when the question was limited to one. */
  readonly period: string | null;
}

/** A result column together with its position in each row. */
interface Field {
  readonly index: number;
  readonly column: ResultColumn;
}

interface Entry {
  readonly label: string;
  readonly value: number;
}

/** Rows left out of a description because a label is blank: reported, not ranked. */
interface Blank {
  readonly rows: number;
  readonly value: number;
}

const MAX_LABEL = 60;
const MAX_KPIS = 3;
const ONE_DECIMAL = new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 });
const WHOLE = new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 });

/**
 * Summarises an answer in two or three plain sentences: the highest and lowest values,
 * shares of the total, the trend over time or the change against the previous period.
 *
 * Built from the result itself, so every number is the one the chart/KPI cards show
 * (a model never restates figures here). Shares and totals use only the server's
 * "total" KPIs, which exist for additive measures alone - averages are never summed.
 * Blank values are never a category: those rows are left out of rankings and the
 * missing data is stated separately (`notes`), as are fields the server found unfit
 * to describe (free text, identifiers, mostly blank). A business analysis is stated
 * with the server's validated insights. Shapes it cannot describe fall back to the
 * model's explanation, then the plan description.
 */
export function summarizeAnswer(response: QueryResponse): AnswerSummary | null {
  const period = describePeriod(response);
  const analysis = response.analysis;
  if (analysis !== null && analysis.title !== null) {
    return describeAnalysis(analysis, period);
  }
  const result = response.result;
  if (result === null) {
    return null;
  }
  const sentences = describeResult(result);
  const text = sentences.length > 0 ? sentences.join(' ') : fallback(response);
  if (text === null) {
    return null;
  }
  const notes = unique(analysis?.warnings ?? result.warnings);
  return { title: null, text, points: [], notes, period };
}

/** "Lead Analysis — Pending Metals Co", its statements, then what cannot be known. */
function describeAnalysis(analysis: Analysis, period: string | null): AnswerSummary {
  // An observation that only restates unavailable figures is said by the notes.
  const gaps = new Set(analysis.unavailable.map((u) => u.key));
  const points = analysis.insights
    .filter(
      (i) =>
        !(i.kind === 'observation' && i.metrics.length > 0 && i.metrics.every((k) => gaps.has(k))),
    )
    .map((i) => i.text);
  const byReason = new Map<string, string[]>();
  for (const missing of analysis.unavailable) {
    byReason.set(missing.reason, [...(byReason.get(missing.reason) ?? []), missing.label]);
  }
  const unavailable = [...byReason.entries()].map(
    ([reason, labels]) =>
      `${joinAnd(labels.map((l, i) => (i === 0 ? l : phrase(l))))} cannot be calculated: ` +
      phrase(reason),
  );
  // A warning that only repeats why a figure is unavailable is stated once.
  const reasons = new Set(analysis.unavailable.map((u) => normalize(u.reason)));
  const warnings = analysis.warnings.filter((w) => !reasons.has(normalize(w)));
  return {
    title: analysis.title,
    text: points.join(' '),
    points,
    notes: unique([...unavailable, ...warnings]),
    period,
  };
}

// ------------------------------------------------------------------ shapes

function describeResult(result: FormattedResult): string[] {
  if (result.rows.length === 0) {
    return ['No data matched this question.'];
  }
  const fields: Field[] = result.columns.map((column, index) => ({ column, index }));
  // Free text, identifiers and mostly-blank fields describe nothing (see `notes`).
  const unfit = new Set(result.quality.filter((q) => !q.usable).map((q) => q.name));
  const describable = (f: Field) =>
    !unfit.has(f.column.name) && f.column.role !== 'free_text' && f.column.role !== 'identifier';
  const labelled = fields.some((f) => f.column.kind === 'dimension' || f.column.kind === 'time');
  const dims = fields.filter((f) => f.column.kind === 'dimension' && describable(f));
  const times = fields.filter((f) => f.column.kind === 'time' && describable(f));
  const [firstDim] = dims;
  const [firstTime] = times;
  const measure = primaryMeasure(result, fields);
  let sentences: string[];
  if (firstDim === undefined && firstTime === undefined) {
    sentences = describeFigures(result.kpis);
    if (sentences.length === 0 && labelled) {
      sentences = [describeCount(result, undefined)];
    }
  } else if (measure === undefined) {
    sentences = [describeCount(result, firstDim ?? firstTime)];
  } else if (result.columns.some((c) => c.kind === 'change_pct')) {
    sentences = describeComparison(result, [...dims, ...times], measure);
  } else if (firstTime !== undefined && dims.length <= 1) {
    sentences = describeTrend(result, firstTime, firstDim, measure);
  } else {
    sentences = describeRanking(result, [...dims, ...times], measure);
  }
  if (sentences.length > 0 && result.truncated) {
    sentences.push(`Only the first ${WHOLE.format(result.rows.length)} rows are shown.`);
  }
  return sentences;
}

/** "Total sales is 1.2M, up 5.2% from 1.14M in the previous period." */
function describeFigures(kpis: readonly KpiCard[]): string[] {
  const shown = kpis.slice(0, MAX_KPIS);
  if (shown.length === 0) {
    return [];
  }
  const exact = shown.length <= 2;
  const parts = shown.map((kpi, i) => {
    const name = i === 0 ? kpi.label : phrase(kpi.label);
    return `${name} is ${figure(kpi.value, kpi.format, exact)}${changeSince(kpi)}`;
  });
  return [`${capitalize(joinAnd(parts))}.`];
}

/** "South has the highest total sales at 1.75M (29% of the total). …" */
function describeRanking(result: FormattedResult, labels: readonly Field[], m: Field): string[] {
  const measure = phrase(m.column.label);
  const value = compact(m.column);
  const total = totalOf(result, m.column.name);
  const { entries, blank } = rowEntries(result, labels, m);
  const sorted = entries.sort((a, b) => b.value - a.value);
  const [top, next] = sorted;
  const bottom = sorted.at(-1);
  if (top === undefined || bottom === undefined) {
    return blank.rows > 0 ? [notRecorded(labels)] : [describeCount(result, labels[0])];
  }
  if (top === bottom) {
    return [`${top.label}: ${measure} is ${value(top.value)}${withBlank(blank, labels, value)}.`];
  }
  const sentences = [
    `${top.label} has the highest ${measure} at ${value(top.value)}${shareOf(top.value, total)}.`,
  ];
  if (next === undefined || next === bottom) {
    sentences.push(`${bottom.label} has ${value(bottom.value)}.`);
  } else {
    sentences.push(
      `${next.label} is next at ${value(next.value)}, ` +
        `and ${bottom.label} is the lowest at ${value(bottom.value)}.`,
    );
  }
  if (total !== null) {
    sentences.push(
      blank.rows > 0
        ? `In total: ${value(total)}, including ${value(blank.value)} with no ` +
            `${labelNames(labels)}.`
        : `Together they total ${value(total)}.`,
    );
  }
  return sentences;
}

/** "Source is not recorded for these rows." */
function notRecorded(labels: readonly Field[]): string {
  return `${capitalize(labelNames(labels))} is not recorded for these rows.`;
}

/** " (another 14 have no source)" - stated, never shown as a "(blank)" category. */
function withBlank(blank: Blank, labels: readonly Field[], value: (v: number) => string): string {
  return blank.rows > 0 ? ` (another ${value(blank.value)} with no ${labelNames(labels)})` : '';
}

function labelNames(labels: readonly Field[]): string {
  return joinAnd(labels.map((f) => phrase(f.column.label)));
}

/** "Total sales went from 410K in Jan 2026 to 520K in Jun 2026 (up 26.8%). …" */
function describeTrend(
  result: FormattedResult,
  t: Field,
  series: Field | undefined,
  m: Field,
): string[] {
  const measure = phrase(m.column.label);
  const value = compact(m.column);
  const total = totalOf(result, m.column.name);
  if (series !== undefined) {
    return total === null
      ? describePeak(result, [series, t], m)
      : describeSeriesTotals(result, series, m, total);
  }

  const points = result.rows
    .map((row) => ({ time: row[t.index] ?? null, value: numeric(row[m.index]) }))
    .filter((p): p is { time: Scalar; value: number } => p.value !== null && p.time !== null)
    .sort((a, b) => compareScalars(a.time, b.time));
  const [first] = points;
  const last = points.at(-1);
  if (first === undefined || last === undefined) {
    return [describeCount(result, t)];
  }
  const when = (time: Scalar) => timeLabel(time, t.column);
  if (first === last) {
    return [`${capitalize(measure)} was ${value(first.value)} in ${when(first.time)}.`];
  }
  const change =
    first.value !== 0 ? ((last.value - first.value) / Math.abs(first.value)) * 100 : null;
  const sentences = [
    `${capitalize(measure)} went from ${value(first.value)} in ${when(first.time)} to ` +
      `${value(last.value)} in ${when(last.time)}` +
      `${change === null ? '' : ` (${direction(change)})`}.`,
  ];
  const peak = points.reduce((best, p) => (p.value > best.value ? p : best), first);
  if (peak !== first && peak !== last) {
    sentences.push(`The peak was ${when(peak.time)} at ${value(peak.value)}.`);
  }
  if (total !== null) {
    sentences.push(`In total: ${value(total)}.`);
  }
  return sentences;
}

/** Additive measure over time per series: rank the series by their totals. */
function describeSeriesTotals(
  result: FormattedResult,
  series: Field,
  m: Field,
  total: number,
): string[] {
  const measure = phrase(m.column.label);
  const value = compact(m.column);
  const sums = new Map<string, number>();
  for (const row of result.rows) {
    const v = numeric(row[m.index]);
    if (v !== null && !isBlank(row[series.index])) {
      const key = cellLabel(row[series.index] ?? null, series.column);
      sums.set(key, (sums.get(key) ?? 0) + v);
    }
  }
  const ranked = [...sums.entries()].sort((a, b) => b[1] - a[1]);
  const [top] = ranked;
  const last = ranked.at(-1);
  if (top === undefined || last === undefined) {
    return [describeCount(result, series)];
  }
  const sentences = [
    `${top[0]} contributed the most ${measure} ` +
      `(${value(top[1])}${shareOf(top[1], total, true)}).`,
  ];
  if (last !== top) {
    sentences.push(`${last[0]} contributed the least (${value(last[1])}).`);
  }
  sentences.push(`In total: ${value(total)}.`);
  return sentences;
}

/** Non-additive measure: name the single highest value instead of adding values up. */
function describePeak(result: FormattedResult, labels: readonly Field[], m: Field): string[] {
  const [best] = rowEntries(result, labels, m).entries.sort((a, b) => b.value - a.value);
  return best === undefined
    ? [describeCount(result, labels[0])]
    : [
        `The highest ${phrase(m.column.label)} was ${best.label} at ${compact(m.column)(best.value)}.`,
      ];
}

/** "Overall, total sales is 6.01M, up 4.3% from 5.76M … North grew the most, up 12.5%." */
function describeComparison(result: FormattedResult, labels: readonly Field[], m: Field): string[] {
  const { columns, rows } = result;
  const measure = phrase(m.column.label);
  const value = compact(m.column);
  const previousIdx = columns.findIndex((c) => c.name === `${m.column.name}_previous`);
  const pctIdx = columns.findIndex((c) => c.name === `${m.column.name}_change_pct`);
  const labelled = rows.filter((row) => labels.every((f) => !isBlank(row[f.index])));
  const entries = labelled.map((row) => ({
    label: rowLabel(row, labels),
    previous: previousIdx < 0 ? null : numeric(row[previousIdx]),
    pct: pctIdx < 0 ? null : numeric(row[pctIdx]),
  }));

  const sentences: string[] = [];
  const total = totalOf(result, m.column.name);
  const complete = labelled.length === rows.length; // the total includes blank rows
  if (total !== null && complete && entries.every((e) => e.previous !== null)) {
    const before = entries.reduce((sum, e) => sum + (e.previous ?? 0), 0);
    const change = before !== 0 ? ((total - before) / Math.abs(before)) * 100 : null;
    sentences.push(
      `Overall, ${measure} is ${value(total)}` +
        (change === null
          ? '.'
          : `, ${direction(change)} from ${value(before)} in the previous period.`),
    );
  }
  const changed = entries
    .filter((e): e is typeof e & { pct: number } => e.pct !== null)
    .sort((a, b) => b.pct - a.pct);
  const [best] = changed;
  const worst = changed.at(-1);
  if (best !== undefined && best.pct > 0) {
    sentences.push(`${best.label} grew the most, ${direction(best.pct)}.`);
  }
  if (worst !== undefined && worst.pct < 0) {
    sentences.push(`${worst.label} fell the most, ${direction(worst.pct)}.`);
  }
  if (best !== undefined && best.pct <= 0 && worst !== undefined && worst.pct >= 0) {
    sentences.push(`${capitalize(measure)} is unchanged from the previous period.`);
  }
  return sentences.length > 0 ? sentences : describeRanking(result, labels, m);
}

/** "2 companies found." */
function describeCount(result: FormattedResult, label: Field | undefined): string {
  const n = result.rows.length;
  const noun = label === undefined ? 'row' : phrase(label.column.label);
  return `${WHOLE.format(n)} ${n === 1 ? noun : plural(noun)} found.`;
}

// ------------------------------------------------------------------ helpers

function describePeriod(response: QueryResponse): string | null {
  const { dateWindow, previousWindow } = response.appliedFilters;
  if (dateWindow === null) {
    return null;
  }
  const range = formatRange(dateWindow);
  return previousWindow === null
    ? range
    : `${range} (compared with ${formatRange(previousWindow)})`;
}

function formatRange(window: DateWindow): string {
  const end = dayBefore(window.endExclusive);
  return end === null ? window.label : `${formatDate(window.start)} – ${formatDate(end)}`;
}

function dayBefore(iso: string): string | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso);
  if (match === null) {
    return null;
  }
  const d = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]) - 1);
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

/** The model's own words, minus highlights that just repeat the summary. */
function fallback(response: QueryResponse): string | null {
  const explanation = response.explanation;
  if (explanation === null) {
    return response.description;
  }
  const summary = normalize(explanation.summary);
  const extra = explanation.highlights.filter((h) => normalize(h) !== summary);
  return [explanation.summary, ...extra].join(' ');
}

/** The charted measure when there is one, else the first measure. */
function primaryMeasure(result: FormattedResult, fields: readonly Field[]): Field | undefined {
  const measures = fields.filter((f) => f.column.kind === 'measure');
  const charted = result.chart.y[0];
  return measures.find((f) => f.column.name === charted) ?? measures[0];
}

/** Rows with a value and every label recorded; blank-labelled rows are counted apart. */
function rowEntries(
  result: FormattedResult,
  labels: readonly Field[],
  m: Field,
): { entries: Entry[]; blank: Blank } {
  const entries: Entry[] = [];
  let rows = 0;
  let sum = 0;
  for (const row of result.rows) {
    const value = numeric(row[m.index]);
    if (value === null) {
      continue;
    }
    if (labels.some((f) => isBlank(row[f.index]))) {
      rows += 1;
      sum += value;
    } else {
      entries.push({ label: rowLabel(row, labels), value });
    }
  }
  return { entries, blank: { rows, value: sum } };
}

function rowLabel(row: readonly Scalar[], labels: readonly Field[]): string {
  return labels.map((f) => cellLabel(row[f.index] ?? null, f.column)).join(' – ');
}

function cellLabel(value: Scalar, column: ResultColumn): string {
  const text =
    column.kind === 'time'
      ? timeLabel(value, column)
      : isBlank(value)
        ? '—'
        : formatValue(value, column.format);
  return text.length > MAX_LABEL ? `${text.slice(0, MAX_LABEL - 1)}…` : text;
}

/** Time buckets in the words people use: "Jan 2026", "Q3 2026", "2026". */
function timeLabel(value: Scalar, column: ResultColumn): string {
  if (typeof value !== 'string') {
    return formatValue(value, column.format);
  }
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(value);
  if (match === null) {
    return value;
  }
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = `${match[1]}-${match[2]}-${match[3]}`;
  switch (column.timeGrain) {
    case 'year':
      return String(year);
    case 'quarter':
      return `Q${Math.floor((month - 1) / 3) + 1} ${year}`;
    case 'month':
      return new Date(year, month - 1, 1).toLocaleDateString(undefined, {
        month: 'short',
        year: 'numeric',
      });
    case 'week':
      return `the week of ${formatDate(day)}`;
    default:
      return formatDate(day);
  }
}

function compact(column: ResultColumn): (value: number) => string {
  return (value) => formatValue(value, column.format, { compact: true });
}

function totalOf(result: FormattedResult, measureName: string): number | null {
  const kpi = result.kpis.find((k) => k.scope === 'total' && k.name === measureName);
  return kpi === undefined ? null : numeric(kpi.value);
}

function shareOf(value: number, total: number | null, inline = false): string {
  if (total === null || total <= 0 || value < 0) {
    return '';
  }
  const share = (value / total) * 100;
  const text = `${(share < 10 ? ONE_DECIMAL : WHOLE).format(share)}% of the total`;
  return inline ? `, ${text}` : ` (${text})`;
}

function figure(value: Scalar, format: KpiCard['format'], exact: boolean): string {
  const short = formatValue(value, format, { compact: true });
  const full = formatValue(value, format);
  return exact && short !== full ? `${short} (${full})` : short;
}

function changeSince(kpi: KpiCard): string {
  if (kpi.changePct === null) {
    return '';
  }
  const previous = formatValue(kpi.previous, kpi.format, { compact: true });
  return `, ${direction(kpi.changePct)} from ${previous} in the previous period`;
}

function direction(pct: number): string {
  if (pct === 0) return 'unchanged';
  return `${pct > 0 ? 'up' : 'down'} ${ONE_DECIMAL.format(Math.abs(pct))}%`;
}

function numeric(value: Scalar | undefined): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function compareScalars(a: Scalar, b: Scalar): number {
  if (typeof a === 'number' && typeof b === 'number') return a - b;
  return String(a).localeCompare(String(b));
}

/** A label inside a sentence: "Total sales" → "total sales"; "GST amount" stays. */
function phrase(label: string): string {
  return /^[A-Z][a-z]/.test(label) ? label.charAt(0).toLowerCase() + label.slice(1) : label;
}

function capitalize(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function joinAnd(parts: readonly string[]): string {
  const last = parts.at(-1);
  if (parts.length <= 1 || last === undefined) return parts.join('');
  return `${parts.slice(0, -1).join(', ')} and ${last}`;
}

/** Plural of a label's last word: "company" → "companies", "branch" → "branches". */
function plural(noun: string): string {
  if (/[^aeiou]y$/i.test(noun)) return `${noun.slice(0, -1)}ies`;
  if (/(s|x|z|ch|sh)$/i.test(noun)) return `${noun}es`;
  return `${noun}s`;
}

function unique(texts: readonly string[]): string[] {
  return [...new Set(texts.map((t) => t.trim()).filter((t) => t !== ''))];
}

function normalize(text: string): string {
  return text
    .trim()
    .replace(/\s+/g, ' ')
    .replace(/[.\s]+$/, '')
    .toLowerCase();
}
