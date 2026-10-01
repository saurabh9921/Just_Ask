import type {
  ConnectionSummary,
  ExtensionSettings,
  HealthCheckResult,
  SessionInfo,
} from '../app/core/messaging/protocol';
import type { FeedbackInput } from '../app/core/messaging/analytics-types';
import { ApiError } from './api-client';
import { PreferencesStore } from './preferences';
import { createMessageRouter, type AuthPort, type RouterDependencies } from './router';
import { SettingsStore } from './settings';
import { MemoryStorage } from './storage';

const EXTENSION_ID = 'abcdefghijklmnopabcdefghijklmnop';
const OWN_SENDER = { id: EXTENSION_ID, url: `chrome-extension://${EXTENSION_ID}/index.html` };

const HEALTHY: HealthCheckResult = {
  status: 'ok',
  latencyMs: 12,
  checks: { database: 'ok' },
  checkedAt: '2026-01-01T00:00:00.000Z',
};

const SESSION: SessionInfo = {
  user: {
    id: 'user-1',
    email: 'ana@acme.test',
    displayName: 'Ana',
    tenant: { id: 't-1', slug: 'acme', name: 'Acme' },
    roles: ['analyst'],
    permissions: ['analytics.query'],
  },
};

const CONNECTION: ConnectionSummary = {
  id: 'conn-1',
  name: 'ERP prod',
  connectorType: 'postgresql',
  host: 'db',
  port: 5432,
  databaseName: 'erp',
  schemaName: 'analytics',
  status: 'ok',
  lastTestedAt: null,
  readOnly: true,
  hasSecret: true,
};

function fakeAuth(session: SessionInfo | null = SESSION): AuthPort {
  return {
    login: vi.fn(() => Promise.resolve(SESSION)),
    logout: vi.fn(() => Promise.resolve()),
    getSession: vi.fn(() => Promise.resolve(session)),
  };
}

function makeRouter(overrides: Partial<RouterDependencies> = {}) {
  const store = new SettingsStore(new MemoryStorage(), { apiBaseUrl: 'http://localhost:8080' });
  const preferences = new PreferencesStore(new MemoryStorage());
  const deps: RouterDependencies = {
    extensionId: EXTENSION_ID,
    getSettings: () => store.get(),
    setSettings: (input) => store.set(input),
    checkHealth: vi.fn(() => Promise.resolve(HEALTHY)),
    auth: fakeAuth(),
    connections: {
      list: vi.fn(() => Promise.resolve([CONNECTION])),
      test: vi.fn(() =>
        Promise.resolve({
          ok: true,
          latencyMs: 4,
          serverVersion: '18.6',
          errorCode: null,
          message: null,
        }),
      ),
      create: vi.fn(() => Promise.reject(new Error('not used'))),
      update: vi.fn(() => Promise.reject(new Error('not used'))),
      remove: vi.fn(() => Promise.resolve()),
      sync: vi.fn(() => Promise.reject(new Error('not used'))),
      index: vi.fn(() => Promise.reject(new Error('not used'))),
      status: vi.fn(() => Promise.reject(new Error('not used'))),
    },
    preferences,
    history: {
      list: vi.fn(() => Promise.resolve({ items: [], total: 0, limit: 20, offset: 0 })),
      get: vi.fn(() => Promise.reject(new Error('not used'))),
      setSaved: vi.fn(() => Promise.reject(new Error('not used'))),
      delete: vi.fn(() => Promise.resolve()),
      listConversations: vi.fn(() =>
        Promise.resolve({ items: [], total: 0, limit: 20, offset: 0 }),
      ),
      getConversation: vi.fn(() => Promise.reject(new Error('not used'))),
      setConversationSaved: vi.fn(() => Promise.reject(new Error('not used'))),
      deleteConversation: vi.fn(() => Promise.resolve()),
      submitFeedback: vi.fn((input: FeedbackInput) =>
        Promise.resolve({
          queryId: input.queryId,
          rating: input.rating,
          categories: [...input.categories],
          remembered: input.rating === 1,
        }),
      ),
    },
    exports: {
      create: vi.fn(() => Promise.reject(new Error('not used'))),
      get: vi.fn(() => Promise.reject(new Error('not used'))),
      download: vi.fn(() => Promise.reject(new Error('not used'))),
    },
    logger: { warn: vi.fn(), error: vi.fn() },
    ...overrides,
  };
  return { router: createMessageRouter(deps), deps };
}

describe('createMessageRouter', () => {
  it('rejects messages from another extension', async () => {
    const { router, deps } = makeRouter();
    const result = await router({ type: 'settings.get' }, { id: 'someotherextensionid' });
    expect(result).toEqual({
      ok: false,
      error: { code: 'FORBIDDEN_SENDER', message: 'Sender is not permitted.' },
    });
    expect(deps.logger?.warn).toHaveBeenCalled();
  });

  it('rejects messages without a sender id (e.g. web pages)', async () => {
    const { router } = makeRouter();
    const result = await router({ type: 'settings.get' }, { url: 'https://evil.example' });
    expect(result.ok).toBe(false);
    expect(!result.ok && result.error.code).toBe('FORBIDDEN_SENDER');
  });

  it('rejects unknown message types', async () => {
    const { router } = makeRouter();
    const result = await router({ type: 'db.execute', payload: 'DROP TABLE x' }, OWN_SENDER);
    expect(!result.ok && result.error.code).toBe('UNKNOWN_MESSAGE');
  });

  it.each([null, 'settings.get', 42, ['settings.get'], {}])(
    'rejects malformed message %j',
    async (message) => {
      const { router } = makeRouter();
      const result = await router(message, OWN_SENDER);
      expect(result.ok).toBe(false);
    },
  );

  it('returns default settings', async () => {
    const { router } = makeRouter();
    const result = await router({ type: 'settings.get' }, OWN_SENDER);
    expect(result).toEqual({ ok: true, data: { apiBaseUrl: 'http://localhost:8080' } });
  });

  it('persists valid settings and normalises the URL', async () => {
    const { router } = makeRouter();
    const set = await router(
      { type: 'settings.set', payload: { apiBaseUrl: 'https://erp.example.com/insight/' } },
      OWN_SENDER,
    );
    expect(set).toEqual({ ok: true, data: { apiBaseUrl: 'https://erp.example.com/insight' } });
    const get = await router({ type: 'settings.get' }, OWN_SENDER);
    expect(get.ok && (get.data as ExtensionSettings).apiBaseUrl).toBe(
      'https://erp.example.com/insight',
    );
  });

  it('returns VALIDATION_ERROR for an insecure remote URL', async () => {
    const { router } = makeRouter();
    const result = await router(
      { type: 'settings.set', payload: { apiBaseUrl: 'http://erp.example.com' } },
      OWN_SENDER,
    );
    expect(!result.ok && result.error.code).toBe('VALIDATION_ERROR');
  });

  it('runs the health check against the configured API URL', async () => {
    const checkHealth = vi.fn(() => Promise.resolve(HEALTHY));
    const { router } = makeRouter({ checkHealth });
    const result = await router({ type: 'health.check' }, OWN_SENDER);
    expect(result).toEqual({ ok: true, data: HEALTHY });
    expect(checkHealth).toHaveBeenCalledWith('http://localhost:8080');
  });

  it('hides internal error details', async () => {
    const { router } = makeRouter({
      checkHealth: () => Promise.reject(new Error('secret internal detail')),
    });
    const result = await router({ type: 'health.check' }, OWN_SENDER);
    expect(result).toEqual({
      ok: false,
      error: { code: 'INTERNAL_ERROR', message: 'The request could not be completed.' },
    });
  });

  describe('auth messages', () => {
    const LOGIN = { tenant: ' ACME ', email: ' ana@acme.test ', password: 'pw-123456' };

    it('validates, normalises and forwards a login, then remembers non-secret hints', async () => {
      const { router, deps } = makeRouter();
      const result = await router({ type: 'auth.login', payload: LOGIN }, OWN_SENDER);
      expect(result).toEqual({ ok: true, data: SESSION });
      expect(deps.auth.login).toHaveBeenCalledWith({
        tenant: 'acme',
        email: 'ana@acme.test',
        password: 'pw-123456',
      });
      const hints = await router({ type: 'auth.loginHints' }, OWN_SENDER);
      expect(hints).toEqual({ ok: true, data: { tenant: 'acme', email: 'ana@acme.test' } });
      expect(JSON.stringify(hints)).not.toContain('pw-123456');
    });

    it.each([
      { ...LOGIN, tenant: 'bad tenant!' },
      { ...LOGIN, email: 'no-at-sign' },
      { ...LOGIN, password: '' },
      { ...LOGIN, password: 42 },
      null,
    ])('rejects invalid login payload %j without calling the API', async (payload) => {
      const { router, deps } = makeRouter();
      const result = await router({ type: 'auth.login', payload }, OWN_SENDER);
      expect(!result.ok && result.error.code).toBe('VALIDATION_ERROR');
      expect(deps.auth.login).not.toHaveBeenCalled();
    });

    it('maps INVALID_CREDENTIALS and never logs the password', async () => {
      const login = vi.fn(() =>
        Promise.reject(new ApiError(401, 'INVALID_CREDENTIALS', 'Invalid credentials.', 'req_1')),
      );
      const { router, deps } = makeRouter({ auth: { ...fakeAuth(), login } });
      const result = await router({ type: 'auth.login', payload: LOGIN }, OWN_SENDER);
      expect(result).toEqual({
        ok: false,
        error: { code: 'INVALID_CREDENTIALS', message: 'Invalid credentials.', requestId: 'req_1' },
      });
      const logged = JSON.stringify([
        vi.mocked(deps.logger!.error).mock.calls,
        vi.mocked(deps.logger!.warn).mock.calls,
      ]);
      expect(logged).not.toContain('pw-123456');
    });

    it('passes Retry-After through for rate limiting', async () => {
      const login = vi.fn(() =>
        Promise.reject(new ApiError(429, 'RATE_LIMITED', 'Slow down.', null, 30)),
      );
      const { router } = makeRouter({ auth: { ...fakeAuth(), login } });
      const result = await router({ type: 'auth.login', payload: LOGIN }, OWN_SENDER);
      expect(!result.ok && result.error).toEqual({
        code: 'RATE_LIMITED',
        message: 'Slow down.',
        retryAfterSeconds: 30,
      });
    });

    it('returns the session (user only) and handles logout', async () => {
      const { router, deps } = makeRouter();
      await expect(router({ type: 'auth.session' }, OWN_SENDER)).resolves.toEqual({
        ok: true,
        data: SESSION,
      });
      await expect(router({ type: 'auth.logout' }, OWN_SENDER)).resolves.toEqual({
        ok: true,
        data: null,
      });
      expect(deps.auth.logout).toHaveBeenCalledOnce();
    });

    it('hides 5xx server messages', async () => {
      const list = vi.fn(() =>
        Promise.reject(new ApiError(500, 'INTERNAL_ERROR', 'stack trace here')),
      );
      const { router } = makeRouter({
        connections: {
          list,
          test: vi.fn(),
          create: vi.fn(),
          update: vi.fn(),
          remove: vi.fn(),
          sync: vi.fn(),
          index: vi.fn(),
          status: vi.fn(),
        },
      });
      const result = await router({ type: 'connections.list' }, OWN_SENDER);
      expect(!result.ok && result.error.code).toBe('API_ERROR');
      expect(JSON.stringify(result)).not.toContain('stack trace');
    });

    it('maps revoked sessions to SESSION_EXPIRED', async () => {
      const list = vi.fn(() => Promise.reject(new ApiError(401, 'SESSION_REVOKED', 'Revoked.')));
      const { router } = makeRouter({
        connections: {
          list,
          test: vi.fn(),
          create: vi.fn(),
          update: vi.fn(),
          remove: vi.fn(),
          sync: vi.fn(),
          index: vi.fn(),
          status: vi.fn(),
        },
      });
      const result = await router({ type: 'connections.list' }, OWN_SENDER);
      expect(!result.ok && result.error.code).toBe('SESSION_EXPIRED');
    });
  });

  describe('connections and workspace messages', () => {
    it('lists and tests connections', async () => {
      const { router, deps } = makeRouter();
      await expect(router({ type: 'connections.list' }, OWN_SENDER)).resolves.toEqual({
        ok: true,
        data: [CONNECTION],
      });
      const test = await router(
        { type: 'connections.test', payload: { id: 'conn-1' } },
        OWN_SENDER,
      );
      expect(test.ok).toBe(true);
      expect(deps.connections.test).toHaveBeenCalledWith('conn-1');
    });

    it.each(['../../admin', 'a/b', '', 'x'.repeat(65)])(
      'rejects unsafe connection id %j',
      async (id) => {
        const { router, deps } = makeRouter();
        const result = await router({ type: 'connections.test', payload: { id } }, OWN_SENDER);
        expect(!result.ok && result.error.code).toBe('VALIDATION_ERROR');
        expect(deps.connections.test).not.toHaveBeenCalled();
      },
    );

    it('stores the selected connection per user', async () => {
      const { router } = makeRouter();
      await expect(router({ type: 'workspace.get' }, OWN_SENDER)).resolves.toEqual({
        ok: true,
        data: { connectionId: null },
      });
      await router({ type: 'workspace.set', payload: { connectionId: 'conn-1' } }, OWN_SENDER);
      await expect(router({ type: 'workspace.get' }, OWN_SENDER)).resolves.toEqual({
        ok: true,
        data: { connectionId: 'conn-1' },
      });
    });

    it('requires a session for workspace messages', async () => {
      const { router } = makeRouter({ auth: fakeAuth(null) });
      const result = await router({ type: 'workspace.get' }, OWN_SENDER);
      expect(!result.ok && result.error.code).toBe('UNAUTHENTICATED');
    });

    it('does not leak a workspace selection across users', async () => {
      const storage = new MemoryStorage();
      const preferences = new PreferencesStore(storage);
      await preferences.setWorkspace('other-user', { connectionId: 'conn-9' });
      const { router } = makeRouter({ preferences });
      await expect(router({ type: 'workspace.get' }, OWN_SENDER)).resolves.toEqual({
        ok: true,
        data: { connectionId: null },
      });
    });
  });
});
