import { TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';

import {
  BackgroundBridgeService,
  BridgeError,
} from '../../core/messaging/background-bridge.service';
import type { ExtensionRequest } from '../../core/messaging/protocol';
import { ConnectionStatusService } from '../../core/status/connection-status.service';
import { ConnectionSettingsComponent } from './connection-settings.component';

async function render(request: (message: ExtensionRequest) => unknown) {
  const refresh = vi.fn();
  const requestSpy = vi.fn(request);
  TestBed.configureTestingModule({
    imports: [ConnectionSettingsComponent],
    providers: [
      { provide: BackgroundBridgeService, useValue: { request: requestSpy } },
      { provide: ConnectionStatusService, useValue: { refresh } },
    ],
  });
  const fixture = TestBed.createComponent(ConnectionSettingsComponent);
  fixture.detectChanges();
  await fixture.whenStable();
  const element = fixture.nativeElement as HTMLElement;
  const input = element.querySelector<HTMLInputElement>('input[formcontrolname="apiBaseUrl"]')!;
  const click = async (label: string) => {
    const button = Array.from(element.querySelectorAll('button')).find((b) =>
      b.textContent.includes(label),
    )!;
    button.click();
    fixture.detectChanges();
    await fixture.whenStable();
  };
  return { fixture, element, input, click, requestSpy, refresh };
}

describe('ConnectionSettingsComponent', () => {
  it('loads the saved API URL', async () => {
    const { input } = await render(() => of({ apiBaseUrl: 'https://erp.example.com' }));
    expect(input.value).toBe('https://erp.example.com');
  });

  it('blocks saving an insecure remote URL', async () => {
    const { fixture, element, input, click, requestSpy } = await render(() =>
      of({ apiBaseUrl: 'http://localhost:8080' }),
    );
    input.value = 'http://erp.example.com';
    input.dispatchEvent(new Event('input'));
    await click('Save');
    fixture.detectChanges();
    expect(element.textContent).toContain('HTTPS');
    expect(requestSpy).toHaveBeenCalledTimes(1); // only the initial settings.get
  });

  it('saves a valid URL and refreshes the status indicator', async () => {
    const { element, input, click, requestSpy, refresh } = await render((message) =>
      message.type === 'settings.set'
        ? of(message.payload)
        : of({ apiBaseUrl: 'http://localhost:8080' }),
    );
    input.value = 'https://insight.example.com';
    input.dispatchEvent(new Event('input'));
    await click('Save');
    expect(requestSpy).toHaveBeenCalledWith({
      type: 'settings.set',
      payload: { apiBaseUrl: 'https://insight.example.com' },
    });
    expect(refresh).toHaveBeenCalled();
    expect(element.querySelector('[data-testid="save-message"]')?.textContent).toContain('saved');
  });

  it('shows the connection test result', async () => {
    const { element, click } = await render((message) =>
      message.type === 'health.check'
        ? of({
            status: 'degraded',
            latencyMs: 30,
            checks: { database: 'ok', qdrant: 'error' },
            checkedAt: '2026-01-01T00:00:00Z',
            detail: 'API readiness check returned HTTP 503.',
          })
        : of({ apiBaseUrl: 'http://localhost:8080' }),
    );
    await click('Test connection');
    const result = element.querySelector('[data-testid="health-result"]');
    expect(result?.textContent).toContain('degraded');
    expect(result?.textContent).toContain('qdrant');
    expect(result?.textContent).toContain('503');
  });

  it('shows an error state when running outside the extension', async () => {
    const { element } = await render(() =>
      throwError(() => new BridgeError('NOT_IN_EXTENSION', 'Not running inside the extension.')),
    );
    expect(element.querySelector('[role="alert"]')?.textContent).toContain(
      'Not running inside the extension.',
    );
  });
});
