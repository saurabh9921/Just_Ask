import { ChangeDetectionStrategy, Component, OnInit, computed, inject } from '@angular/core';
import { MatTooltipModule } from '@angular/material/tooltip';

import { ConnectionStatusService, type ConnectionState } from './connection-status.service';

const LABELS: Record<ConnectionState, string> = {
  ok: 'Online',
  degraded: 'Degraded',
  offline: 'Offline',
  unknown: 'Checking',
};

/** Compact API connection indicator for the top bar. Click to re-check. */
@Component({
  selector: 'eia-connection-status-indicator',
  imports: [MatTooltipModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <button
      type="button"
      class="status"
      [attr.data-state]="status.state()"
      [matTooltip]="tooltip()"
      [attr.aria-label]="'API connection: ' + label() + '. Activate to check again.'"
      [attr.aria-busy]="status.checking()"
      (click)="status.refresh()"
    >
      <span class="status__dot" aria-hidden="true"></span>
      <span class="status__label">{{ label() }}</span>
    </button>
    <span class="visually-hidden" aria-live="polite" data-testid="status-live">
      API connection {{ label() }}
    </span>
  `,
  styles: `
    :host {
      display: inline-flex;
    }
    .status {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 4px 10px;
      border-radius: 999px;
      border: 1px solid var(--mat-sys-outline-variant);
      background: var(--mat-sys-surface-container);
      color: var(--mat-sys-on-surface);
      font: var(--mat-sys-label-medium);
      cursor: pointer;
    }
    .status:hover {
      background: var(--mat-sys-surface-container-high);
    }
    .status__dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: var(--eia-status-unknown);
    }
    .status[data-state='ok'] .status__dot {
      background: var(--eia-status-ok);
    }
    .status[data-state='degraded'] .status__dot {
      background: var(--eia-status-degraded);
    }
    .status[data-state='offline'] .status__dot {
      background: var(--eia-status-offline);
    }
  `,
})
export class ConnectionStatusIndicatorComponent implements OnInit {
  protected readonly status = inject(ConnectionStatusService);

  protected readonly label = computed(() => LABELS[this.status.state()]);

  protected readonly tooltip = computed(() => {
    const result = this.status.result();
    if (result === null) {
      return 'Checking API connection...';
    }
    const parts = [`API ${LABELS[result.status].toLowerCase()}`];
    if (result.latencyMs !== null) {
      parts.push(`${result.latencyMs} ms`);
    }
    if (result.detail !== undefined) {
      parts.push(result.detail);
    }
    return parts.join(' · ');
  });

  ngOnInit(): void {
    this.status.start();
  }
}
