import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';
import { MatButtonModule } from '@angular/material/button';
import { MatProgressBarModule } from '@angular/material/progress-bar';

import { IconComponent } from '../../shared/icon/icon.component';
import { formatDuration } from '../../shared/format/value-format';
import type { StepProgress } from '../chat.store';

/** Execution states of the analytics agent, in order (mirrors the API's AgentStep). */
export const AGENT_STEPS = [
  'RECEIVE_QUESTION',
  'CLASSIFY_INTENT',
  'RESOLVE_DATA_SOURCE',
  'RETRIEVE_SEMANTIC_CONTEXT',
  'RETRIEVE_SIMILAR_EXAMPLES',
  'BUILD_QUERY_PLAN',
  'VALIDATE_QUERY_PLAN',
  'REQUEST_CLARIFICATION_IF_NEEDED',
  'COMPILE_DIALECT_SQL',
  'VALIDATE_SQL',
  'ESTIMATE_COST',
  'EXECUTE_READONLY_QUERY',
  'FORMAT_RESULT',
  'GENERATE_EXPLANATION',
  'STORE_AUDIT_EVENT',
] as const;

/** Live progress of one question with a cancel control. */
@Component({
  selector: 'eia-query-progress',
  imports: [MatButtonModule, MatProgressBarModule, IconComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <section class="progress" aria-label="Query progress">
      <mat-progress-bar
        [mode]="steps().length === 0 ? 'indeterminate' : 'determinate'"
        [value]="percent()"
        aria-label="Progress"
      />
      <div class="row">
        <p class="current" aria-live="polite">{{ current() }}</p>
        <span class="elapsed">{{ elapsed() }}</span>
        <button
          mat-stroked-button
          type="button"
          class="cancel"
          [disabled]="cancelling()"
          (click)="cancelQuery.emit()"
        >
          <eia-icon name="stop" [size]="16" />
          {{ cancelling() ? 'Cancelling…' : 'Cancel' }}
        </button>
      </div>
      @if (steps().length > 0) {
        <ol class="steps">
          @for (s of steps(); track s.step) {
            <li [class]="s.status">
              <span class="dot" aria-hidden="true"></span>
              {{ s.message }}
              <span class="visually-hidden">{{ s.status }}</span>
            </li>
          }
        </ol>
      }
      @if (transport() === 'http') {
        <p class="note">Live progress is unavailable on this network; waiting for the answer.</p>
      }
    </section>
  `,
  styles: `
    .progress {
      padding: 10px 12px;
      border-radius: 12px;
      background: var(--mat-sys-surface-container-low);
    }
    .row {
      display: flex;
      align-items: center;
      gap: 8px;
      margin-top: 8px;
    }
    .current {
      flex: 1;
      margin: 0;
      font: var(--mat-sys-body-medium);
    }
    .elapsed {
      font: var(--mat-sys-label-small);
      color: var(--mat-sys-on-surface-variant);
      font-variant-numeric: tabular-nums;
    }
    .steps {
      list-style: none;
      margin: 8px 0 0;
      padding: 0;
      display: grid;
      gap: 2px;
      font: var(--mat-sys-body-small);
      color: var(--mat-sys-on-surface-variant);
    }
    .steps li {
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: var(--mat-sys-outline);
      flex: none;
    }
    .completed .dot {
      background: var(--eia-status-ok);
    }
    .started .dot {
      background: var(--mat-sys-primary);
      animation: pulse 1s ease-in-out infinite alternate;
    }
    .started {
      color: var(--mat-sys-on-surface);
    }
    .failed .dot {
      background: var(--eia-status-offline);
    }
    @keyframes pulse {
      to {
        opacity: 0.3;
      }
    }
    .note {
      margin: 6px 0 0;
      font: var(--mat-sys-body-small);
      color: var(--mat-sys-on-surface-variant);
    }
  `,
})
export class QueryProgressComponent {
  readonly steps = input.required<readonly StepProgress[]>();
  readonly elapsedMs = input(0);
  readonly cancelling = input(false);
  readonly transport = input<'websocket' | 'http' | null>(null);
  readonly cancelQuery = output();

  protected readonly current = computed(() => {
    const steps = this.steps();
    const active = [...steps].reverse().find((s) => s.status === 'started') ?? steps.at(-1);
    return active?.message ?? 'Sending your question…';
  });
  protected readonly percent = computed(() => {
    const done = new Set(
      this.steps()
        .filter((s) => s.status !== 'started')
        .map((s) => s.step),
    );
    const reached = AGENT_STEPS.filter((s) => done.has(s)).length;
    return Math.round((reached / AGENT_STEPS.length) * 100);
  });
  protected readonly elapsed = computed(() => formatDuration(this.elapsedMs()));
}
