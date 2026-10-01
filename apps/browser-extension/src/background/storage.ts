/**
 * Minimal key/value storage contract. Implemented by `chrome.storage.*` areas in
 * production and by an in-memory map in tests, so background logic never depends
 * on the `chrome` global directly.
 */
export interface KeyValueStorage {
  get(key: string): Promise<Record<string, unknown>>;
  set(items: Record<string, unknown>): Promise<void>;
  remove(key: string): Promise<void>;
}

/** Adapts a `chrome.storage.StorageArea` to {@link KeyValueStorage}. */
export function fromChromeStorageArea(area: chrome.storage.StorageArea): KeyValueStorage {
  return {
    get: async (key) => area.get(key),
    set: async (items) => area.set(items),
    remove: async (key) => area.remove(key),
  };
}

/** In-memory storage, used by tests and as a safe fallback. */
export class MemoryStorage implements KeyValueStorage {
  private readonly data = new Map<string, unknown>();

  get(key: string): Promise<Record<string, unknown>> {
    return Promise.resolve(
      this.data.has(key) ? { [key]: structuredClone(this.data.get(key)) } : {},
    );
  }

  set(items: Record<string, unknown>): Promise<void> {
    for (const [key, value] of Object.entries(items)) {
      this.data.set(key, structuredClone(value));
    }
    return Promise.resolve();
  }

  remove(key: string): Promise<void> {
    this.data.delete(key);
    return Promise.resolve();
  }
}
