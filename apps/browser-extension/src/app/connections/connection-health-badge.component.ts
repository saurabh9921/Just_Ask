import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import type { ConnectionHealth } from '../core/messaging/protocol';

const LABELS: Record<ConnectionHealth, string> = {
  ok: 'Connected',
  error: 'Connection error',
  untested: 'Not tested',
};

/** Colour + text status badge (colour is never the only signal). */
@Component({
  selector: 'eia-connection-health-badge',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `<span class="badge" [attr.data-status]="status()"
    ><span class="dot" aria-hidden="true"></span>{{ label() }}</span
  >`,
  styles: `
    .badge {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 2px 8px;
      border-radius: 999px;
      font: var(--mat-sys-label-small);
      background: var(--mat-sys-surface-container-high);
      color: var(--mat-sys-on-surface);
      white-space: nowrap;
    }
    .dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: var(--eia-status-unknown);
    }
    [data-status='ok'] .dot {
      background: var(--eia-status-ok);
    }
    [data-status='error'] .dot {
      background: var(--eia-status-offline);
    }
  `,
})
export class ConnectionHealthBadgeComponent {
  readonly status = input.required<ConnectionHealth>();
  protected readonly label = computed(() => LABELS[this.status()]);
}
