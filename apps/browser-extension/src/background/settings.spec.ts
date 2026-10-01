import { SettingsStore, SettingsValidationError, validateApiBaseUrl } from './settings';
import { MemoryStorage } from './storage';

describe('validateApiBaseUrl', () => {
  it.each([
    ['https://erp.example.com', 'https://erp.example.com'],
    ['https://erp.example.com/', 'https://erp.example.com'],
    ['https://erp.example.com:8443/api/', 'https://erp.example.com:8443/api'],
    ['  https://erp.example.com  ', 'https://erp.example.com'],
    ['http://localhost:8080', 'http://localhost:8080'],
    ['http://127.0.0.1:8080/', 'http://127.0.0.1:8080'],
    ['http://[::1]:8080', 'http://[::1]:8080'],
  ])('accepts %s', (input, expected) => {
    expect(validateApiBaseUrl(input)).toEqual({ valid: true, normalized: expected });
  });

  it.each([
    ['', 'required'],
    [undefined, 'required'],
    [42, 'required'],
    ['erp.example.com', 'valid absolute URL'],
    ['http://erp.example.com', 'HTTPS'],
    ['http://localhost.evil.com', 'HTTPS'],
    ['ftp://erp.example.com', 'HTTPS'],
    ['javascript:alert(1)', 'HTTPS'],
    ['https://user:pass@erp.example.com', 'credentials'],
    ['https://erp.example.com/?token=abc', 'query string'],
    ['https://erp.example.com/#x', 'fragment'],
    [`https://erp.example.com/${'a'.repeat(3000)}`, 'too long'],
  ])('rejects %j', (input, reason) => {
    const result = validateApiBaseUrl(input);
    expect(result.valid).toBe(false);
    expect(!result.valid && result.reason).toContain(reason);
  });
});

describe('SettingsStore', () => {
  const defaults = { apiBaseUrl: 'http://localhost:8080' };

  it('returns defaults when nothing is stored', async () => {
    const store = new SettingsStore(new MemoryStorage(), defaults);
    await expect(store.get()).resolves.toEqual(defaults);
  });

  it('round-trips a valid value', async () => {
    const store = new SettingsStore(new MemoryStorage(), defaults);
    await store.set({ apiBaseUrl: 'https://insight.example.com/' });
    await expect(store.get()).resolves.toEqual({ apiBaseUrl: 'https://insight.example.com' });
  });

  it('throws SettingsValidationError for invalid input and keeps the old value', async () => {
    const store = new SettingsStore(new MemoryStorage(), defaults);
    await expect(store.set({ apiBaseUrl: 'http://remote.example.com' })).rejects.toBeInstanceOf(
      SettingsValidationError,
    );
    await expect(store.set(null)).rejects.toBeInstanceOf(SettingsValidationError);
    await expect(store.get()).resolves.toEqual(defaults);
  });

  it('falls back to defaults when stored data was tampered with', async () => {
    const storage = new MemoryStorage();
    await storage.set({ 'eia.settings.v1': { apiBaseUrl: 'http://attacker.example' } });
    const store = new SettingsStore(storage, defaults);
    await expect(store.get()).resolves.toEqual(defaults);
  });
});
