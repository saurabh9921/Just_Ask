import type { QueryResponse, Scalar } from '../../core/messaging/analytics-types';

/** Cells starting with these characters are treated as formulas by spreadsheets. */
const FORMULA_PREFIX = /^[=+\-@\t\r]/;

/**
 * One CSV field (RFC 4180). Text that a spreadsheet would evaluate as a formula is
 * prefixed with an apostrophe (CSV/formula-injection defence); numbers are kept as
 * numbers so they stay numeric in the spreadsheet.
 */
export function csvField(value: Scalar): string {
  if (value === null) {
    return '';
  }
  if (typeof value === 'number' || typeof value === 'boolean') {
    return String(value);
  }
  let text = value;
  if (FORMULA_PREFIX.test(text)) {
    text = `'${text}`;
  }
  if (/[",\r\n]/.test(text) || text !== text.trim()) {
    text = `"${text.replace(/"/g, '""')}"`;
  }
  return text;
}

export function toCsv(header: readonly string[], rows: readonly (readonly Scalar[])[]): string {
  const lines = [header.map(csvField).join(','), ...rows.map((row) => row.map(csvField).join(','))];
  // BOM so spreadsheet applications detect UTF-8; CRLF per RFC 4180.
  return `﻿${lines.join('\r\n')}\r\n`;
}

/** CSV of a query result (column labels as header). */
export function responseToCsv(response: QueryResponse): string {
  const result = response.result;
  if (result === null) {
    return toCsv([], []);
  }
  return toCsv(
    result.columns.map((c) => c.label),
    result.rows,
  );
}

/** JSON export: the rows plus the context needed to interpret them (no SQL values). */
export function responseToJson(response: QueryResponse, exportedAt = new Date()): string {
  const result = response.result;
  const columns = result?.columns ?? [];
  return JSON.stringify(
    {
      question: response.question,
      query_id: response.queryId,
      exported_at: exportedAt.toISOString(),
      data_source: response.connection.name,
      data_freshness: response.dataFreshness,
      description: response.description,
      date_window: response.appliedFilters.dateWindow,
      previous_window: response.appliedFilters.previousWindow,
      filters: response.appliedFilters.filters,
      assumptions: response.assumptions,
      columns: columns.map((c) => ({ name: c.name, label: c.label, type: c.type })),
      rows: (result?.rows ?? []).map((row) =>
        Object.fromEntries(columns.map((c, i) => [c.name, row[i] ?? null])),
      ),
      row_count: result?.rowCount ?? 0,
      truncated: result?.truncated ?? false,
    },
    null,
    2,
  );
}

export function exportFileName(
  question: string,
  extension: 'csv' | 'json',
  now = new Date(),
): string {
  const slug =
    question
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, '-')
      .replace(/^-+|-+$/g, '')
      .slice(0, 60) || 'query';
  const stamp = now.toISOString().slice(0, 19).replace(/[:T]/g, '-');
  return `erp-insight-${slug}-${stamp}.${extension}`;
}

/** Triggers a download of in-memory content (no network, no storage). */
export function downloadFile(
  fileName: string,
  mimeType: string,
  content: string,
  doc: Document = document,
): void {
  const blob = new Blob([content], { type: mimeType });
  const url = URL.createObjectURL(blob);
  const anchor = doc.createElement('a');
  anchor.href = url;
  anchor.download = fileName;
  anchor.rel = 'noopener';
  anchor.style.display = 'none';
  doc.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}
