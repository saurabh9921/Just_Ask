import { TestBed } from '@angular/core/testing';
import { Router, provideRouter } from '@angular/router';
import { of, throwError } from 'rxjs';

import {
  BackgroundBridgeService,
  BridgeError,
  RUNTIME_EVENTS,
  RUNTIME_MESSENGER,
  type RuntimeEvents,
} from '../messaging/background-bridge.service';
import type { ExtensionRequest, SessionInfo } from '../messaging/protocol';
import { AuthService } from './auth.service';

const SESSION: SessionInfo = {
  user: {
    id: 'u1',
    email: 'ana@acme.test',
    displayName: 'Ana',
    tenant: { id: 't1', slug: 'acme', name: 'Acme' },
    roles: ['analyst'],
    permissions: [],
  },
};

type Listener = Parameters<RuntimeEvents['addListener']>[0];

function setup(request: (message: ExtensionRequest) => unknown) {
  let listener: Listener | null = null;
  const events: RuntimeEvents = {
    addListener: (l) => {
      listener = l;
    },
    removeListener: vi.fn(),
  };
  TestBed.configureTestingModule({
    providers: [
      provideRouter([]),
      { provide: BackgroundBridgeService, useValue: { request: vi.fn(request) } },
      { provide: RUNTIME_MESSENGER, useValue: { id: 'own-id', sendMessage: vi.fn() } },
      { provide: RUNTIME_EVENTS, useValue: events },
    ],
  });
  const auth = TestBed.inject(AuthService);
  const navigate = vi.spyOn(TestBed.inject(Router), 'navigate').mockResolvedValue(true);
  const emit = (message: unknown, senderId: string) => listener?.(message, { id: senderId });
  return { auth, navigate, emit };
}

describe('AuthService', () => {
  it('loads the session once and caches it', async () => {
    const request = vi.fn(() => of(SESSION));
    const { auth } = setup(request);
    await expect(auth.ensureLoaded()).resolves.toEqual(SESSION);
    await auth.ensureLoaded();
    expect(auth.user()?.displayName).toBe('Ana');
    expect(auth.signedIn()).toBe(true);
  });

  it('treats a failing background as signed out', async () => {
    const { auth } = setup(() => throwError(() => new BridgeError('NOT_IN_EXTENSION', 'x')));
    await expect(auth.ensureLoaded()).resolves.toBeNull();
    expect(auth.signedIn()).toBe(false);
  });

  it('applies auth.changed events from its own background and returns to sign-in', async () => {
    const { auth, emit, navigate } = setup(() => of(SESSION));
    await auth.ensureLoaded();
    emit({ event: 'auth.changed', session: null }, 'own-id');
    expect(auth.signedIn()).toBe(false);
    expect(navigate).toHaveBeenCalledWith(['/login']);
  });

  it('ignores events from other senders', async () => {
    const { auth, emit } = setup(() => of(SESSION));
    await auth.ensureLoaded();
    emit({ event: 'auth.changed', session: null }, 'another-extension');
    expect(auth.signedIn()).toBe(true);
  });

  it('clears the session on logout even if the background call fails', async () => {
    const { auth } = setup((message) =>
      message.type === 'auth.session'
        ? of(SESSION)
        : throwError(() => new BridgeError('TIMEOUT', 'x')),
    );
    await auth.ensureLoaded();
    auth.logout().subscribe();
    expect(auth.signedIn()).toBe(false);
  });
});
