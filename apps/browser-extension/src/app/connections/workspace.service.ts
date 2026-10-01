import { Injectable, computed, inject, signal } from '@angular/core';
import { Observable, catchError, of, tap, throwError } from 'rxjs';

import { AuthService } from '../core/auth/auth.service';
import {
  BackgroundBridgeService,
  BridgeError,
  describeBridgeError,
} from '../core/messaging/background-bridge.service';
import type { ConnectionSummary, ConnectionTestResult } from '../core/messaging/protocol';

const SESSION_ERRORS = new Set(['SESSION_EXPIRED', 'UNAUTHENTICATED']);

/**
 * Connections the signed-in user may query, and the one currently selected.
 * The selection is persisted per user by the background worker (non-secret).
 */
@Injectable({ providedIn: 'root' })
export class WorkspaceService {
  private readonly bridge = inject(BackgroundBridgeService);
  private readonly auth = inject(AuthService);

  private readonly _connections = signal<readonly ConnectionSummary[]>([]);
  private readonly _selectedId = signal<string | null>(null);
  private readonly _loading = signal(false);
  private readonly _loaded = signal(false);
  private readonly _error = signal<string | null>(null);

  readonly connections = this._connections.asReadonly();
  readonly selectedId = this._selectedId.asReadonly();
  readonly loading = this._loading.asReadonly();
  readonly loaded = this._loaded.asReadonly();
  readonly error = this._error.asReadonly();
  readonly selected = computed(
    () => this._connections().find((c) => c.id === this._selectedId()) ?? null,
  );

  load(): void {
    this._loading.set(true);
    this._error.set(null);
    this.bridge.request<'connections.list'>({ type: 'connections.list' }).subscribe({
      next: (connections) => {
        this._connections.set(connections);
        this._loaded.set(true);
        this.restoreSelection(connections);
      },
      error: (error: unknown) => {
        this._loading.set(false);
        this.handleSessionError(error);
        this._error.set(describeBridgeError(error));
      },
    });
  }

  select(connectionId: string | null): void {
    this._selectedId.set(connectionId);
    this.bridge
      .request<'workspace.set'>({ type: 'workspace.set', payload: { connectionId } })
      .pipe(catchError(() => of(null)))
      .subscribe();
  }

  test(connectionId: string): Observable<ConnectionTestResult> {
    return this.bridge
      .request<'connections.test'>({ type: 'connections.test', payload: { id: connectionId } })
      .pipe(
        tap((result) => {
          const testedAt = new Date().toISOString();
          this._connections.update((list) =>
            list.map((c) =>
              c.id === connectionId
                ? { ...c, status: result.ok ? 'ok' : 'error', lastTestedAt: testedAt }
                : c,
            ),
          );
        }),
        catchError((error: unknown) => {
          this.handleSessionError(error);
          return throwError(() => error);
        }),
      );
  }

  /** Forget everything (called on sign-out). */
  reset(): void {
    this._connections.set([]);
    this._selectedId.set(null);
    this._loaded.set(false);
    this._error.set(null);
  }

  private restoreSelection(connections: readonly ConnectionSummary[]): void {
    this.bridge
      .request<'workspace.get'>({ type: 'workspace.get' })
      .pipe(catchError(() => of({ connectionId: null })))
      .subscribe((stored) => {
        this._loading.set(false);
        if (connections.some((c) => c.id === stored.connectionId)) {
          this._selectedId.set(stored.connectionId);
          return;
        }
        const fallback = connections[0]?.id ?? null;
        if (fallback === stored.connectionId) {
          this._selectedId.set(fallback);
        } else {
          this.select(fallback);
        }
      });
  }

  private handleSessionError(error: unknown): void {
    if (error instanceof BridgeError && SESSION_ERRORS.has(error.code)) {
      this.auth.sessionEnded();
    }
  }
}
