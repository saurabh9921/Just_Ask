import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  computed,
  effect,
  inject,
  signal,
  untracked,
  viewChild,
} from '@angular/core';
import { ActivatedRoute, Router } from '@angular/router';
import { MatButtonModule } from '@angular/material/button';

import { WorkspaceSelectorComponent } from '../../connections/workspace-selector.component';
import { WorkspaceService } from '../../connections/workspace.service';
import { AuthService } from '../../core/auth/auth.service';
import type { QueryMode } from '../../core/messaging/analytics-types';
import { IconComponent } from '../../shared/icon/icon.component';
import { ChatMessageComponent } from '../chat-message/chat-message.component';
import { type ChatTurn, ChatStore } from '../chat.store';

const MAX_QUESTION = 1000;

/**
 * The primary experience: pick a data source, ask in plain English, watch the
 * governed query run and read the answer. Follow-ups refine the previous plan.
 */
@Component({
  selector: 'eia-analytics-chat',
  imports: [MatButtonModule, IconComponent, WorkspaceSelectorComponent, ChatMessageComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <section class="chat" aria-labelledby="chat-title">
      <h1 id="chat-title" class="visually-hidden">Ask your ERP data</h1>
      <eia-workspace-selector />

      <div class="chat__bar">
        <div class="context" aria-label="Query context">
          <span><strong>Source:</strong> {{ dataSource() }}</span>
          <span><strong>Scope:</strong> {{ securityScope() }}</span>
          <span class="context__ro">Read-only</span>
        </div>
        <button
          mat-stroked-button
          type="button"
          class="new-chat"
          [disabled]="store.running() || store.turns().length === 0"
          [attr.title]="store.running() ? 'Wait for the current answer to finish' : null"
          (click)="newChat()"
        >
          <eia-icon name="add" [size]="16" /> New chat
        </button>
      </div>

      <div class="conversation" role="log" aria-live="polite" aria-relevant="additions">
        @for (turn of store.turns(); track turn.id) {
          <eia-chat-message
            [turn]="turn"
            (followUp)="startFollowUp($event)"
            (retry)="store.retry($event)"
            (runPlan)="runPlan($event)"
          />
        } @empty {
          <div class="welcome">
            <p class="welcome__title">Ask a question about your ERP data</p>
            <p class="welcome__text">
              Answers are computed from governed, read-only views and always show their source,
              filters and security scope.
            </p>
            <ul class="examples" aria-label="Example questions">
              @for (q of examples; track q) {
                <li>
                  <button type="button" class="example" (click)="useExample(q)">{{ q }}</button>
                </li>
              }
            </ul>
          </div>
        }
        <div #end></div>
      </div>

      <form class="composer" (submit)="$event.preventDefault(); submit('execute')">
        @if (followUpOf(); as parent) {
          <div class="follow-up" role="status">
            <eia-icon name="reply" [size]="14" />
            <span>Following up on “{{ parent.question }}”</span>
            <button
              type="button"
              class="follow-up__clear"
              aria-label="Ask a new question instead"
              (click)="followUpOf.set(null)"
            >
              <eia-icon name="close" [size]="14" />
            </button>
          </div>
        }
        <label for="eia-question" class="visually-hidden">Your question</label>
        <textarea
          #input
          id="eia-question"
          class="composer__input"
          rows="2"
          [attr.maxlength]="maxLength"
          aria-describedby="eia-question-hint"
          [placeholder]="
            followUpOf()
              ? 'e.g. Now break it down by branch'
              : 'e.g. Total sales by region for the last six months'
          "
          [value]="draft()"
          (input)="draft.set($any($event.target).value)"
          (keydown)="onKeydown($event)"
        ></textarea>
        <div class="composer__bar">
          <span id="eia-question-hint" class="hint">
            @if (!canAsk()) {
              {{ blockedReason() }}
            } @else {
              Enter to ask · Shift+Enter for a new line
            }
          </span>
          <button
            mat-stroked-button
            type="button"
            [disabled]="!canSubmit()"
            (click)="submit('plan')"
            title="Validate and show the query plan without running it"
          >
            Plan only
          </button>
          <button mat-flat-button type="submit" [disabled]="!canSubmit()">
            <eia-icon name="send" [size]="16" /> Ask
          </button>
        </div>
      </form>
    </section>
  `,
  styles: `
    .chat {
      display: flex;
      flex-direction: column;
      gap: 12px;
    }
    .chat__bar {
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .new-chat {
      flex: none;
    }
    .context {
      flex: 1;
      display: flex;
      flex-wrap: wrap;
      gap: 4px 12px;
      font: var(--mat-sys-body-small);
      color: var(--mat-sys-on-surface-variant);
    }
    .context__ro {
      padding: 0 8px;
      border-radius: 999px;
      background: var(--mat-sys-tertiary-container);
      color: var(--mat-sys-on-tertiary-container);
    }
    .conversation {
      display: grid;
      gap: 16px;
    }
    .welcome {
      padding: 16px;
      border-radius: 14px;
      background: var(--mat-sys-surface-container-low);
    }
    .welcome__title {
      margin: 0 0 4px;
      font: var(--mat-sys-title-medium);
    }
    .welcome__text {
      margin: 0 0 12px;
      color: var(--mat-sys-on-surface-variant);
    }
    .examples {
      list-style: none;
      margin: 0;
      padding: 0;
      display: grid;
      gap: 6px;
    }
    .example {
      width: 100%;
      text-align: left;
      padding: 8px 12px;
      border-radius: 10px;
      border: 1px solid var(--mat-sys-outline-variant);
      background: var(--mat-sys-surface);
      color: inherit;
      font: var(--mat-sys-body-medium);
      cursor: pointer;
    }
    .example:hover {
      background: var(--mat-sys-surface-container-high);
    }
    .composer {
      position: sticky;
      bottom: 0;
      padding: 8px;
      border-radius: 14px;
      border: 1px solid var(--mat-sys-outline-variant);
      background: var(--mat-sys-surface-container);
      box-shadow: 0 -4px 12px rgb(0 0 0 / 6%);
    }
    .composer__input {
      box-sizing: border-box;
      width: 100%;
      resize: vertical;
      min-height: 44px;
      max-height: 200px;
      padding: 8px;
      border: 0;
      border-radius: 8px;
      background: var(--mat-sys-surface);
      color: inherit;
      font: var(--mat-sys-body-medium);
    }
    .composer__bar {
      display: flex;
      align-items: center;
      gap: 6px;
      margin-top: 6px;
    }
    .hint {
      flex: 1;
      font: var(--mat-sys-label-small);
      color: var(--mat-sys-on-surface-variant);
    }
    .follow-up {
      display: flex;
      align-items: center;
      gap: 6px;
      margin-bottom: 6px;
      font: var(--mat-sys-label-medium);
      color: var(--mat-sys-on-surface-variant);
    }
    .follow-up span {
      flex: 1;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .follow-up__clear {
      all: unset;
      cursor: pointer;
      display: inline-flex;
      padding: 2px;
      border-radius: 4px;
    }
    .follow-up__clear:focus-visible {
      outline: 2px solid var(--mat-sys-primary);
    }
  `,
})
export class AnalyticsChatComponent {
  protected readonly store = inject(ChatStore);
  private readonly auth = inject(AuthService);
  private readonly workspace = inject(WorkspaceService);
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);

  private readonly input = viewChild<ElementRef<HTMLTextAreaElement>>('input');
  private readonly end = viewChild<ElementRef<HTMLDivElement>>('end');

  protected readonly maxLength = MAX_QUESTION;
  protected readonly draft = signal('');
  protected readonly followUpOf = signal<ChatTurn | null>(null);

  protected readonly dataSource = computed(() => this.workspace.selected()?.name ?? 'Not selected');
  protected readonly securityScope = computed(() => this.auth.user()?.tenant.name ?? '—');
  protected readonly canAsk = computed(
    () => this.workspace.selectedId() !== null || this.followUpOf() !== null,
  );
  protected readonly blockedReason = computed(() =>
    this.workspace.loaded() && this.workspace.connections().length === 0
      ? 'No data source is available to you.'
      : 'Select a data source to ask a question.',
  );
  protected readonly canSubmit = computed(
    () => this.canAsk() && this.draft().trim().length > 0 && this.draft().length <= MAX_QUESTION,
  );

  protected readonly examples: readonly string[] = [
    'Show total sales by region for the last six months',
    'Top 5 customers by revenue this year',
    "Compare this month's sales with last month",
    'Monthly sales trend for the last 12 months',
    'Which products are below minimum stock?',
    'Show open sales orders older than seven days',
  ];

  constructor() {
    // Keep the newest answer in view.
    effect(() => {
      const count = this.store.turns().length;
      if (count > 0) {
        untracked(() => this.end()?.nativeElement.scrollIntoView({ block: 'end' }));
      }
    });
    // "Run again" from history: /chat?q=...&connection=...
    const params = this.route.snapshot.queryParamMap;
    const question = params.get('q');
    if (question !== null) {
      const connection = params.get('connection');
      if (connection !== null && this.workspace.selectedId() !== connection) {
        this.workspace.select(connection);
      }
      this.draft.set(question.slice(0, MAX_QUESTION));
      void this.router.navigate([], { queryParams: {}, replaceUrl: true });
    }
  }

  protected onKeydown(event: KeyboardEvent): void {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      this.submit('execute');
    } else if (event.key === 'Escape' && this.followUpOf() !== null) {
      this.followUpOf.set(null);
    }
  }

  protected submit(mode: QueryMode): void {
    if (!this.canSubmit()) {
      return;
    }
    const turn = this.store.ask(this.draft(), {
      mode,
      connectionId: this.workspace.selectedId(),
      followUpOf: this.followUpOf(),
    });
    if (turn !== null) {
      this.draft.set('');
      this.followUpOf.set(null);
    }
  }

  /** Starts a new chat: a fresh conversation in the panel and in the history. */
  protected newChat(): void {
    this.store.clear();
    this.followUpOf.set(null);
    this.input()?.nativeElement.focus();
  }

  protected useExample(question: string): void {
    this.draft.set(question);
    this.input()?.nativeElement.focus();
  }

  protected startFollowUp(turn: ChatTurn): void {
    this.followUpOf.set(turn.response?.status === 'succeeded' ? turn : null);
    this.input()?.nativeElement.focus();
  }

  protected runPlan(turn: ChatTurn): void {
    this.store.ask(turn.question, {
      mode: 'execute',
      connectionId: turn.connectionId,
      followUpOf:
        turn.parentQueryId === null
          ? null
          : (this.store.turns().find((t) => t.queryId === turn.parentQueryId) ?? null),
    });
  }
}
