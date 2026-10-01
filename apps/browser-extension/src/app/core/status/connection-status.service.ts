import { DestroyRef, Injectable, InjectionToken, computed, inject, signal } from '@angular/core';
import { Subject, catchError, exhaustMap, interval, merge, of, type Observable } from 'rxjs';

import { environment } from '../../../environments/environment';
import {
  BackgroundBridgeService,
  describeBridgeError,
} from '../messaging/background-bridge.service';
import type { HealthCheckResult, HealthStatus } from '../messaging/protocol';

export const HEALTH_POLL_INTERVAL_MS = new InjectionToken<number>('HEALTH_POLL_INTERVAL_MS', {
  providedIn: 'root',
  factory: () => environment.healthPollIntervalMs,
});

export type ConnectionState = HealthStatus | 'unknown';

/**
 * Polls API readiness through the background worker and exposes it as signals.
 * Polling starts on the first `start()` call and stops with the root injector.
 */
@Injectable({ providedIn: 'root' })
export class ConnectionStatusService {
  private readonly bridge = inject(BackgroundBridgeService);
  private readonly intervalMs = inject(HEALTH_POLL_INTERVAL_MS);
  private readonly destroyRef = inject(DestroyRef);
  private readonly manualRefresh$ = new Subject<void>();
  private started = false;

  private readonly _result = signal<HealthCheckResult | null>(null);
  private readonly _checking = signal(false);

  readonly result = this._result.asReadonly();
  readonly checking = this._checking.asReadonly();
  readonly state = computed<ConnectionState>(() => this._result()?.status ?? 'unknown');

  start(): void {
    if (this.started) {
      return;
    }
    this.started = true;
    const subscription = merge(of(undefined), interval(this.intervalMs), this.manualRefresh$)
      .pipe(exhaustMap(() => this.check()))
      .subscribe((result) => {
        this._result.set(result);
        this._checking.set(false);
      });
    this.destroyRef.onDestroy(() => subscription.unsubscribe());
  }

  refresh(): void {
    this.manualRefresh$.next();
  }

  private check(): Observable<HealthCheckResult> {
    this._checking.set(true);
    return this.bridge.request<'health.check'>({ type: 'health.check' }).pipe(
      catchError((error: unknown) =>
        of<HealthCheckResult>({
          status: 'offline',
          latencyMs: null,
          checks: {},
          checkedAt: new Date().toISOString(),
          detail: describeBridgeError(error),
        }),
      ),
    );
  }
}
