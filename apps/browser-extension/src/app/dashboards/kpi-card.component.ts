import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import type { KpiCard } from '../core/messaging/analytics-types';
import { formatPercentChange, formatValue } from '../shared/format/value-format';

/** One headline number, with the change against the previous period when compared. */
@Component({
  selector: 'eia-kpi-card',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <article class="kpi" [attr.aria-label]="accessibleLabel()">
      <p class="kpi__label">
        {{ kpi().label }}
        @if (kpi().scope === 'total') {
          <span class="kpi__scope">Total</span>
        }
      </p>
      <p class="kpi__value" [title]="exact()">{{ value() }}</p>
      @if (kpi().changePct !== null) {
        <p class="kpi__change" [class.up]="trend() === 'up'" [class.down]="trend() === 'down'">
          <span aria-hidden="true">{{
            trend() === 'up' ? '▲' : trend() === 'down' ? '▼' : '■'
          }}</span>
          {{ change() }}
          <span class="kpi__previous">vs {{ previous() }}</span>
        </p>
      }
    </article>
  `,
  styles: `
    .kpi {
      padding: 12px 14px;
      border-radius: 12px;
      background: var(--mat-sys-surface-container);
      border: 1px solid var(--mat-sys-outline-variant);
      min-width: 0;
    }
    .kpi__label {
      margin: 0;
      font: var(--mat-sys-label-medium);
      color: var(--mat-sys-on-surface-variant);
      display: flex;
      gap: 6px;
      align-items: center;
    }
    .kpi__scope {
      font: var(--mat-sys-label-small);
      padding: 0 6px;
      border-radius: 999px;
      background: var(--mat-sys-secondary-container);
      color: var(--mat-sys-on-secondary-container);
    }
    .kpi__value {
      margin: 4px 0 0;
      font: var(--mat-sys-headline-small);
      font-variant-numeric: tabular-nums;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .kpi__change {
      margin: 2px 0 0;
      font: var(--mat-sys-label-medium);
      color: var(--mat-sys-on-surface-variant);
    }
    .kpi__change.up {
      color: var(--eia-status-ok);
    }
    .kpi__change.down {
      color: var(--eia-status-offline);
    }
    .kpi__previous {
      color: var(--mat-sys-on-surface-variant);
      margin-left: 4px;
    }
  `,
})
export class KpiCardComponent {
  readonly kpi = input.required<KpiCard>();

  protected readonly value = computed(() =>
    formatValue(this.kpi().value, this.kpi().format, { compact: true }),
  );
  protected readonly exact = computed(() => formatValue(this.kpi().value, this.kpi().format));
  protected readonly previous = computed(() =>
    formatValue(this.kpi().previous, this.kpi().format, { compact: true }),
  );
  protected readonly change = computed(() => formatPercentChange(this.kpi().changePct));
  protected readonly trend = computed(() => {
    const pct = this.kpi().changePct;
    return pct === null || pct === 0 ? 'flat' : pct > 0 ? 'up' : 'down';
  });
  protected readonly accessibleLabel = computed(() => {
    const k = this.kpi();
    const base = `${k.label}: ${this.exact()}`;
    if (k.changePct === null) {
      return base;
    }
    const direction = this.trend() === 'up' ? 'up' : this.trend() === 'down' ? 'down' : 'unchanged';
    return `${base}, ${direction} ${this.change()} versus ${formatValue(k.previous, k.format)}`;
  });
}
