import type { ExtensionSettings } from '../app/core/messaging/protocol';
import { validateApiBaseUrl } from '../app/core/settings/api-url';
import type { KeyValueStorage } from './storage';

export { validateApiBaseUrl };

const SETTINGS_KEY = 'eia.settings.v1';

export class SettingsValidationError extends Error {
  override readonly name = 'SettingsValidationError';
}

/** Persists non-secret user settings (never tokens) in `chrome.storage.local`. */
export class SettingsStore {
  constructor(
    private readonly storage: KeyValueStorage,
    private readonly defaults: ExtensionSettings,
  ) {}

  async get(): Promise<ExtensionSettings> {
    const stored = (await this.storage.get(SETTINGS_KEY))[SETTINGS_KEY];
    if (typeof stored === 'object' && stored !== null) {
      const candidate = (stored as Record<string, unknown>)['apiBaseUrl'];
      const result = validateApiBaseUrl(candidate);
      if (result.valid) {
        return { apiBaseUrl: result.normalized };
      }
    }
    return this.defaults;
  }

  async set(input: unknown): Promise<ExtensionSettings> {
    const candidate =
      typeof input === 'object' && input !== null
        ? (input as Record<string, unknown>)['apiBaseUrl']
        : undefined;
    const result = validateApiBaseUrl(candidate);
    if (!result.valid) {
      throw new SettingsValidationError(result.reason);
    }
    const settings: ExtensionSettings = { apiBaseUrl: result.normalized };
    await this.storage.set({ [SETTINGS_KEY]: settings });
    return settings;
  }
}
