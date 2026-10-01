import { MemoryStorage } from './storage';
import { TokenStore, type AuthTokens } from './token-store';

const TOKENS: AuthTokens = {
  accessToken: 'access.jwt.value',
  refreshToken: 'opaque-refresh',
  accessTokenExpiresAt: 2_000_000,
  refreshTokenExpiresAt: 9_000_000,
  user: {
    id: 'u1',
    email: 'analyst@acme.test',
    displayName: 'Ana Analyst',
    tenant: { id: 't1', slug: 'acme', name: 'Acme' },
    roles: ['analyst'],
    permissions: ['analytics.query'],
  },
};

describe('TokenStore', () => {
  it('restricts session storage to trusted contexts on init', async () => {
    const setAccessLevel = vi.fn(() => Promise.resolve());
    const store = new TokenStore(new MemoryStorage(), { setAccessLevel });
    await store.init();
    expect(setAccessLevel).toHaveBeenCalledWith({ accessLevel: 'TRUSTED_CONTEXTS' });
  });

  it('returns null when empty', async () => {
    const store = new TokenStore(new MemoryStorage(), null);
    await expect(store.get()).resolves.toBeNull();
  });

  it('stores, reads and clears tokens', async () => {
    const store = new TokenStore(new MemoryStorage(), null);
    await store.set(TOKENS);
    await expect(store.get()).resolves.toEqual(TOKENS);
    await store.clear();
    await expect(store.get()).resolves.toBeNull();
  });

  it('refuses to store malformed tokens', async () => {
    const store = new TokenStore(new MemoryStorage(), null);
    await expect(store.set({ ...TOKENS, accessToken: '' })).rejects.toThrow(TypeError);
    await expect(store.set({ ...TOKENS, accessTokenExpiresAt: Number.NaN })).rejects.toThrow(
      TypeError,
    );
  });

  it('discards corrupt stored entries', async () => {
    const storage = new MemoryStorage();
    await storage.set({ 'eia.auth.tokens.v2': { accessToken: 42 } });
    const store = new TokenStore(storage, null);
    await expect(store.get()).resolves.toBeNull();
    await expect(storage.get('eia.auth.tokens.v2')).resolves.toEqual({});
  });

  it('rejects a session without a valid user', async () => {
    const store = new TokenStore(new MemoryStorage(), null);
    await expect(
      store.set({ ...TOKENS, user: { ...TOKENS.user, tenant: { id: '', slug: 'x', name: 'x' } } }),
    ).rejects.toThrow(TypeError);
  });

  it('removes legacy token entries on init', async () => {
    const storage = new MemoryStorage();
    await storage.set({ 'eia.auth.tokens.v1': { accessToken: 'old' } });
    await new TokenStore(storage, null).init();
    await expect(storage.get('eia.auth.tokens.v1')).resolves.toEqual({});
  });

  it('checks access token expiry with skew', async () => {
    const store = new TokenStore(new MemoryStorage(), null);
    await expect(store.hasValidAccessToken(0)).resolves.toBe(false);
    await store.set(TOKENS);
    await expect(store.hasValidAccessToken(1_000_000)).resolves.toBe(true);
    await expect(store.hasValidAccessToken(1_990_000)).resolves.toBe(false);
  });
});
