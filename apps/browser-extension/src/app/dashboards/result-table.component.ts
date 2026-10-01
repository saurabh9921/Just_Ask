import { ChangeDetectionStrategy, Component, computed, input, signal } from '@angular/core';
import { MatButtonModule } from '@angular/material/button';

import type { FormattedResult, ResultColumn, Scalar } from '../core/messaging/analytics-types';
import { formatValue } from '../shared/format/value-format';

type Direction = 'ascending' | 'descending';

const NUMERIC_KINDS = new Set(['measure', 'previous', 'change', 'change_pct']);

function compare(a: Scalar, b: Scalar): number {
  if (a === b) {
    return 0;
  }
  if (a === null) {
    return 1; // nulls last
  }
  if (b === null) {
    return -1;
  }
  if (typeof a === 'number' && typeof b === 'number') {
    return a - b;
  }
  return String(a).localeCompare(String(b), undefined, { numeric: true, sensitivity: 'base' });
}

/** Accessible, sortable, paginated result table (client-side over the returned rows). */
@Component({
  selector: 'eia-result-table',
  imports: [MatButtonModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div
      class="scroller"
      tabindex="0"
      role="region"
      [attr.aria-label]="'Result table, ' + summary()"
    >
      <table class="table">
        <caption class="visually-hidden">
          {{
            summary()
          }}
        </caption>
        <thead>
          <tr>
            @for (column of result().columns; track column.name; let i = $index) {
              <th
                scope="col"
                [class.numeric]="isNumeric(column)"
                [attr.aria-sort]="sortColumn() === i ? sortDirection() : null"
              >
                <button type="button" class="sort" (click)="sortBy(i)">
                  {{ column.label }}
                  <span aria-hidden="true" class="sort__mark">{{
                    sortColumn() === i ? (sortDirection() === 'ascending' ? '▲' : '▼') : ''
                  }}</span>
                </button>
              </th>
            }
          </tr>
        </thead>
        <tbody>
          @for (row of pageRows(); track $index) {
            <tr>
              @for (cell of row; track $index; let i = $index) {
                <td [class.numeric]="isNumeric(result().columns[i]!)">
                  {{ format(cell, result().columns[i]!) }}
                </td>
              }
            </tr>
          } @empty {
            <tr>
              <td class="empty" [attr.colspan]="result().columns.length || 1">
                No rows match this question for the selected scope.
              </td>
            </tr>
          }
        </tbody>
      </table>
    </div>
    <div class="footer">
      <span class="count" data-testid="row-summary">{{ summary() }}</span>
      @if (pageCount() > 1) {
        <nav class="pager" aria-label="Table pages">
          <button mat-button type="button" [disabled]="page() === 0" (click)="go(-1)">
            Previous
          </button>
          <span aria-live="polite">Page {{ page() + 1 }} of {{ pageCount() }}</span>
          <button mat-button type="button" [disabled]="page() >= pageCount() - 1" (click)="go(1)">
            Next
          </button>
        </nav>
      }
    </div>
    @if (result().truncated) {
      <p class="note" role="note">
        The result was capped at {{ result().rowCount }} rows. Narrow the question (period, filters
        or top N) to see everything.
      </p>
    }
    @if (result().redactedColumns.length > 0) {
      <p class="note" role="note">
        Hidden by data policy: {{ result().redactedColumns.join(', ') }}.
      </p>
    }
  `,
  styles: `
    .scroller {
      overflow: auto;
      max-height: 420px;
      border: 1px solid var(--mat-sys-outline-variant);
      border-radius: 10px;
    }
    .table {
      width: 100%;
      border-collapse: collapse;
      font: var(--mat-sys-body-small);
      font-variant-numeric: tabular-nums;
    }
    th {
      position: sticky;
      top: 0;
      background: var(--mat-sys-surface-container-high);
      text-align: left;
      padding: 0;
      white-space: nowrap;
    }
    td {
      padding: 6px 10px;
      border-top: 1px solid var(--mat-sys-outline-variant);
      white-space: nowrap;
    }
    tbody tr:nth-child(even) td {
      background: var(--mat-sys-surface-container-lowest);
    }
    .numeric {
      text-align: right;
    }
    .sort {
      all: unset;
      box-sizing: border-box;
      display: flex;
      gap: 4px;
      width: 100%;
      padding: 8px 10px;
      cursor: pointer;
      font: var(--mat-sys-label-medium);
    }
    th.numeric .sort {
      justify-content: flex-end;
    }
    .sort:focus-visible {
      outline: 2px solid var(--mat-sys-primary);
      outline-offset: -2px;
    }
    .sort__mark {
      font-size: 10px;
      min-width: 10px;
    }
    .empty {
      text-align: center;
      color: var(--mat-sys-on-surface-variant);
      padding: 16px;
    }
    .footer {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 8px;
      margin-top: 4px;
      font: var(--mat-sys-body-small);
      color: var(--mat-sys-on-surface-variant);
    }
    .pager {
      display: flex;
      align-items: center;
      gap: 4px;
    }
    .note {
      margin: 6px 0 0;
      font: var(--mat-sys-body-small);
      color: var(--mat-sys-on-surface-variant);
    }
  `,
})
export class ResultTableComponent {
  readonly result = input.required<FormattedResult>();
  readonly pageSize = input(25);

  protected readonly sortColumn = signal<number | null>(null);
  protected readonly sortDirection = signal<Direction>('ascending');
  protected readonly page = signal(0);

  protected readonly sortedRows = computed(() => {
    const rows = this.result().rows;
    const column = this.sortColumn();
    if (column === null) {
      return rows;
    }
    const factor = this.sortDirection() === 'ascending' ? 1 : -1;
    return [...rows].sort((a, b) => factor * compare(a[column] ?? null, b[column] ?? null));
  });
  protected readonly pageCount = computed(() =>
    Math.max(1, Math.ceil(this.sortedRows().length / this.pageSize())),
  );
  protected readonly pageRows = computed(() => {
    const start = Math.min(this.page(), this.pageCount() - 1) * this.pageSize();
    return this.sortedRows().slice(start, start + this.pageSize());
  });
  protected readonly summary = computed(() => {
    const n = this.result().rows.length;
    return `${n} ${n === 1 ? 'row' : 'rows'}`;
  });

  protected isNumeric(column: ResultColumn): boolean {
    return NUMERIC_KINDS.has(column.kind);
  }

  protected format(value: Scalar, column: ResultColumn): string {
    return formatValue(value, column.kind === 'change_pct' ? 'percent' : column.format);
  }

  protected sortBy(index: number): void {
    if (this.sortColumn() === index) {
      this.sortDirection.update((d) => (d === 'ascending' ? 'descending' : 'ascending'));
    } else {
      this.sortColumn.set(index);
      const column = this.result().columns[index];
      this.sortDirection.set(
        column !== undefined && this.isNumeric(column) ? 'descending' : 'ascending',
      );
    }
    this.page.set(0);
  }

  protected go(delta: number): void {
    this.page.update((p) => Math.max(0, Math.min(this.pageCount() - 1, p + delta)));
  }
}
