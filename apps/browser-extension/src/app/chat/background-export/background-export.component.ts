import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  InjectionToken,
  inject,
  input,
  signal,
} from '@angular/core';
import { MatButtonModule } from '@angular/material/button';
import { Subscription, filter, switchMap, take, takeWhile, timer } from 'rxjs';

import {
  EXPORT_TERMINAL,
  type ExportFileFormat,
  type ExportJob,
} from '../../core/messaging/analytics-types';
import {
  BackgroundBridgeService,
  describeBridgeError,
} from '../../core/messaging/background-bridge.service';
import { IconComponent } from '../../shared/icon/icon.component';

/** How often a queued export is polled, and for how long (the API times jobs out). */
export const EXPORT_POLL_MS = new InjectionToken<number>('EXPORT_POLL_MS', {
  providedIn: 'root',
  factory: () => 2000,
});
const MAX_POLLS = 400;

type State =
  | { readonly kind: 'idle' }
  | { readonly kind: 'confirm'; readonly format: ExportFileFormat }
  | { readonly kind: 'working'; readonly job: ExportJob | null }
  | { readonly kind: 'done'; readonly job: ExportJob; readonly fileName: string }
  | { readonly kind: 'error'; readonly message: string };

const FAILURES: Readonly<Record<string, string>> = {
  SENSITIVE_DATA_DETECTED:
    'The export was stopped because the data looks like it contains card or bank numbers.',
  EXPORT_TIMEOUT: 'The export took too long. Narrow the question and try again.',
  PERMISSION_DENIED: 'You are no longer allowed to export this data.',
  QUERY_NOT_EXPORTABLE: 'This answer can no longer be exported.',
};

/**
 * "Export all rows": the same validated query re-run by the server without the
 * interactive row cap (up to the platform export limit), written to a temporary file
 * and downloaded through a one-time link. Requires an explicit confirmation.
 */
@Component({
  selector: 'eia-background-export',
  imports: [MatButtonModule, IconComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @let s = state();
    <div class="bg-export" role="group" aria-label="Export all rows">
      @switch (s.kind) {
        @case ('idle') {
          <button mat-button type="button" (click)="state.set({ kind: 'confirm', format: 'csv' })">
            <eia-icon name="download" [size]="16" /> All rows…
          </button>
        }
        @case ('confirm') {
          <span class="text" role="alert">
            Export <strong>every matching row</strong> (not only the rows shown) as
            {{ s.format.toUpperCase() }}? The file is prepared on the server, kept for a short time,
            and saved to this computer.
          </span>
          <label class="format">
            Format
            <select
              [value]="s.format"
              (change)="state.set({ kind: 'confirm', format: asFormat($event) })"
            >
              <option value="csv">CSV</option>
              <option value="json">JSON</option>
            </select>
          </label>
          <button mat-flat-button type="button" (click)="start(s.format)">Export all rows</button>
          <button mat-button type="button" (click)="reset()">Cancel</button>
        }
        @case ('working') {
          <span class="text" role="status" aria-live="polite">
            Preparing export{{ s.job?.status === 'running' ? ' (running)' : '' }}…
          </span>
          <button mat-button type="button" (click)="reset()">Hide</button>
        }
        @case ('done') {
          <span class="text" role="status">
            Downloading {{ s.fileName }} ({{ s.job.rowCount ?? 0 }} rows).
            @if (s.job.truncated) {
              The platform export limit was reached; the file does not contain every row.
            }
          </span>
          <button mat-button type="button" (click)="reset()">Done</button>
        }
        @case ('error') {
          <span class="text error" role="alert">{{ s.message }}</span>
          <button mat-button type="button" (click)="reset()">Dismiss</button>
        }
      }
    </div>
  `,
  styles: `
    .bg-export {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 4px;
    }
    .text {
      font: var(--mat-sys-body-small);
      flex-basis: 100%;
    }
    .error {
      color: var(--mat-sys-error);
    }
    .format {
      font: var(--mat-sys-body-small);
      display: inline-flex;
      gap: 4px;
      align-items: center;
    }
  `,
})
export class BackgroundExportComponent {
  readonly queryId = input.required<string>();

  private readonly bridge = inject(BackgroundBridgeService);
  private readonly pollMs = inject(EXPORT_POLL_MS);
  private running: Subscription | null = null;
  /** The job reached a terminal state (the poll stream completing is then expected). */
  private settled = false;

  protected readonly state = signal<State>({ kind: 'idle' });

  constructor() {
    inject(DestroyRef).onDestroy(() => this.running?.unsubscribe());
  }

  protected asFormat(event: Event): ExportFileFormat {
    return (event.target as HTMLSelectElement).value === 'json' ? 'json' : 'csv';
  }

  protected reset(): void {
    this.running?.unsubscribe();
    this.running = null;
    this.state.set({ kind: 'idle' });
  }

  protected start(format: ExportFileFormat): void {
    this.running?.unsubscribe();
    this.settled = false;
    this.state.set({ kind: 'working', job: null });
    this.running = this.bridge
      .request<'exports.create'>({
        type: 'exports.create',
        payload: { queryId: this.queryId(), format, scope: 'all_rows' },
      })
      .pipe(
        switchMap((created) =>
          timer(0, this.pollMs).pipe(
            take(MAX_POLLS),
            switchMap(() =>
              this.bridge.request<'exports.get'>({
                type: 'exports.get',
                payload: { exportId: created.exportId },
              }),
            ),
            takeWhile((job) => !EXPORT_TERMINAL.includes(job.status), true),
          ),
        ),
      )
      .pipe(filter((job) => this.progress(job)))
      .subscribe({
        next: (job) => this.download(job),
        error: (error: unknown) => this.fail(describeBridgeError(error)),
        complete: () => {
          if (!this.settled) {
            this.fail('The export is taking longer than expected. Try again later.');
          }
        },
      });
  }

  /** Updates the progress state; returns true once the job reached a terminal state. */
  private progress(job: ExportJob): boolean {
    if (!EXPORT_TERMINAL.includes(job.status)) {
      this.state.set({ kind: 'working', job });
      return false;
    }
    this.settled = true;
    if (job.status !== 'succeeded') {
      this.fail(
        FAILURES[job.errorCode ?? ''] ??
          (job.status === 'expired'
            ? 'The export file has expired.'
            : 'The export could not be completed.'),
      );
      return false;
    }
    return true;
  }

  private download(job: ExportJob): void {
    this.bridge
      .request<'exports.download'>({
        type: 'exports.download',
        payload: { exportId: job.exportId },
      })
      .subscribe({
        next: ({ fileName }) => this.state.set({ kind: 'done', job, fileName }),
        error: (error: unknown) => this.fail(describeBridgeError(error)),
      });
  }

  private fail(message: string): void {
    this.state.set({ kind: 'error', message });
  }
}
