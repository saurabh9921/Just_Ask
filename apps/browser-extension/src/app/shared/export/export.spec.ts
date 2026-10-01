import { queryResponse } from '../../../testing/analytics-fixtures';
import { csvField, exportFileName, responseToCsv, responseToJson, toCsv } from './export';

describe('CSV export', () => {
  it('quotes separators, quotes and line breaks (RFC 4180)', () => {
    expect(csvField('plain')).toBe('plain');
    expect(csvField('a,b')).toBe('"a,b"');
    expect(csvField('say "hi"')).toBe('"say ""hi"""');
    expect(csvField('two\nlines')).toBe('"two\nlines"');
    expect(csvField(' padded ')).toBe('" padded "');
    expect(csvField(null)).toBe('');
    expect(csvField(-12.5)).toBe('-12.5');
  });

  it.each(['=HYPERLINK("http://evil")', '+1+2', '-2+3', '@SUM(A1)', '\tcmd'])(
    'neutralises spreadsheet formulas: %s',
    (value) => {
      expect(csvField(value).replace(/^"/, '').startsWith("'")).toBe(true);
    },
  );

  it('writes a BOM, header and CRLF rows', () => {
    const csv = toCsv(
      ['Region', 'Sales'],
      [
        ['North', 1],
        ['South', null],
      ],
    );
    expect(csv).toBe('﻿Region,Sales\r\nNorth,1\r\nSouth,\r\n');
  });

  it('exports a result with its column labels', () => {
    const csv = responseToCsv(queryResponse());
    expect(csv.split('\r\n')[0]).toBe('﻿Region,Total sales');
    expect(csv).toContain('South,1751966.79');
  });
});

describe('JSON export', () => {
  it('keeps rows keyed by column plus the context to interpret them', () => {
    const json = JSON.parse(responseToJson(queryResponse(), new Date('2026-09-23T10:00:00Z'))) as {
      rows: Record<string, unknown>[];
      data_source: string;
      date_window: { start: string };
      exported_at: string;
    };
    expect(json.rows[0]).toEqual({ region: 'South', total_sales: 1751966.79 });
    expect(json.data_source).toBe('ERP prod');
    expect(json.date_window.start).toBe('2026-03-16');
    expect(json.exported_at).toBe('2026-09-23T10:00:00.000Z');
    expect(JSON.stringify(json)).not.toContain('SELECT');
  });

  it('builds safe file names', () => {
    expect(exportFileName('Sales / region: "Q3"?', 'csv', new Date('2026-09-23T10:11:12Z'))).toBe(
      'erp-insight-sales-region-q3-2026-09-23-10-11-12.csv',
    );
    expect(exportFileName('???', 'json', new Date('2026-09-23T10:11:12Z'))).toMatch(
      /^erp-insight-query-/,
    );
  });
});
