import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  computed,
  inject,
  signal,
} from '@angular/core';
import { Router } from '@angular/router';
import { MatButtonModule } from '@angular/material/button';
import { Subject, debounceTime } from 'rxjs';

import { ChatStore } from '../chat/chat.store';
import { ResultViewComponent } from '../chat/result-view/result-view.component';
import type {
  ConversationDetail,
  ConversationItem,
  HistoryItem,
} from '../core/messaging/analytics-types';
import { describeBridgeError } from '../core/messaging/background-bridge.service';
import { ErrorStateComponent } from '../shared/error-state/error-state.component';
import { formatDateTime, formatDuration, humanize } from '../shared/format/value-format';
import { IconComponent } from '../shared/icon/icon.component';
import { QueryHistoryService } from './query-history.service';

type Filter = 'all' | 'saved';

/**
 * Past chats: one entry per conversation (its first question is the title). Opening a
 * chat shows every question and answer in order; search, save, delete and run again
 * work on chats and their questions.
 */
@Component({
  selector: 'eia-query-history',
  imports: [MatButtonModule, IconComponent, ErrorStateComponent, ResultViewComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <section class="page" aria-labelledby="history-title">
      <h1 id="history-title" class="page__title">Chat history</h1>
      <p class="page__subtitle">
        Your chats, most recently active first. Answers are kept for a short time.
      </p>

      <div class="toolbar">
        <label class="visually-hidden" for="history-search">Search chats</label>
        <input
          id="history-search"
          class="search"
          type="search"
          placeholder="Search chats"
          maxlength="200"
          [value]="search()"
          (input)="onSearch($any($event.target).value)"
        />
        <div class="filters" role="radiogroup" aria-label="Show">
          <button
            type="button"
            role="radio"
            class="filter"
            [attr.aria-checked]="filter() === 'all'"
            (click)="setFilter('all')"
          >
            All
          </button>
          <button
            type="button"
            role="radio"
            class="filter"
            [attr.aria-checked]="filter() === 'saved'"
            (click)="setFilter('saved')"
          >
            Saved
          </button>
        </div>
      </div>

      @if (history.error(); as error) {
        <eia-error-state
          title="History is unavailable"
          [message]="error"
          [retryable]="true"
          (retry)="reload()"
        />
      }

      <ul class="items">
        @for (chat of history.items(); track chat.conversationId) {
          <li class="item" [class.open]="openId() === chat.conversationId">
            <button
              type="button"
              class="item__head"
              [attr.aria-expanded]="openId() === chat.conversationId"
              (click)="toggle(chat)"
            >
              <span class="item__title">{{ chat.title }}</span>
              @if (chat.questionCount > 1) {
                <span class="item__last">Latest: {{ chat.lastQuestion }}</span>
              }
              <span class="item__meta">
                <span class="count">{{ countLabel(chat.questionCount) }}</span>
                @if (chat.connectionName) {
                  <span>{{ chat.connectionName }}</span>
                }
                <span>Last activity {{ when(chat.lastActivityAt) }}</span>
                @if (chat.saved) {
                  <span aria-label="Saved chat"><eia-icon name="bookmark" [size]="14" /></span>
                }
              </span>
            </button>
            <div class="item__actions">
              <button
                mat-button
                type="button"
                [attr.aria-pressed]="chat.saved"
                (click)="toggleSaved(chat)"
              >
                <eia-icon [name]="chat.saved ? 'bookmark' : 'bookmarkOutline'" [size]="16" />
                {{ chat.saved ? 'Saved' : 'Save' }}
              </button>
              @if (confirmDelete() === chat.conversationId) {
                <span class="confirm" role="alert">
                  Delete this chat and its {{ countLabel(chat.questionCount) }}?
                </span>
                <button mat-flat-button type="button" (click)="remove(chat)">Delete</button>
                <button mat-button type="button" (click)="confirmDelete.set(null)">Keep</button>
              } @else {
                <button
                  mat-button
                  type="button"
                  [attr.aria-label]="'Delete chat: ' + chat.title"
                  (click)="confirmDelete.set(chat.conversationId)"
                >
                  <eia-icon name="delete" [size]="16" /> Delete
                </button>
              }
            </div>
            @if (actionError(); as message) {
              @if (actionFor() === chat.conversationId) {
                <p class="action-error" role="alert">{{ message }}</p>
              }
            }
            @if (openId() === chat.conversationId) {
              <div class="detail" aria-live="polite">
                @if (detailLoading()) {
                  <p class="muted">Loading the chat…</p>
                } @else if (detail(); as d) {
                  <ol class="messages" [attr.aria-label]="'Chat: ' + d.title">
                    @for (message of d.messages; track message.queryId) {
                      <li class="message">
                        <p class="message__question">
                          <span class="visually-hidden">Question: </span>{{ message.question }}
                        </p>
                        <p class="message__meta">
                          <span class="status status--{{ message.status }}">
                            {{ statusLabel(message) }}
                          </span>
                          @if (message.rowCount !== null) {
                            <span>{{ message.rowCount }} rows</span>
                          }
                          @if (message.totalMs !== null) {
                            <span>{{ duration(message.totalMs) }}</span>
                          }
                          <span>{{ when(message.createdAt) }}</span>
                          @if (message.feedbackRating === 1) {
                            <span aria-label="Rated correct"
                              ><eia-icon name="thumbUp" [size]="14"
                            /></span>
                          } @else if (message.feedbackRating === -1) {
                            <span aria-label="Rated wrong"
                              ><eia-icon name="thumbDown" [size]="14"
                            /></span>
                          }
                        </p>
                        <div class="message__answer">
                          @if (message.response) {
                            <eia-result-view
                              [response]="message.response"
                              [canExport]="chatStore.canExport()"
                            />
                          } @else {
                            <p class="muted">
                              {{
                                message.responseExpired
                                  ? 'The answer is no longer cached. Run the question again to get fresh results.'
                                  : 'No answer is available for this question.'
                              }}
                            </p>
                          }
                        </div>
                        <button
                          mat-button
                          type="button"
                          class="message__again"
                          [attr.aria-label]="'Run again: ' + message.question"
                          (click)="runAgain(message)"
                        >
                          <eia-icon name="refresh" [size]="16" /> Run again
                        </button>
                      </li>
                    }
                  </ol>
                } @else if (detailError(); as message) {
                  <p class="action-error" role="alert">{{ message }}</p>
                }
              </div>
            }
          </li>
        } @empty {
          @if (!history.loading() && history.error() === null) {
            <li class="empty" role="status">
              <p class="empty__title">
                {{ filter() === 'saved' ? 'No saved chats' : 'No chats yet' }}
              </p>
              <p class="empty__text">
                @if (search()) {
                  Nothing matches “{{ search() }}”.
                } @else {
                  Chats you have with the assistant appear here, with every question and answer.
                }
              </p>
            </li>
          }
        }
      </ul>

      @if (history.loading()) {
        <p class="muted" role="status">Loading history…</p>
      } @else if (hasMore()) {
        <button mat-stroked-button type="button" class="more" (click)="history.loadMore()">
          Load more
        </button>
      }
    </section>
  `,
  styles: `
    .toolbar {
      display: flex;
      gap: 8px;
      align-items: center;
      margin-bottom: 12px;
    }
    .search {
      flex: 1;
      min-width: 0;
      padding: 8px 10px;
      border-radius: 10px;
      border: 1px solid var(--mat-sys-outline-variant);
      background: var(--mat-sys-surface);
      color: inherit;
      font: var(--mat-sys-body-medium);
    }
    .filters {
      display: flex;
      border: 1px solid var(--mat-sys-outline-variant);
      border-radius: 999px;
      overflow: hidden;
    }
    .filter {
      border: 0;
      padding: 6px 12px;
      background: transparent;
      color: inherit;
      cursor: pointer;
      font: var(--mat-sys-label-large);
    }
    .filter[aria-checked='true'] {
      background: var(--mat-sys-secondary-container);
      color: var(--mat-sys-on-secondary-container);
    }
    .items {
      list-style: none;
      margin: 0;
      padding: 0;
      display: grid;
      gap: 8px;
    }
    .item {
      border: 1px solid var(--mat-sys-outline-variant);
      border-radius: 12px;
      background: var(--mat-sys-surface-container-lowest);
    }
    .item__head {
      all: unset;
      box-sizing: border-box;
      display: grid;
      gap: 4px;
      width: 100%;
      padding: 10px 12px 4px;
      cursor: pointer;
    }
    .item__head:focus-visible {
      outline: 2px solid var(--mat-sys-primary);
      border-radius: 12px;
    }
    .item__title {
      font: var(--mat-sys-body-medium);
    }
    .item__last {
      font: var(--mat-sys-body-small);
      color: var(--mat-sys-on-surface-variant);
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .item__meta,
    .message__meta {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 4px 10px;
      font: var(--mat-sys-label-small);
      color: var(--mat-sys-on-surface-variant);
    }
    .count,
    .status {
      padding: 0 6px;
      border-radius: 999px;
      background: var(--mat-sys-surface-container-high);
    }
    .status--succeeded {
      color: var(--eia-status-ok);
    }
    .status--failed,
    .status--refused {
      color: var(--eia-status-offline);
    }
    .item__actions {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 2px;
      padding: 0 6px 6px;
    }
    .confirm {
      font: var(--mat-sys-body-small);
      margin: 0 4px;
    }
    .detail {
      padding: 8px 12px 12px;
      border-top: 1px solid var(--mat-sys-outline-variant);
    }
    .messages {
      list-style: none;
      margin: 0;
      padding: 0;
      display: grid;
      gap: 14px;
    }
    .message {
      display: grid;
      gap: 6px;
    }
    .message + .message {
      padding-top: 12px;
      border-top: 1px dashed var(--mat-sys-outline-variant);
    }
    .message__question {
      justify-self: end;
      max-width: 90%;
      margin: 0;
      padding: 8px 12px;
      border-radius: 14px 14px 4px 14px;
      background: var(--mat-sys-primary-container);
      color: var(--mat-sys-on-primary-container);
      font: var(--mat-sys-body-medium);
    }
    .message__meta {
      justify-content: flex-end;
      margin: 0;
    }
    .message__again {
      justify-self: start;
    }
    .muted,
    .empty__text {
      margin: 0;
      font: var(--mat-sys-body-small);
      color: var(--mat-sys-on-surface-variant);
    }
    .action-error {
      margin: 0 12px 8px;
      font: var(--mat-sys-body-small);
      color: var(--eia-status-offline);
    }
    .empty {
      padding: 24px;
      border-radius: 12px;
      border: 1px dashed var(--mat-sys-outline-variant);
      text-align: center;
    }
    .empty__title {
      margin: 0 0 4px;
      font: var(--mat-sys-title-small);
    }
    .more {
      margin-top: 12px;
    }
  `,
})
export class QueryHistoryComponent {
  protected readonly history = inject(QueryHistoryService);
  protected readonly chatStore = inject(ChatStore);
  private readonly router = inject(Router);

  protected readonly filter = signal<Filter>('all');
  protected readonly search = signal('');
  protected readonly openId = signal<string | null>(null);
  protected readonly detail = signal<ConversationDetail | null>(null);
  protected readonly detailLoading = signal(false);
  protected readonly detailError = signal<string | null>(null);
  protected readonly confirmDelete = signal<string | null>(null);
  protected readonly actionError = signal<string | null>(null);
  protected readonly actionFor = signal<string | null>(null);
  protected readonly hasMore = computed(() => this.history.items().length < this.history.total());

  private readonly searches = new Subject<string>();

  constructor() {
    this.reload();
    const subscription = this.searches.pipe(debounceTime(300)).subscribe(() => this.reload());
    inject(DestroyRef).onDestroy(() => subscription.unsubscribe());
  }

  protected reload(): void {
    this.openId.set(null);
    this.history.load({ savedOnly: this.filter() === 'saved', search: this.search() });
  }

  protected onSearch(value: string): void {
    this.search.set(value);
    this.searches.next(value);
  }

  protected setFilter(filter: Filter): void {
    if (this.filter() !== filter) {
      this.filter.set(filter);
      this.reload();
    }
  }

  protected toggle(chat: ConversationItem): void {
    if (this.openId() === chat.conversationId) {
      this.openId.set(null);
      return;
    }
    this.openId.set(chat.conversationId);
    this.detail.set(null);
    this.detailError.set(null);
    this.detailLoading.set(true);
    this.history.detail(chat.conversationId).subscribe({
      next: (detail) => {
        if (this.openId() === chat.conversationId) {
          this.detail.set(detail);
        }
        this.detailLoading.set(false);
      },
      error: (error: unknown) => {
        this.detailLoading.set(false);
        this.detailError.set(describeBridgeError(error));
      },
    });
  }

  /** Asks one question of the chat again, in the chat panel. */
  protected runAgain(message: HistoryItem): void {
    void this.router.navigate(['/chat'], {
      queryParams: {
        q: message.question,
        ...(message.connectionId ? { connection: message.connectionId } : {}),
      },
    });
  }

  protected toggleSaved(chat: ConversationItem): void {
    this.actionError.set(null);
    this.history.setSaved(chat, !chat.saved).subscribe({
      error: (error: unknown) => this.fail(chat, error),
    });
  }

  protected remove(chat: ConversationItem): void {
    this.confirmDelete.set(null);
    this.actionError.set(null);
    this.history.delete(chat).subscribe({
      next: () => {
        if (this.openId() === chat.conversationId) {
          this.openId.set(null);
        }
      },
      error: (error: unknown) => this.fail(chat, error),
    });
  }

  protected countLabel(count: number): string {
    return count === 1 ? '1 question' : `${count} questions`;
  }

  protected statusLabel(item: HistoryItem): string {
    return humanize(item.status);
  }

  protected duration(ms: number): string {
    return formatDuration(ms);
  }

  protected when(iso: string): string {
    return formatDateTime(iso);
  }

  private fail(chat: ConversationItem, error: unknown): void {
    this.actionFor.set(chat.conversationId);
    this.actionError.set(describeBridgeError(error));
  }
}
