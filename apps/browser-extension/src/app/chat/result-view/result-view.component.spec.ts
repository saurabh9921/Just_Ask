import { TestBed } from '@angular/core/testing';

import type { QueryResponse } from '../../core/messaging/analytics-types';
import { formatDate, formatValue } from '../../shared/format/value-format';
import { allLeads, blankBreakdown, companyWithoutLeads } from '../../../testing/analysis-fixtures';
import { queryResponse } from '../../../testing/analytics-fixtures';
import { ResultViewComponent } from './result-view.component';

async function render(response: QueryResponse, canExport = true) {
  TestBed.configureTestingModule({ imports: [ResultViewComponent] });
  const fixture = TestBed.createComponent(ResultViewComponent);
  fixture.componentRef.setInput('response', response);
  fixture.componentRef.setInput('canExport', canExport);
  fixture.detectChanges();
  await fixture.whenStable();
  fixture.detectChanges();
  const element = fixture.nativeElement as HTMLElement;
  const summary = element.querySelector<HTMLElement>('[data-testid="answer-summary"]');
  return { element, summary };
}

/** No tabs, no query plan and no provenance panel - just the visual and its summary. */
function expectNoExtras(element: HTMLElement): void {
  expect(element.querySelector('[role="tablist"]')).toBeNull();
  expect(element.querySelector('eia-query-plan-viewer')).toBeNull();
  expect(element.querySelector('eia-source-metadata')).toBeNull();
}

function follows(first: Element | null, second: Element | null): boolean {
  return (
    first !== null &&
    second !== null &&
    (first.compareDocumentPosition(second) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0
  );
}

/** "total revenue": one row of figures only (the screenshot's case). */
function singleFigure(): QueryResponse {
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
  return queryResponse({
    result: {
      columns: [
        { name: 'total', label: 'Total', kind: 'measure', type: 'decimal', format: 'currency' },
        { name: 'total_inr', label: 'Total inr', kind: 'measure', type: 'decimal' },
      ],
      rows: [[116802593676.63, 9753016572752.72]],
      row_count: 1,
      truncated: false,
      kpis: [
        kpi('total', 'Total', 116802593676.63, 'currency'),
        kpi('total_inr', 'Total inr', 9753016572752.72, null),
      ],
      chart: { type: 'kpi', x: null, y: ['total'], series: null },
      redacted_columns: [],
    },
    explanation: {
      summary: 'total: 116,802,593,676.63.',
      highlights: ['total: 116,802,593,676.63.'],
      source: 'llm',
    },
    applied_filters: { date_window: null, previous_window: null, filters: [] },
  });
}

/** A list that cannot be charted (text columns only). */
function listAnswer(): QueryResponse {
  return queryResponse({
    result: {
      columns: [
        { name: 'company', label: 'Company', kind: 'dimension', type: 'string', format: null },
        { name: 'city', label: 'City', kind: 'dimension', type: 'string', format: null },
      ],
      rows: [
        ['Acme Traders', 'Pune'],
        ['Sun Exports', 'Mumbai'],
      ],
      row_count: 2,
      truncated: false,
      kpis: [],
      chart: { type: 'table', x: null, y: [], series: null },
      redacted_columns: [],
    },
  });
}

describe('ResultViewComponent', () => {
  it('shows a chart answer as the chart followed by a plain-language summary', async () => {
    const { element, summary } = await render(queryResponse());
    const chart = element.querySelector('[data-testid="chart-answer"] eia-result-chart');
    expect(chart).not.toBeNull();
    expect(follows(chart, summary)).toBe(true); // the summary comes at the end
    expect(summary!.textContent).toContain('Summary');
    expect(summary!.textContent).toContain('South has the highest total sales');
    expect(summary!.textContent).toContain('Together they total');
    // The model's terse text is not repeated above the chart.
    expect(element.textContent).not.toContain('4 regions; South is highest.');
    expect(element.querySelector('eia-result-table')).toBeNull();
    expect(element.querySelector('eia-kpi-card')).toBeNull();
    expectNoExtras(element);
  });

  it('shows a single-figure answer as KPI cards and states each figure once in the summary', async () => {
    const { element, summary } = await render(singleFigure());
    const cards = element.querySelectorAll('[data-testid="kpi-answer"] eia-kpi-card');
    expect(cards).toHaveLength(2);
    expect(follows(cards[1]!, summary)).toBe(true);
    expect(element.querySelector('eia-result-table')).toBeNull();
    expectNoExtras(element);
    const exact = formatValue(116802593676.63, 'currency');
    expect(summary!.textContent).toContain(`Total is`);
    expect(element.textContent.split(exact)).toHaveLength(2); // exactly once
  });

  it('shows a list that cannot be charted as a plain table, then its summary', async () => {
    const { element, summary } = await render(listAnswer());
    const table = element.querySelector('[data-testid="table-answer"] eia-result-table');
    expect(table).not.toBeNull();
    expect(element.textContent).toContain('Acme Traders');
    expect(follows(table, summary)).toBe(true);
    expect(summary!.textContent).toContain('2 companies found.');
    expectNoExtras(element);
  });

  it('states the period inside the summary, only when one was applied', async () => {
    const { summary } = await render(queryResponse());
    expect(summary!.querySelector('.takeaway__period')!.textContent).toContain(
      `Period: ${formatDate('2026-03-16')} – ${formatDate('2026-09-15')}`,
    );

    TestBed.resetTestingModule();
    const allTime = await render(singleFigure());
    expect(allTime.summary!.querySelector('.takeaway__period')).toBeNull();
  });

  it('keeps the data available: answers can still be exported', async () => {
    const { element } = await render(queryResponse());
    expect(element.querySelector('[aria-label="Export"]')?.textContent).toContain('CSV');

    TestBed.resetTestingModule();
    const withoutPermission = await render(queryResponse(), false);
    expect(withoutPermission.element.querySelector('[aria-label="Export"]')).toBeNull();
  });

  it('still shows the plan for "Plan only" answers, which have no rows', async () => {
    const { element, summary } = await render(queryResponse({ status: 'planned', result: null }));
    expect(element.querySelector('eia-query-plan-viewer')).not.toBeNull();
    expect(summary).toBeNull();
  });

  it('shows a rejected breakdown as its figures and notes, never as a "(blank)" chart', async () => {
    const { element, summary } = await render(blankBreakdown());
    expect(element.querySelector('eia-result-chart')).toBeNull();
    expect(element.querySelector('eia-result-table')).toBeNull();
    expect(element.querySelectorAll('[data-testid="kpi-answer"] eia-kpi-card')).toHaveLength(1);
    expect(summary!.querySelector('.takeaway__text')!.textContent).toContain('Record count is 28.');
    expect(summary!.querySelector('[data-testid="answer-notes"]')!.textContent).toContain(
      'Analysis note is not recorded for any of the 28 lead candidates.',
    );
    expect(element.textContent).not.toContain('(blank)');
  });

  it('shows a business analysis as its validated chart, then title, points and notes', async () => {
    const { element, summary } = await render(allLeads());
    const chart = element.querySelector('[data-testid="chart-answer"] eia-result-chart');
    expect(chart).not.toBeNull();
    expect(chart!.textContent).toContain('Leads by stage');
    expect(follows(chart, summary)).toBe(true);
    expect(summary!.querySelector('[data-testid="answer-title"]')!.textContent).toContain(
      'Lead Analysis',
    );
    const points = [...summary!.querySelectorAll('.takeaway__points li')].map((li) =>
      li.textContent.trim(),
    );
    expect(points).toEqual([
      '28 leads in total.',
      '50% of leads are “New” (14 of 28); then “Qualified” 8, “Won” 6.',
      '4 leads created in the last 30 days.',
    ]);
    expect(summary!.querySelector('[data-testid="answer-notes"]')!.textContent).toContain(
      'cannot be calculated: the leads data has no value or amount field.',
    );
    expectNoExtras(element);
  });

  it('falls back to the headline figures when an analysis chart is disabled', async () => {
    const { element } = await render(
      allLeads({ enabled: false, validation: { valid: false, reasons: ['Stage is blank'] } }),
    );
    expect(element.querySelector('eia-result-chart')).toBeNull();
    const cards = [...element.querySelectorAll('[data-testid="kpi-answer"] eia-kpi-card')];
    // Counts and rates only; "Leads by stage" counts categories and is not a headline.
    expect(cards.map((c) => c.querySelector('.kpi__label')!.textContent.trim())).toEqual([
      'Total leads',
      'Leads created in the last 30 days',
    ]);
  });

  it('keeps a long analysis short until "Show all"', () => {
    const long = allLeads();
    const insights = Array.from({ length: 9 }, (_, i) => ({
      text: `Point ${i + 1}.`,
      metrics: [],
      kind: 'fact' as const,
    }));
    const response = { ...long, analysis: { ...long.analysis!, insights } };
    TestBed.configureTestingModule({ imports: [ResultViewComponent] });
    const fixture = TestBed.createComponent(ResultViewComponent);
    fixture.componentRef.setInput('response', response);
    fixture.detectChanges();
    const element = fixture.nativeElement as HTMLElement;
    const points = () => element.querySelectorAll('.takeaway__points li').length;
    expect(points()).toBe(6);
    const more = element.querySelector<HTMLButtonElement>('[data-testid="show-all"]')!;
    expect(more.textContent).toContain('Show all (3 more)');
    more.click();
    fixture.detectChanges();
    expect(points()).toBe(9);
    expect(element.querySelector('[data-testid="show-all"]')).toBeNull();
  });

  it('answers a company with no leads without a chart', async () => {
    const { element, summary } = await render(companyWithoutLeads());
    expect(element.querySelector('eia-result-chart')).toBeNull();
    expect(element.querySelector('[data-testid="kpi-answer"]')!.textContent).toContain(
      'Total leads',
    );
    expect(summary!.textContent).toContain('Lead Analysis — Pending Metals Co');
    expect(summary!.textContent).toContain('Pending Metals Co has no leads.');
  });
});
