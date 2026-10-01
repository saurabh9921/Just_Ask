import { Injectable, inject, signal } from '@angular/core';
import { Observable, tap } from 'rxjs';

import { AuthService } from '../core/auth/auth.service';
import type { ConversationDetail, ConversationItem } from '../core/messaging/analytics-types';
import {
  BackgroundBridgeService,
  BridgeError,
  describeBridgeError,
} from '../core/messaging/background-bridge.service';

export const HISTORY_PAGE = 20;
const SESSION_ERRORS = new Set(['SESSION_EXPIRED', 'UNAUTHENTICATED']);

/** The signed-in user's chats (server-side, per user); each chat groups its questions. */
@Injectable({ providedIn: 'root' })
export class QueryHistoryService {
  private readonly bridge = inject(BackgroundBridgeService);
  private readonly auth = inject(AuthService);

  private readonly _items = signal<readonly ConversationItem[]>([]);
  private readonly _total = signal(0);
  private readonly _loading = signal(false);
  private readonly _error = signal<string | null>(null);

  readonly items = this._items.asReadonly();
  readonly total = this._total.asReadonly();
  readonly loading = this._loading.asReadonly();
  readonly error = this._error.asReadonly();

  private savedOnly = false;
  private search = '';

  load(options: { savedOnly: boolean; search: string }, append = false): void {
    this.savedOnly = options.savedOnly;
    this.search = options.search.trim();
    const offset = append ? this._items().length : 0;
    this._loading.set(true);
    this._error.set(null);
    this.bridge
      .request<'history.conversations'>({
        type: 'history.conversations',
        payload: {
          limit: HISTORY_PAGE,
          offset,
          ...(this.savedOnly ? { saved: true } : {}),
          ...(this.search !== '' ? { search: this.search } : {}),
        },
      })
      .subscribe({
        next: (page) => {
          this._items.set(append ? [...this._items(), ...page.items] : page.items);
          this._total.set(page.total);
          this._loading.set(false);
        },
        error: (error: unknown) => {
          this._loading.set(false);
          this.handle(error);
        },
      });
  }

  loadMore(): void {
    this.load({ savedOnly: this.savedOnly, search: this.search }, true);
  }

  /** The whole chat: every question with its answer while cached, oldest first. */
  detail(conversationId: string): Observable<ConversationDetail> {
    return this.bridge.request<'history.conversation'>({
      type: 'history.conversation',
      payload: { conversationId },
    });
  }

  /** Saves (or unsaves) every question of the chat. */
  setSaved(item: ConversationItem, saved: boolean): Observable<ConversationItem> {
    return this.bridge
      .request<'history.conversationSave'>({
        type: 'history.conversationSave',
        payload: { conversationId: item.conversationId, saved },
      })
      .pipe(
        tap((updated) => {
          this._items.update((items) =>
            this.savedOnly && !saved
              ? items.filter((i) => i.conversationId !== item.conversationId)
              : items.map((i) => (i.conversationId === item.conversationId ? updated : i)),
          );
        }),
      );
  }

  /** Deletes the chat with all of its questions and cached answers. */
  delete(item: ConversationItem): Observable<null> {
    return this.bridge
      .request<'history.conversationDelete'>({
        type: 'history.conversationDelete',
        payload: { conversationId: item.conversationId },
      })
      .pipe(
        tap(() => {
          this._items.update((items) =>
            items.filter((i) => i.conversationId !== item.conversationId),
          );
          this._total.update((n) => Math.max(0, n - 1));
        }),
      );
  }

  reset(): void {
    this._items.set([]);
    this._total.set(0);
    this._error.set(null);
  }

  private handle(error: unknown): void {
    if (error instanceof BridgeError && SESSION_ERRORS.has(error.code)) {
      this.auth.sessionEnded();
    }
    this._error.set(describeBridgeError(error));
  }
}
