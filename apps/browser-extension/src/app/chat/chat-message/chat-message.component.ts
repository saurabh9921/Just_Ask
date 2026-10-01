import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  output,
  signal,
} from '@angular/core';
import { MatButtonModule } from '@angular/material/button';

import type { FeedbackInput } from '../../core/messaging/analytics-types';
import { describeBridgeError } from '../../core/messaging/background-bridge.service';
import { ErrorStateComponent } from '../../shared/error-state/error-state.component';
import { IconComponent } from '../../shared/icon/icon.component';
import { type ChatTurn, ChatStore } from '../chat.store';
import { FeedbackComponent } from '../feedback/feedback.component';
import { QueryPlanViewerComponent } from '../query-plan-viewer/query-plan-viewer.component';
import { QueryProgressComponent } from '../query-progress/query-progress.component';
import { ResultViewComponent } from '../result-view/result-view.component';

const REFUSALS: Readonly<Record<string, string>> = {
  FORBIDDEN_FIELD:
    'This question asks for restricted data (for example bank, card or tax identifiers). ERP Insight AI never returns it.',
  SENSITIVE_FIELD:
    'This question involves personal contact data, which is not available for analysis.',
  WRITE_NOT_ALLOWED: 'ERP Insight AI is read-only: it can analyse data but never change it.',
  OUT_OF_SCOPE: 'This question is outside the governed data available for this data source.',
};

const DEFAULT_REFUSAL =
  'This question is outside the governed data available for this data source.';

const RETRYABLE = new Set([
  'NETWORK_ERROR',
  'TIMEOUT',
  'RATE_LIMITED',
  'AGENT_LIMIT_EXCEEDED',
  'QUERY_TIMEOUT',
  'LLM_UNAVAILABLE',
  'DEPENDENCY_UNAVAILABLE',
  'CONNECTOR_UNAVAILABLE',
  'QUERY_CANCELLED',
  'INTERNAL_ERROR',
  'API_ERROR',
]);

/** One question and its answer (progress, result, clarification, refusal or error). */
@Component({
  selector: 'eia-chat-message',
  imports: [
    MatButtonModule,
    IconComponent,
    ErrorStateComponent,
    FeedbackComponent,
    QueryPlanViewerComponent,
    QueryProgressComponent,
    ResultViewComponent,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @let t = turn();
    <article class="turn" [attr.aria-label]="'Question: ' + t.question">
      <div class="question">
        @if (t.parentQuestion) {
          <p class="question__context">
            <eia-icon name="reply" [size]="14" /> Follow-up to “{{ t.parentQuestion }}”
          </p>
        }
        <p class="question__text">{{ t.question }}</p>
        @if (t.mode === 'plan') {
          <span class="badge">Plan only</span>
        }
      </div>

      <div class="answer" [attr.aria-busy]="t.phase === 'running'">
        @if (t.phase === 'running') {
          <eia-query-progress
            [steps]="t.steps"
            [elapsedMs]="t.elapsedMs"
            [cancelling]="t.cancelling"
            [transport]="t.transport"
            (cancelQuery)="store.cancel(t)"
          />
        } @else if (t.phase === 'failed') {
          <eia-error-state
            [title]="errorTitle()"
            [message]="t.error?.message ?? 'The question could not be answered.'"
            [retryable]="retryable()"
            retryLabel="Try again"
            (retry)="retry.emit(t)"
          />
          @if (t.response?.plan && t.error?.code !== 'QUERY_CANCELLED') {
            <details class="why">
              <summary>What was attempted</summary>
              <eia-query-plan-viewer [response]="t.response!" />
            </details>
          }
        } @else if (t.response; as r) {
          @switch (r.status) {
            @case ('clarification') {
              <div class="notice" role="status">
                <p class="notice__title">A little more detail is needed</p>
                <p>{{ r.clarificationQuestion }}</p>
                <button mat-stroked-button type="button" (click)="followUp.emit(t)">
                  <eia-icon name="reply" [size]="16" /> Answer
                </button>
              </div>
            }
            @case ('refused') {
              <div class="notice notice--refused" role="status">
                <p class="notice__title"><eia-icon name="shield" [size]="16" /> Not available</p>
                <p>{{ refusal() }}</p>
              </div>
            }
            @case ('planned') {
              <p class="plan-note">
                Plan only: the query was validated but not run.
                <button mat-button type="button" (click)="runPlan.emit(t)">Run it</button>
              </p>
              <eia-result-view [response]="r" />
            }
            @default {
              <eia-result-view [response]="r" [canExport]="store.canExport()" />
            }
          }

          <div class="actions">
            @if (r.status === 'succeeded') {
              <button mat-button type="button" (click)="followUp.emit(t)">
                <eia-icon name="reply" [size]="16" /> Follow up
              </button>
            }
            <button
              mat-button
              type="button"
              [attr.aria-pressed]="t.saved"
              [disabled]="saving()"
              (click)="toggleSave()"
            >
              <eia-icon [name]="t.saved ? 'bookmark' : 'bookmarkOutline'" [size]="16" />
              {{ t.saved ? 'Saved' : 'Save question' }}
            </button>
            @if (saveError(); as message) {
              <span class="action-error" role="alert">{{ message }}</span>
            }
          </div>
          <eia-feedback [queryId]="r.queryId" [initialRating]="t.feedback" [send]="sendFeedback" />
        }
      </div>
    </article>
  `,
  styles: `
    .turn {
      display: grid;
      gap: 8px;
    }
    .question {
      justify-self: end;
      max-width: 92%;
      padding: 8px 12px;
      border-radius: 14px 14px 4px 14px;
      background: var(--mat-sys-primary-container);
      color: var(--mat-sys-on-primary-container);
    }
    .question__context {
      margin: 0 0 2px;
      font: var(--mat-sys-label-small);
      display: flex;
      align-items: center;
      gap: 4px;
      opacity: 0.8;
    }
    .question__text {
      margin: 0;
      font: var(--mat-sys-body-medium);
      white-space: pre-wrap;
    }
    .badge {
      display: inline-block;
      margin-top: 4px;
      font: var(--mat-sys-label-small);
      padding: 0 6px;
      border-radius: 999px;
      background: var(--mat-sys-surface);
      color: var(--mat-sys-on-surface-variant);
    }
    .answer {
      padding: 12px;
      border-radius: 14px;
      border: 1px solid var(--mat-sys-outline-variant);
      background: var(--mat-sys-surface-container-lowest);
    }
    .notice p {
      margin: 0 0 8px;
    }
    .notice__title {
      font: var(--mat-sys-title-small);
      display: flex;
      align-items: center;
      gap: 6px;
    }
    .notice--refused .notice__title {
      color: var(--mat-sys-tertiary);
    }
    .plan-note {
      margin: 0 0 8px;
      font: var(--mat-sys-body-small);
      color: var(--mat-sys-on-surface-variant);
    }
    .why {
      margin-top: 8px;
    }
    .why summary {
      cursor: pointer;
      font: var(--mat-sys-label-large);
    }
    .actions {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 4px;
      margin-top: 8px;
      border-top: 1px solid var(--mat-sys-outline-variant);
      padding-top: 6px;
    }
    .action-error {
      font: var(--mat-sys-body-small);
      color: var(--eia-status-offline);
    }
  `,
})
export class ChatMessageComponent {
  protected readonly store = inject(ChatStore);

  readonly turn = input.required<ChatTurn>();
  readonly followUp = output<ChatTurn>();
  readonly retry = output<ChatTurn>();
  readonly runPlan = output<ChatTurn>();

  protected readonly saving = signal(false);
  protected readonly saveError = signal<string | null>(null);

  protected readonly refusal = computed(
    () => REFUSALS[this.turn().response?.refusalReason ?? ''] ?? DEFAULT_REFUSAL,
  );
  protected readonly retryable = computed(() => RETRYABLE.has(this.turn().error?.code ?? ''));
  protected readonly errorTitle = computed(() => {
    switch (this.turn().error?.code) {
      case 'QUERY_CANCELLED':
        return 'Cancelled';
      case 'QUERY_NOT_ALLOWED':
        return 'Not permitted by data policy';
      case 'PLAN_INVALID':
        return 'The question could not be mapped to the data';
      case 'AGENT_LIMIT_EXCEEDED':
      case 'QUERY_TIMEOUT':
        return 'That took too long';
      case 'RATE_LIMITED':
        return 'Too many questions';
      case 'NETWORK_ERROR':
        return 'Connection problem';
      default:
        return 'Something went wrong';
    }
  });

  protected readonly sendFeedback = (input: FeedbackInput) =>
    this.store.submitFeedback(this.turn(), input);

  protected toggleSave(): void {
    const turn = this.turn();
    this.saving.set(true);
    this.saveError.set(null);
    this.store.setSaved(turn, !turn.saved).subscribe({
      next: () => this.saving.set(false),
      error: (error: unknown) => {
        this.saving.set(false);
        this.saveError.set(describeBridgeError(error));
      },
    });
  }
}
