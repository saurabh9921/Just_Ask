import type { AuthChangedEvent } from '../app/core/messaging/protocol';
import { ApiClient, ApiError, type FetchFn } from './api-client';
import { SessionManager } from './session-manager';
import { MemoryStorage } from './storage';
import { TokenStore } from './token-store';

const USER_JSON = {
  id: 'u-1',
  email: 'ana@acme.test',
  display_name: 'Ana',
  tenant: { id: 't-1', slug: 'acme', name: 'Acme' },
  roles: ['analyst'],
  permissions: ['analytics.query'],
};

function tokenBody(access: string, refresh: string, expiresIn = 600) {
  return {
    access_token: access,
    token_type: 'bearer',
    expires_in: expiresIn,
    refresh_token: refresh,
    refresh_expires_in: 43_200,
    user: USER_JSON,
  };
}

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function apiError(status: number, code: string): Response {
  return json(status, { error: { code, message: code, request_id: 'req_x', details: {} } });
}

type Route = (
  body: Record<string, unknown> | null,
  init: RequestInit,
) => Response | Promise<Response>;

/** Fake API server keyed by "METHOD path". */
function setup(routes: Record<string, Route>, start = 1_000_000) {
  let now = start;
  const calls: { key: string; body: Record<string, unknown> | null; auth: string | undefined }[] =
    [];
  const fetchFn = vi.fn<FetchFn>(async (url, init) => {
    const path = url.replace('https://api.test', '');
    const key = `${init?.method ?? 'GET'} ${path}`;
    const body =
      typeof init?.body === 'string' ? (JSON.parse(init.body) as Record<string, unknown>) : null;
    const auth = (init?.headers as Record<string, string>)['Authorization'];
    calls.push({ key, body, auth });
    const route = routes[key];
    if (route === undefined) {
      return json(404, { error: { code: 'NOT_FOUND', message: 'nope' } });
    }
    return route(body, init ?? {});
  });
  const api = new ApiClient({
    getBaseUrl: () => Promise.resolve('https://api.test'),
    fetchFn,
    timeoutMs: 1_000,
  });
  const storage = new MemoryStorage();
  const tokens = new TokenStore(storage, null);
  const events: AuthChangedEvent[] = [];
  const session = new SessionManager(tokens, api, {
    now: () => now,
    broadcast: (event) => events.push(event),
  });
  return {
    session,
    tokens,
    storage,
    calls,
    events,
    api,
    advance: (ms: number) => {
      now += ms;
    },
  };
}

const LOGIN = { tenant: 'acme', email: 'ana@acme.test', password: 'secret-pass' };

function getMe(api: ApiClient) {
  return (token: string) =>
    api.request({ method: 'GET', path: '/api/v1/auth/me', accessToken: token, parse: (b) => b });
}

describe('SessionManager', () => {
  it('stores tokens on login and exposes only the user', async () => {
    const t = setup({ 'POST /api/v1/auth/login': () => json(200, tokenBody('a1', 'r1')) });
    const info = await t.session.login(LOGIN);
    expect(info.user.displayName).toBe('Ana');
    expect(JSON.stringify(info)).not.toContain('a1');
    const stored = await t.tokens.get();
    expect(stored).toMatchObject({
      accessToken: 'a1',
      refreshToken: 'r1',
      accessTokenExpiresAt: 1_000_000 + 600_000,
    });
    expect(t.calls[0]!.body).toEqual(LOGIN);
    expect(t.events).toEqual([{ event: 'auth.changed', session: info }]);
    expect(JSON.stringify(await t.storage.get('eia.auth.tokens.v2'))).not.toContain('secret-pass');
  });

  it('propagates login errors without storing anything', async () => {
    const t = setup({ 'POST /api/v1/auth/login': () => apiError(401, 'INVALID_CREDENTIALS') });
    await expect(t.session.login(LOGIN)).rejects.toMatchObject({ code: 'INVALID_CREDENTIALS' });
    await expect(t.tokens.get()).resolves.toBeNull();
  });

  it('uses the stored access token while it is fresh', async () => {
    const t = setup({
      'POST /api/v1/auth/login': () => json(200, tokenBody('a1', 'r1')),
      'GET /api/v1/auth/me': () => json(200, USER_JSON),
    });
    await t.session.login(LOGIN);
    await t.session.authorized(getMe(t.api));
    expect(t.calls.at(-1)!.auth).toBe('Bearer a1');
    expect(t.calls.some((c) => c.key.includes('refresh'))).toBe(false);
  });

  it('refreshes lazily within 60s of expiry and stores the rotated refresh token', async () => {
    const t = setup({
      'POST /api/v1/auth/login': () => json(200, tokenBody('a1', 'r1')),
      'POST /api/v1/auth/refresh': (body) => {
        expect(body).toEqual({ refresh_token: 'r1' });
        return json(200, tokenBody('a2', 'r2'));
      },
      'GET /api/v1/auth/me': () => json(200, USER_JSON),
    });
    await t.session.login(LOGIN);
    t.advance(600_000 - 30_000); // 30 s before expiry
    await t.session.authorized(getMe(t.api));
    expect(t.calls.at(-1)!.auth).toBe('Bearer a2');
    expect(await t.tokens.get()).toMatchObject({ accessToken: 'a2', refreshToken: 'r2' });
  });

  it('single-flights concurrent refreshes', async () => {
    let release!: () => void;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    const t = setup({
      'POST /api/v1/auth/login': () => json(200, tokenBody('a1', 'r1')),
      'POST /api/v1/auth/refresh': async () => {
        await gate;
        return json(200, tokenBody('a2', 'r2'));
      },
      'GET /api/v1/auth/me': () => json(200, USER_JSON),
    });
    await t.session.login(LOGIN);
    t.advance(10 * 60_000);
    const all = Promise.all([1, 2, 3].map(() => t.session.authorized(getMe(t.api))));
    await new Promise((resolve) => setTimeout(resolve, 0));
    release();
    await all;
    expect(t.calls.filter((c) => c.key === 'POST /api/v1/auth/refresh')).toHaveLength(1);
    expect(
      t.calls.filter((c) => c.key === 'GET /api/v1/auth/me').every((c) => c.auth === 'Bearer a2'),
    ).toBe(true);
  });

  it('refreshes and retries exactly once on 401 TOKEN_EXPIRED', async () => {
    let meCalls = 0;
    const t = setup({
      'POST /api/v1/auth/login': () => json(200, tokenBody('a1', 'r1')),
      'POST /api/v1/auth/refresh': () => json(200, tokenBody('a2', 'r2')),
      'GET /api/v1/auth/me': () => {
        meCalls += 1;
        return meCalls === 1 ? apiError(401, 'TOKEN_EXPIRED') : json(200, USER_JSON);
      },
    });
    await t.session.login(LOGIN);
    await expect(t.session.authorized(getMe(t.api))).resolves.toEqual(USER_JSON);
    expect(meCalls).toBe(2);
    expect(t.calls.filter((c) => c.key === 'POST /api/v1/auth/refresh')).toHaveLength(1);
  });

  it('does not loop when the retried call is still unauthorized', async () => {
    const t = setup({
      'POST /api/v1/auth/login': () => json(200, tokenBody('a1', 'r1')),
      'POST /api/v1/auth/refresh': () => json(200, tokenBody('a2', 'r2')),
      'GET /api/v1/auth/me': () => apiError(401, 'TOKEN_EXPIRED'),
    });
    await t.session.login(LOGIN);
    await expect(t.session.authorized(getMe(t.api))).rejects.toBeInstanceOf(ApiError);
    expect(t.calls.filter((c) => c.key === 'GET /api/v1/auth/me')).toHaveLength(2);
    await expect(t.tokens.get()).resolves.toBeNull();
  });

  it.each(['REFRESH_TOKEN_INVALID', 'SESSION_REVOKED'])(
    'clears the session and broadcasts when refresh fails with %s',
    async (code) => {
      const t = setup({
        'POST /api/v1/auth/login': () => json(200, tokenBody('a1', 'r1')),
        'POST /api/v1/auth/refresh': () => apiError(401, code),
      });
      await t.session.login(LOGIN);
      t.advance(10 * 60_000);
      await expect(t.session.authorized(getMe(t.api))).rejects.toMatchObject({
        code: 'SESSION_EXPIRED',
      });
      await expect(t.tokens.get()).resolves.toBeNull();
      expect(t.events.at(-1)).toEqual({ event: 'auth.changed', session: null });
    },
  );

  it('keeps the session when refresh fails because of the network', async () => {
    const t = setup({
      'POST /api/v1/auth/login': () => json(200, tokenBody('a1', 'r1')),
      'POST /api/v1/auth/refresh': () => {
        throw new TypeError('Failed to fetch');
      },
    });
    await t.session.login(LOGIN);
    t.advance(10 * 60_000);
    await expect(t.session.authorized(getMe(t.api))).rejects.toMatchObject({
      code: 'NETWORK_ERROR',
    });
    await expect(t.tokens.get()).resolves.not.toBeNull();
  });

  it('clears on 401 SESSION_REVOKED from an API call', async () => {
    const t = setup({
      'POST /api/v1/auth/login': () => json(200, tokenBody('a1', 'r1')),
      'GET /api/v1/auth/me': () => apiError(401, 'SESSION_REVOKED'),
    });
    await t.session.login(LOGIN);
    await expect(t.session.authorized(getMe(t.api))).rejects.toMatchObject({
      code: 'SESSION_REVOKED',
    });
    await expect(t.session.getSession()).resolves.toBeNull();
    expect(t.events.at(-1)).toEqual({ event: 'auth.changed', session: null });
  });

  it('rejects authorized calls without a session', async () => {
    const t = setup({});
    await expect(t.session.authorized(getMe(t.api))).rejects.toMatchObject({
      status: 401,
      code: 'UNAUTHENTICATED',
    });
  });

  it('treats an expired refresh token as signed out', async () => {
    const t = setup({ 'POST /api/v1/auth/login': () => json(200, tokenBody('a1', 'r1')) });
    await t.session.login(LOGIN);
    t.advance(43_200_000 + 1);
    await expect(t.session.getSession()).resolves.toBeNull();
  });

  it('logs out server-side with the refresh token and clears locally', async () => {
    const t = setup({
      'POST /api/v1/auth/login': () => json(200, tokenBody('a1', 'r1')),
      'POST /api/v1/auth/logout': () => new Response(null, { status: 204 }),
    });
    await t.session.login(LOGIN);
    await t.session.logout();
    expect(t.calls.at(-1)).toMatchObject({
      key: 'POST /api/v1/auth/logout',
      body: { refresh_token: 'r1' },
    });
    await expect(t.tokens.get()).resolves.toBeNull();
    expect(t.events.at(-1)).toEqual({ event: 'auth.changed', session: null });
  });

  it('clears the local session even when the logout call fails', async () => {
    const t = setup({
      'POST /api/v1/auth/login': () => json(200, tokenBody('a1', 'r1')),
      'POST /api/v1/auth/logout': () => {
        throw new TypeError('offline');
      },
    });
    await t.session.login(LOGIN);
    await expect(t.session.logout()).resolves.toBeUndefined();
    await expect(t.tokens.get()).resolves.toBeNull();
  });
});
