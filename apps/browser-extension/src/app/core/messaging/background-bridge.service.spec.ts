import { TestBed } from '@angular/core/testing';
import { firstValueFrom } from 'rxjs';

import {
  BRIDGE_TIMEOUT_MS,
  BackgroundBridgeService,
  BridgeError,
  RUNTIME_MESSENGER,
  describeBridgeError,
  type RuntimeMessenger,
} from './background-bridge.service';

function setup(runtime: RuntimeMessenger | null, timeoutMs = 1_000): BackgroundBridgeService {
  TestBed.configureTestingModule({
    providers: [
      { provide: RUNTIME_MESSENGER, useValue: runtime },
      { provide: BRIDGE_TIMEOUT_MS, useValue: timeoutMs },
    ],
  });
  return TestBed.inject(BackgroundBridgeService);
}

function runtimeReturning(
  response: unknown,
): RuntimeMessenger & { sendMessage: ReturnType<typeof vi.fn> } {
  return { id: 'ext-id', sendMessage: vi.fn(() => Promise.resolve(response)) };
}

describe('BackgroundBridgeService', () => {
  it('sends the message and unwraps a successful response', async () => {
    const runtime = runtimeReturning({ ok: true, data: { apiBaseUrl: 'http://localhost:8080' } });
    const bridge = setup(runtime);
    const settings = await firstValueFrom(bridge.request<'settings.get'>({ type: 'settings.get' }));
    expect(settings).toEqual({ apiBaseUrl: 'http://localhost:8080' });
    expect(runtime.sendMessage).toHaveBeenCalledWith({ type: 'settings.get' });
    expect(bridge.inExtension).toBe(true);
  });

  it('maps protocol errors to BridgeError', async () => {
    const bridge = setup(
      runtimeReturning({ ok: false, error: { code: 'VALIDATION_ERROR', message: 'Bad URL' } }),
    );
    const error: unknown = await firstValueFrom(
      bridge.request<'settings.set'>({ type: 'settings.set', payload: { apiBaseUrl: 'x' } }),
    ).catch((e: unknown) => e);
    expect(error).toBeInstanceOf(BridgeError);
    expect((error as BridgeError).code).toBe('VALIDATION_ERROR');
    expect(describeBridgeError(error)).toBe('Bad URL');
  });

  it('rejects malformed responses', async () => {
    const bridge = setup(runtimeReturning('not a response'));
    const error: unknown = await firstValueFrom(
      bridge.request<'health.check'>({ type: 'health.check' }),
    ).catch((e: unknown) => e);
    expect((error as BridgeError).code).toBe('INVALID_MESSAGE');
  });

  it('times out when the background worker does not answer', async () => {
    const bridge = setup(
      { id: 'ext-id', sendMessage: () => new Promise<unknown>(() => undefined) },
      20,
    );
    const error: unknown = await firstValueFrom(
      bridge.request<'health.check'>({ type: 'health.check' }),
    ).catch((e: unknown) => e);
    expect((error as BridgeError).code).toBe('TIMEOUT');
  });

  it('reports NOT_IN_EXTENSION outside the extension', async () => {
    const bridge = setup(null);
    expect(bridge.inExtension).toBe(false);
    const error: unknown = await firstValueFrom(
      bridge.request<'settings.get'>({ type: 'settings.get' }),
    ).catch((e: unknown) => e);
    expect((error as BridgeError).code).toBe('NOT_IN_EXTENSION');
  });

  it('never exposes raw non-bridge errors', () => {
    expect(describeBridgeError(new Error('stack details'))).toBe('An unexpected error occurred.');
  });
});
