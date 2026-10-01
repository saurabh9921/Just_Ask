import {
  formatDate,
  formatDuration,
  formatPercentChange,
  formatRelative,
  formatValue,
  humanize,
} from './value-format';

describe('value formatting', () => {
  it('formats by column format', () => {
    expect(formatValue(null, 'currency')).toBe('—');
    expect(formatValue(true, null)).toBe('Yes');
    expect(formatValue(1234.5, 'currency')).toMatch(/1.234.50|1,234\.50/);
    expect(formatValue(12.345, 'percent')).toMatch(/12.35%/);
    expect(formatValue(7.6, 'integer')).toBe('8');
    expect(formatValue('North', null)).toBe('North');
  });

  it('compacts large KPI values', () => {
    const compact = new Intl.NumberFormat(undefined, {
      notation: 'compact',
      maximumFractionDigits: 2,
    });
    expect(formatValue(6008638.33, 'currency', { compact: true })).toBe(compact.format(6008638.33));
    expect(formatValue(950, 'currency', { compact: true })).toMatch(/950/);
  });

  it('formats changes, durations and dates', () => {
    expect(formatPercentChange(-40.16)).toMatch(/^-40.16%$/);
    expect(formatPercentChange(3)).toBe('+3%');
    expect(formatDuration(412.3)).toBe('412 ms');
    expect(formatDuration(1520)).toBe('1.5 s');
    expect(formatDuration(null)).toBe('—');
    expect(formatDate('2026-09-15')).toContain('2026');
    expect(formatDate('not a date')).toBe('not a date');
    expect(humanize('wrong_period')).toBe('Wrong period');
  });

  it('describes freshness relative to now', () => {
    const now = Date.parse('2026-09-23T10:00:00Z');
    expect(formatRelative('2026-09-23T09:55:00Z', now)).toMatch(/5 minutes ago/);
    expect(formatRelative('2026-09-21T10:00:00Z', now)).toMatch(/2 days ago/);
    expect(formatRelative(null, now)).toBe('Unknown');
  });
});
