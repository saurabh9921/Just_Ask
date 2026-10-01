import type { Scalar, ValueFormat } from '../../core/messaging/analytics-types';

const LOCALE = undefined; // the browser's locale

const decimal = new Intl.NumberFormat(LOCALE, { maximumFractionDigits: 2 });
const money = new Intl.NumberFormat(LOCALE, {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});
const integer = new Intl.NumberFormat(LOCALE, { maximumFractionDigits: 0 });
const compact = new Intl.NumberFormat(LOCALE, { notation: 'compact', maximumFractionDigits: 2 });

/**
 * Formats a result cell for display. Currency values are shown with two decimals and
 * no symbol: the API does not (yet) tell the client which currency a column is in,
 * and guessing one would be worse than showing none.
 */
export function formatValue(
  value: Scalar,
  format: ValueFormat | null,
  options: { readonly compact?: boolean } = {},
): string {
  if (value === null) {
    return '—';
  }
  if (typeof value === 'boolean') {
    return value ? 'Yes' : 'No';
  }
  if (typeof value === 'string') {
    return format === 'date' ? formatDate(value) : value;
  }
  if (options.compact === true && Math.abs(value) >= 10_000 && format !== 'percent') {
    return compact.format(value);
  }
  switch (format) {
    case 'currency':
      return money.format(value);
    case 'integer':
      return integer.format(value);
    case 'percent':
      return `${decimal.format(value)}%`;
    default:
      return decimal.format(value);
  }
}

export function formatPercentChange(value: number | null): string {
  if (value === null) {
    return '';
  }
  const sign = value > 0 ? '+' : '';
  return `${sign}${decimal.format(value)}%`;
}

export function formatDuration(ms: number | null): string {
  if (ms === null) {
    return '—';
  }
  if (ms < 1000) {
    return `${Math.round(ms)} ms`;
  }
  return `${(ms / 1000).toFixed(ms < 10_000 ? 1 : 0)} s`;
}

/** `YYYY-MM-DD` dates stay as calendar dates (no time-zone shift). */
export function formatDate(value: string): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (match === null) {
    return value;
  }
  const date = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
  return date.toLocaleDateString(LOCALE, { year: 'numeric', month: 'short', day: 'numeric' });
}

export function formatDateTime(iso: string | null): string {
  if (iso === null) {
    return '—';
  }
  const date = new Date(iso);
  return Number.isNaN(date.getTime())
    ? iso
    : date.toLocaleString(LOCALE, { dateStyle: 'medium', timeStyle: 'short' });
}

/** "5 minutes ago", "2 days ago" (freshness indicators). */
export function formatRelative(iso: string | null, now: number = Date.now()): string {
  if (iso === null) {
    return 'Unknown';
  }
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) {
    return 'Unknown';
  }
  const seconds = Math.round((then - now) / 1000);
  const rtf = new Intl.RelativeTimeFormat(LOCALE, { numeric: 'auto' });
  const units: readonly [Intl.RelativeTimeFormatUnit, number][] = [
    ['day', 86_400],
    ['hour', 3600],
    ['minute', 60],
  ];
  for (const [unit, size] of units) {
    if (Math.abs(seconds) >= size) {
      return rtf.format(Math.round(seconds / size), unit);
    }
  }
  return rtf.format(seconds, 'second');
}

export function humanize(code: string): string {
  const text = code.replace(/[._]/g, ' ').trim().toLowerCase();
  return text.charAt(0).toUpperCase() + text.slice(1);
}
