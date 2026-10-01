import { ChangeDetectionStrategy, Component, computed, input, signal } from '@angular/core';
import { MatButtonModule } from '@angular/material/button';

import type { QueryResponse } from '../../core/messaging/analytics-types';
import { IconComponent } from '../../shared/icon/icon.component';
import { humanize } from '../../shared/format/value-format';

/**
 * What was asked of the database: the plain-English plan, assumptions, filters, the
 * validated query plan and the compiled SQL (placeholders only - values stay bound).
 */
@Component({
  selector: 'eia-query-plan-viewer',
  imports: [MatButtonModule, IconComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @let r = response();
    @if (r.description) {
      <p class="description">{{ r.description }}</p>
    }
    @if (r.assumptions.length > 0) {
      <h4 class="heading">Assumptions</h4>
      <ul class="list">
        @for (a of r.assumptions; track a) {
          <li>{{ a }}</li>
        }
      </ul>
    }
    @if (r.appliedFilters.filters.length > 0) {
      <h4 class="heading">Filters</h4>
      <ul class="list">
        @for (f of r.appliedFilters.filters; track $index) {
          <li>
            <code>{{ f.field }}</code> {{ op(f.op) }} <strong>{{ value(f.value) }}</strong>
          </li>
        }
      </ul>
    }
    <h4 class="heading">Validation</h4>
    <ul class="checks">
      <li [class.ok]="r.validation.planValid">
        <eia-icon [name]="r.validation.planValid ? 'check' : 'error'" [size]="16" />
        Plan checked against the semantic layer and your permissions
      </li>
      <li [class.ok]="r.validation.sqlValidated">
        <eia-icon [name]="r.validation.sqlValidated ? 'check' : 'error'" [size]="16" />
        Generated SQL passed the read-only safety check
      </li>
      @if (r.validation.corrections > 0) {
        <li>The plan was corrected {{ r.validation.corrections }} time(s) before validation.</li>
      }
      @if (r.cost) {
        <li>
          Cost estimate: {{ r.cost.source }}
          @if (r.cost.estimatedRows !== null) {
            (~{{ r.cost.estimatedRows }} rows)
          }
        </li>
      }
    </ul>
    @if (r.sql) {
      <div class="sql-head">
        <h4 class="heading">SQL</h4>
        <button mat-button type="button" (click)="copy(r.sql)" [attr.aria-label]="'Copy SQL'">
          <eia-icon name="copy" [size]="16" /> {{ copied() ? 'Copied' : 'Copy' }}
        </button>
      </div>
      <pre class="code" tabindex="0" aria-label="Generated SQL">{{ r.sql }}</pre>
      @if (r.previousSql) {
        <h4 class="heading">SQL (previous period)</h4>
        <pre class="code" tabindex="0" aria-label="Generated SQL for the previous period">{{
          r.previousSql
        }}</pre>
      }
      <p class="hint">Values are sent as bound parameters (shown as placeholders).</p>
    }
    @if (planJson(); as json) {
      <details class="plan">
        <summary>Query plan (JSON)</summary>
        <pre class="code" tabindex="0">{{ json }}</pre>
      </details>
    }
  `,
  styles: `
    .description {
      margin: 0 0 8px;
      font: var(--mat-sys-body-medium);
    }
    .heading {
      margin: 12px 0 4px;
      font: var(--mat-sys-title-small);
    }
    .list,
    .checks {
      margin: 0;
      padding-left: 18px;
      font: var(--mat-sys-body-small);
    }
    .checks {
      list-style: none;
      padding-left: 0;
    }
    .checks li {
      display: flex;
      gap: 6px;
      align-items: center;
      color: var(--mat-sys-on-surface-variant);
    }
    .checks li.ok eia-icon {
      color: var(--eia-status-ok);
    }
    .sql-head {
      display: flex;
      justify-content: space-between;
      align-items: center;
    }
    .code {
      margin: 0;
      padding: 10px;
      border-radius: 8px;
      background: var(--mat-sys-surface-container-high);
      font:
        12px/1.45 ui-monospace,
        'Cascadia Code',
        Consolas,
        monospace;
      white-space: pre-wrap;
      word-break: break-word;
      max-height: 260px;
      overflow: auto;
    }
    .hint {
      margin: 4px 0 0;
      font: var(--mat-sys-body-small);
      color: var(--mat-sys-on-surface-variant);
    }
    .plan {
      margin-top: 12px;
    }
    .plan summary {
      cursor: pointer;
      font: var(--mat-sys-label-large);
    }
  `,
})
export class QueryPlanViewerComponent {
  readonly response = input.required<QueryResponse>();
  protected readonly copied = signal(false);
  protected readonly planJson = computed(() => {
    const plan = this.response().plan;
    return plan === null ? null : JSON.stringify(plan, null, 2);
  });

  protected op(op: string): string {
    return humanize(op).toLowerCase();
  }

  protected value(value: unknown): string {
    if (Array.isArray(value)) {
      return value.map((v) => (typeof v === 'string' ? v : JSON.stringify(v))).join(', ');
    }
    return typeof value === 'string' ? value : JSON.stringify(value ?? null);
  }

  protected copy(text: string): void {
    void navigator.clipboard
      .writeText(text)
      .then(() => {
        this.copied.set(true);
        setTimeout(() => this.copied.set(false), 2000);
      })
      .catch(() => undefined);
  }
}
