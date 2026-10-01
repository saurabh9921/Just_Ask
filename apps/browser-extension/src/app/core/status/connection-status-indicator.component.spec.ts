import { TestBed } from '@angular/core/testing';
import { Subject, of, throwError, type Observable } from 'rxjs';

import { BackgroundBridgeService, BridgeError } from '../messaging/background-bridge.service';
import type { HealthCheckResult } from '../messaging/protocol';
import { ConnectionStatusIndicatorComponent } from './connection-status-indicator.component';
import { HEALTH_POLL_INTERVAL_MS } from './connection-status.service';

function result(status: HealthCheckResult['status'], detail?: string): HealthCheckResult {
  return {
    status,
    latencyMs: status === 'offline' ? null : 42,
    checks: {},
    checkedAt: '2026-01-01T00:00:00.000Z',
    ...(detail === undefined ? {} : { detail }),
  };
}

async function render(response: () => Observable<HealthCheckResult>) {
  const request = vi.fn(response);
  TestBed.configureTestingModule({
    imports: [ConnectionStatusIndicatorComponent],
    providers: [
      { provide: BackgroundBridgeService, useValue: { request } },
      { provide: HEALTH_POLL_INTERVAL_MS, useValue: 60_000 },
    ],
  });
  const fixture = TestBed.createComponent(ConnectionStatusIndicatorComponent);
  fixture.detectChanges();
  await fixture.whenStable();
  const element = fixture.nativeElement as HTMLElement;
  const button = element.querySelector('button');
  if (button === null) {
    throw new Error('indicator button not rendered');
  }
  return { fixture, element, button, request };
}

describe('ConnectionStatusIndicatorComponent', () => {
  it('shows Online when the API is healthy', async () => {
    const { element, button, request } = await render(() => of(result('ok')));
    expect(request).toHaveBeenCalledWith({ type: 'health.check' });
    expect(button.dataset['state']).toBe('ok');
    expect(button.textContent).toContain('Online');
    expect(button.getAttribute('aria-label')).toContain('Online');
    expect(element.querySelector('[aria-live="polite"]')?.textContent).toContain('Online');
  });

  it('shows Degraded for a degraded API', async () => {
    const { button } = await render(() => of(result('degraded', 'HTTP 503')));
    expect(button.dataset['state']).toBe('degraded');
    expect(button.textContent).toContain('Degraded');
  });

  it('shows Offline when the bridge fails', async () => {
    const { button } = await render(() =>
      throwError(() => new BridgeError('NOT_IN_EXTENSION', 'Not in extension')),
    );
    expect(button.dataset['state']).toBe('offline');
    expect(button.textContent).toContain('Offline');
  });

  it('shows Checking before the first result and re-checks on click', async () => {
    const pending = new Subject<HealthCheckResult>();
    const { fixture, button, request } = await render(() => pending);
    expect(button.dataset['state']).toBe('unknown');
    expect(button.textContent).toContain('Checking');

    pending.next(result('ok'));
    pending.complete();
    await fixture.whenStable();
    expect(button.dataset['state']).toBe('ok');

    button.click();
    await fixture.whenStable();
    expect(request).toHaveBeenCalledTimes(2);
  });
});
