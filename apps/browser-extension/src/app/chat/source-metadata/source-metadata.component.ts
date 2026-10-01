import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import type { QueryResponse } from '../../core/messaging/analytics-types';
import {
  formatDate,
  formatDateTime,
  formatDuration,
  formatRelative,
} from '../../shared/format/value-format';

/**
 * The provenance every answer must show: data source, freshness, applied date
 * filters, security scope, rows returned, execution time, validation/confidence and
 * the source entities used.
 */
@Component({
  selector: 'eia-source-metadata',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @let r = response();
    <dl class="meta" aria-label="Answer details">
      <div>
        <dt>Data source</dt>
        <dd>
          {{ r.connection.name ?? 'Unknown' }}
          @if (r.connection.connectorType) {
            <span class="sub">{{ r.connection.connectorType }}</span>
          }
        </dd>
      </div>
      <div>
        <dt>Data freshness</dt>
        <dd [title]="freshnessExact()">
          Semantic index {{ freshness() }}
          @if (r.semanticVersion !== null) {
            <span class="sub">v{{ r.semanticVersion }}</span>
          }
        </dd>
      </div>
      <div>
        <dt>Date filter</dt>
        <dd>{{ dateFilter() }}</dd>
      </div>
      <div>
        <dt>Security scope</dt>
        <dd>
          {{ r.securityScope.tenant }}
          @if (r.securityScope.rowFilterFields.length > 0) {
            <span class="sub"
              >rows filtered by {{ r.securityScope.rowFilterFields.join(', ') }}</span
            >
          }
          <span class="sub">{{ r.securityScope.readOnly ? 'read-only' : '' }}</span>
        </dd>
      </div>
      <div>
        <dt>Rows returned</dt>
        <dd>
          {{ r.rowsReturned ?? '—' }}
          @if (r.result?.truncated) {
            <span class="sub">capped</span>
          }
        </dd>
      </div>
      <div>
        <dt>Execution time</dt>
        <dd>
          {{ total() }}
          @if (r.cache.hit) {
            <span class="sub" data-testid="cache-hit">cached result{{ cacheAge() }}</span>
          } @else if (r.timings.executionMs !== null) {
            <span class="sub">database {{ database() }}</span>
          }
        </dd>
      </div>
      <div>
        <dt>Validation</dt>
        <dd>
          {{
            r.validation.sqlValidated
              ? 'Validated'
              : r.validation.planValid
                ? 'Plan valid'
                : 'Not validated'
          }}
          @if (r.validation.confidence !== null) {
            <span class="sub">confidence {{ confidence() }}</span>
          }
        </dd>
      </div>
      <div>
        <dt>Source entities</dt>
        <dd>{{ r.sourceEntities.length > 0 ? r.sourceEntities.join(', ') : '—' }}</dd>
      </div>
    </dl>
  `,
  styles: `
    .meta {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
      gap: 6px;
      margin: 0;
    }
    .meta div {
      padding: 6px 10px;
      border-radius: 8px;
      background: var(--mat-sys-surface-container-low);
      min-width: 0;
    }
    dt {
      font: var(--mat-sys-label-small);
      color: var(--mat-sys-on-surface-variant);
    }
    dd {
      margin: 0;
      font: var(--mat-sys-body-small);
      overflow-wrap: anywhere;
    }
    .sub {
      display: block;
      color: var(--mat-sys-on-surface-variant);
      font: var(--mat-sys-label-small);
    }
  `,
})
export class SourceMetadataComponent {
  readonly response = input.required<QueryResponse>();

  protected readonly freshness = computed(() =>
    formatRelative(this.response().dataFreshness).replace(/^in /, ''),
  );
  protected readonly freshnessExact = computed(() => formatDateTime(this.response().dataFreshness));
  protected readonly dateFilter = computed(() => {
    const { dateWindow, previousWindow } = this.response().appliedFilters;
    if (dateWindow === null) {
      return 'None';
    }
    const range = (w: typeof dateWindow) =>
      `${formatDate(w.start)} – ${formatDate(lastDay(w.endExclusive))}`;
    const current = `${dateWindow.field.replace(/_/g, ' ')}: ${range(dateWindow)}`;
    return previousWindow === null ? current : `${current} vs ${range(previousWindow)}`;
  });
  protected readonly total = computed(() => formatDuration(this.response().timings.totalMs));
  protected readonly database = computed(() => formatDuration(this.response().timings.executionMs));
  protected readonly cacheAge = computed(() => {
    const age = this.response().cache.ageSeconds;
    if (age === null) {
      return '';
    }
    return age < 60 ? `, ${Math.round(age)} s old` : `, ${Math.round(age / 60)} min old`;
  });
  protected readonly confidence = computed(() => {
    const c = this.response().validation.confidence;
    return c === null ? '' : `${Math.round(c * 100)}%`;
  });
}

/** The API's windows are half-open; users expect the inclusive last day. */
function lastDay(endExclusive: string): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(endExclusive);
  if (match === null) {
    return endExclusive;
  }
  const date = new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3]) - 1));
  return date.toISOString().slice(0, 10);
}
