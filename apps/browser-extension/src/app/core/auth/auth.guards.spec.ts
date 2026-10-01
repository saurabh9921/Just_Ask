import { TestBed } from '@angular/core/testing';
import {
  Router,
  UrlTree,
  provideRouter,
  type ActivatedRouteSnapshot,
  type RouterStateSnapshot,
} from '@angular/router';

import type { SessionInfo } from '../messaging/protocol';
import { authGuard, guestGuard, safeReturnUrl } from './auth.guards';
import { AuthService } from './auth.service';

const SESSION = { user: { id: 'u1' } } as unknown as SessionInfo;

function setup(session: SessionInfo | null) {
  TestBed.configureTestingModule({
    providers: [
      provideRouter([]),
      { provide: AuthService, useValue: { ensureLoaded: () => Promise.resolve(session) } },
    ],
  });
  const router = TestBed.inject(Router);
  const run = (guard: typeof authGuard, url: string) =>
    TestBed.runInInjectionContext(() =>
      guard({} as ActivatedRouteSnapshot, { url } as RouterStateSnapshot),
    ) as Promise<boolean | UrlTree>;
  return { router, run };
}

describe('authGuard', () => {
  it('allows signed-in users', async () => {
    const { run } = setup(SESSION);
    await expect(run(authGuard, '/chat')).resolves.toBe(true);
  });

  it('redirects signed-out users to /login with a return URL', async () => {
    const { run, router } = setup(null);
    const result = await run(authGuard, '/connections');
    expect(result).toBeInstanceOf(UrlTree);
    expect(router.serializeUrl(result as UrlTree)).toBe('/login?returnUrl=%2Fconnections');
  });
});

describe('guestGuard', () => {
  it('allows signed-out users onto the sign-in page', async () => {
    const { run } = setup(null);
    await expect(run(guestGuard, '/login')).resolves.toBe(true);
  });

  it('sends signed-in users to chat', async () => {
    const { run, router } = setup(SESSION);
    const result = await run(guestGuard, '/login');
    expect(router.serializeUrl(result as UrlTree)).toBe('/chat');
  });
});

describe('safeReturnUrl', () => {
  it.each([
    ['/history', '/history'],
    ['/connections', '/connections'],
    ['https://evil.example', '/chat'],
    ['//evil.example', '/chat'],
    ['/settings', '/chat'],
    [null, '/chat'],
  ])('%s -> %s', (input, expected) => {
    expect(safeReturnUrl(input)).toBe(expected);
  });
});
