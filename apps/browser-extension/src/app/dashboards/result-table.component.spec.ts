import { TestBed } from '@angular/core/testing';

import type { FormattedResult } from '../core/messaging/analytics-types';
import { queryResponse } from '../../testing/analytics-fixtures';
import { KpiCardComponent } from './kpi-card.component';
import { ResultTableComponent } from './result-table.component';

function render(result: FormattedResult, pageSize = 25) {
  const fixture = TestBed.createComponent(ResultTableComponent);
  fixture.componentRef.setInput('result', result);
  fixture.componentRef.setInput('pageSize', pageSize);
  fixture.detectChanges();
  const element = fixture.nativeElement as HTMLElement;
  const column = (i: number) =>
    Array.from(element.querySelectorAll('tbody tr')).map((tr) =>
      tr.children[i]?.textContent.trim(),
    );
  const click = (label: string) => {
    Array.from(element.querySelectorAll('button'))
      .find((b) => b.textContent.includes(label))!
      .click();
    fixture.detectChanges();
  };
  return { element, column, click };
}

describe('ResultTableComponent', () => {
  it('renders labelled columns with formatted, right-aligned numbers', () => {
    const { element, column } = render(queryResponse().result!);
    expect(Array.from(element.querySelectorAll('th')).map((th) => th.textContent.trim())).toEqual([
      'Region',
      'Total sales',
    ]);
    expect(column(0)).toEqual(['South', 'North', 'East', 'West']);
    expect(element.querySelector('td.numeric')).not.toBeNull();
    expect(element.querySelector('[data-testid="row-summary"]')?.textContent).toContain('4 rows');
  });

  it('sorts by a column and announces the direction', () => {
    const { element, column, click } = render(queryResponse().result!);
    click('Region');
    expect(column(0)).toEqual(['East', 'North', 'South', 'West']);
    expect(element.querySelector('th')?.getAttribute('aria-sort')).toBe('ascending');
    click('Region');
    expect(column(0)).toEqual(['West', 'South', 'North', 'East']);
    click('Total sales'); // numeric columns start descending
    expect(column(0)).toEqual(['South', 'North', 'East', 'West']);
  });

  it('paginates', () => {
    const { element, column, click } = render(queryResponse().result!, 3);
    expect(column(0)).toHaveLength(3);
    expect(element.textContent).toContain('Page 1 of 2');
    click('Next');
    expect(column(0)).toEqual(['West']);
  });

  it('explains truncated results', () => {
    const { element } = render({ ...queryResponse().result!, truncated: true });
    expect(element.textContent).toContain('capped at 4 rows');
  });
});

describe('KpiCardComponent', () => {
  it('shows value, change and an accessible summary', () => {
    const fixture = TestBed.createComponent(KpiCardComponent);
    fixture.componentRef.setInput('kpi', {
      name: 'total_sales',
      label: 'Total sales',
      value: 469301.61,
      format: 'currency',
      previous: 784242.23,
      change: -314940.62,
      changePct: -40.16,
      scope: 'value',
    });
    fixture.detectChanges();
    const element = fixture.nativeElement as HTMLElement;
    expect(element.querySelector('.kpi__change')?.classList).toContain('down');
    expect(element.querySelector('article')?.getAttribute('aria-label')).toMatch(
      /Total sales: .*down -40.16% versus/,
    );
  });
});
