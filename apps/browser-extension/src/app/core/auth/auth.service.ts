import { DestroyRef, Injectable, computed, inject, signal } from '@angular/core';
import { Router } from '@angular/router';
import { Observable, catchError, firstValueFrom, map, of, tap } from 'rxjs';

import {
  BackgroundBridgeService,
  RUNTIME_EVENTS,
  RUNTIME_MESSENGER,
} from '../messaging/background-bridge.service';
import {
  isAuthChangedEvent,
  type LoginHints,
  type LoginRequest,
  type SessionInfo,
} from '../messaging/protocol';

/**
 * Session state for the side panel, as signals.
 *
 * The side panel only ever knows *who* is signed in (user + tenant); tokens stay in
 * the background worker. Session changes made by the worker (logout elsewhere,
 * revoked refresh token) arrive as `auth.changed` events.
 */
@Injectable({ providedIn: 'root' })
export class AuthService {
  private readonly bridge = inject(BackgroundBridgeService);
  private readonly router = inject(Router);
  private readonly runtime = inject(RUNTIME_MESSENGER);
  private readonly events = inject(RUNTIME_EVENTS);

  /** `undefined` until the first `auth.session` lookup completes. */
  private readonly _session = signal<SessionInfo | null | undefined>(undefined);
  private loading: Promise<SessionInfo | null> | null = null;

  readonly session = computed(() => this._session() ?? null);
  readonly user = computed(() => this._session()?.user ?? null);
  readonly signedIn = computed(() => this.user() !== null);

  constructor() {
    const listener = (message: unknown, sender: { id?: string | undefined }): void => {
      if (sender.id === undefined || sender.id !== this.runtime?.id) {
        return;
      }
      if (isAuthChangedEvent(message)) {
        this.applySession(message.session);
      }
    };
    this.events?.addListener(listener);
    inject(DestroyRef).onDestroy(() => this.events?.removeListener(listener));
  }

  /** Resolves the current session once; later calls reuse the cached state. */
  ensureLoaded(): Promise<SessionInfo | null> {
    const current = this._session();
    if (current !== undefined) {
      return Promise.resolve(current);
    }
    this.loading ??= firstValueFrom(
      this.bridge.request<'auth.session'>({ type: 'auth.session' }).pipe(
        // Outside the extension or background unavailable: treat as signed out.
        catchError(() => of(null)),
        tap((session) => {
          this._session.set(session);
        }),
      ),
    ).finally(() => {
      this.loading = null;
    });
    return this.loading;
  }

  login(request: LoginRequest): Observable<SessionInfo> {
    return this.bridge
      .request<'auth.login'>({ type: 'auth.login', payload: request })
      .pipe(tap((session) => this._session.set(session)));
  }

  logout(): Observable<void> {
    return this.bridge.request<'auth.logout'>({ type: 'auth.logout' }).pipe(
      catchError(() => of(null)),
      map(() => undefined),
      tap(() => {
        this.applySession(null);
      }),
    );
  }

  loginHints(): Observable<LoginHints | null> {
    return this.bridge
      .request<'auth.loginHints'>({ type: 'auth.loginHints' })
      .pipe(catchError(() => of(null)));
  }

  /** Called when an API call reports the session is gone. */
  sessionEnded(): void {
    this.applySession(null);
  }

  private applySession(session: SessionInfo | null): void {
    const wasSignedIn = this.signedIn();
    this._session.set(session);
    if (session === null && wasSignedIn) {
      void this.router.navigate(['/login']);
    }
  }
}
