import { ChangeDetectionStrategy, Component, computed, input, output, signal } from '@angular/core';
import { MatButtonModule } from '@angular/material/button';
import type { Observable } from 'rxjs';

import type {
  FeedbackCategory,
  FeedbackInput,
  FeedbackResult,
} from '../../core/messaging/analytics-types';
import { describeBridgeError } from '../../core/messaging/background-bridge.service';
import { IconComponent } from '../../shared/icon/icon.component';

const POSITIVE: readonly [FeedbackCategory, string][] = [
  ['accurate', 'Accurate'],
  ['helpful', 'Helpful'],
  ['fast', 'Fast'],
];
const NEGATIVE: readonly [FeedbackCategory, string][] = [
  ['wrong_numbers', 'Wrong numbers'],
  ['wrong_data_source', 'Wrong data'],
  ['wrong_period', 'Wrong period'],
  ['wrong_filter', 'Wrong filter'],
  ['missing_data', 'Missing data'],
  ['confusing', 'Confusing'],
  ['slow', 'Too slow'],
  ['other', 'Other'],
];

export type SubmitFeedback = (input: FeedbackInput) => Observable<FeedbackResult>;

/**
 * Thumbs up/down with optional reasons and a comment. A confirmed answer becomes an
 * example for future questions on this data source (feedback memory, server side).
 */
@Component({
  selector: 'eia-feedback',
  imports: [MatButtonModule, IconComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <section class="feedback" aria-label="Rate this answer">
      <div class="row">
        <span class="label" id="fb-label-{{ queryId() }}">Was this answer right?</span>
        <button
          mat-icon-button
          type="button"
          aria-label="Yes, this answer is right"
          [attr.aria-pressed]="rating() === 1"
          [class.active]="rating() === 1"
          (click)="choose(1)"
        >
          <eia-icon name="thumbUp" />
        </button>
        <button
          mat-icon-button
          type="button"
          aria-label="No, this answer is wrong"
          [attr.aria-pressed]="rating() === -1"
          [class.active]="rating() === -1"
          (click)="choose(-1)"
        >
          <eia-icon name="thumbDown" />
        </button>
      </div>
      @if (open()) {
        <form class="details" (submit)="$event.preventDefault(); submit()">
          <fieldset class="chips">
            <legend class="visually-hidden">Reasons</legend>
            @for (c of options(); track c[0]) {
              <button
                type="button"
                class="chip"
                [attr.aria-pressed]="selected().includes(c[0])"
                (click)="toggle(c[0])"
              >
                {{ c[1] }}
              </button>
            }
          </fieldset>
          <label class="visually-hidden" [for]="'fb-comment-' + queryId()">Comment</label>
          <textarea
            class="comment"
            rows="2"
            maxlength="2000"
            [id]="'fb-comment-' + queryId()"
            placeholder="Optional comment (do not include personal data)"
            [value]="comment()"
            (input)="comment.set($any($event.target).value)"
          ></textarea>
          <div class="actions">
            <button mat-flat-button type="submit" [disabled]="state() === 'sending'">
              {{ state() === 'sending' ? 'Sending…' : 'Send feedback' }}
            </button>
            <button mat-button type="button" (click)="open.set(false)">Close</button>
          </div>
        </form>
      }
      @if (state() === 'sent') {
        <p class="status" role="status">
          Thanks for the feedback.
          @if (remembered()) {
            This answer will now help with similar questions.
          }
        </p>
      } @else if (error(); as message) {
        <p class="status error" role="alert">{{ message }}</p>
      }
    </section>
  `,
  styles: `
    .row {
      display: flex;
      align-items: center;
      gap: 2px;
    }
    .label {
      font: var(--mat-sys-body-small);
      color: var(--mat-sys-on-surface-variant);
      margin-right: 4px;
    }
    .active {
      color: var(--mat-sys-primary);
    }
    .details {
      margin-top: 4px;
      display: grid;
      gap: 6px;
    }
    .chips {
      border: 0;
      margin: 0;
      padding: 0;
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
    }
    .chip {
      font: var(--mat-sys-label-medium);
      padding: 4px 10px;
      border-radius: 999px;
      border: 1px solid var(--mat-sys-outline-variant);
      background: transparent;
      color: inherit;
      cursor: pointer;
    }
    .chip[aria-pressed='true'] {
      background: var(--mat-sys-secondary-container);
      color: var(--mat-sys-on-secondary-container);
      border-color: transparent;
    }
    .comment {
      font: var(--mat-sys-body-small);
      padding: 8px;
      border-radius: 8px;
      border: 1px solid var(--mat-sys-outline-variant);
      background: var(--mat-sys-surface);
      color: inherit;
      resize: vertical;
    }
    .actions {
      display: flex;
      gap: 6px;
    }
    .status {
      margin: 4px 0 0;
      font: var(--mat-sys-body-small);
      color: var(--mat-sys-on-surface-variant);
    }
    .status.error {
      color: var(--eia-status-offline);
    }
  `,
})
export class FeedbackComponent {
  readonly queryId = input.required<string>();
  readonly initialRating = input<-1 | 1 | null>(null);
  readonly send = input.required<SubmitFeedback>();
  readonly rated = output<-1 | 1>();

  protected readonly choice = signal<-1 | 1 | null>(null);
  protected readonly rating = computed(() => this.choice() ?? this.initialRating());
  protected readonly open = signal(false);
  protected readonly selected = signal<readonly FeedbackCategory[]>([]);
  protected readonly comment = signal('');
  protected readonly state = signal<'idle' | 'sending' | 'sent' | 'failed'>('idle');
  protected readonly remembered = signal(false);
  protected readonly error = signal<string | null>(null);
  protected readonly options = computed(() => (this.rating() === -1 ? NEGATIVE : POSITIVE));

  protected choose(rating: -1 | 1): void {
    if (this.rating() !== rating) {
      this.selected.set([]);
    }
    this.choice.set(rating);
    this.open.set(true);
    this.state.set('idle');
    if (rating === 1) {
      this.submit(); // a thumbs-up counts immediately; reasons are optional
    }
  }

  protected toggle(category: FeedbackCategory): void {
    this.selected.update((list) =>
      list.includes(category)
        ? list.filter((c) => c !== category)
        : [...list, category].slice(0, 5),
    );
  }

  protected submit(): void {
    const rating = this.rating();
    if (rating === null) {
      return;
    }
    this.state.set('sending');
    this.error.set(null);
    const text = this.comment().trim();
    this.send()({
      queryId: this.queryId(),
      rating,
      categories: this.selected(),
      comment: text === '' ? null : text,
    }).subscribe({
      next: (result) => {
        this.state.set('sent');
        this.remembered.set(result.remembered);
        if (rating === -1 || this.selected().length > 0 || text !== '') {
          this.open.set(false);
        }
        this.rated.emit(rating);
      },
      error: (error: unknown) => {
        this.state.set('failed');
        this.error.set(describeBridgeError(error));
      },
    });
  }
}
