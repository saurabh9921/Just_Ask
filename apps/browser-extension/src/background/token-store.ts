import type { UserInfo } from '../app/core/messaging/protocol';
import type { KeyValueStorage } from './storage';

const TOKEN_KEY = 'eia.auth.tokens.v2';
const LEGACY_KEYS = ['eia.auth.tokens.v1'];

/**
 * The authenticated session held by the background worker. The refresh token is
 * rotated by the server on every refresh; the access token is short-lived.
 */
export interface AuthTokens {
  readonly accessToken: string;
  readonly refreshToken: string;
  /** Epoch milliseconds at which the access token expires. */
  readonly accessTokenExpiresAt: number;
  /** Epoch milliseconds at which the refresh token expires. */
  readonly refreshTokenExpiresAt: number;
  readonly user: UserInfo;
}

function isNonEmptyString(value: unknown): value is string {
  return typeof value === 'string' && value !== '';
}

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === 'string');
}

export function isUserInfo(value: unknown): value is UserInfo {
  if (typeof value !== 'object' || value === null) {
    return false;
  }
  const v = value as Record<string, unknown>;
  const tenant = v['tenant'] as Record<string, unknown> | null | undefined;
  return (
    isNonEmptyString(v['id']) &&
    isNonEmptyString(v['email']) &&
    isNonEmptyString(v['displayName']) &&
    typeof tenant === 'object' &&
    tenant !== null &&
    isNonEmptyString(tenant['id']) &&
    isNonEmptyString(tenant['slug']) &&
    isNonEmptyString(tenant['name']) &&
    isStringArray(v['roles']) &&
    isStringArray(v['permissions'])
  );
}

function isAuthTokens(value: unknown): value is AuthTokens {
  if (typeof value !== 'object' || value === null) {
    return false;
  }
  const v = value as Record<string, unknown>;
  return (
    isNonEmptyString(v['accessToken']) &&
    isNonEmptyString(v['refreshToken']) &&
    typeof v['accessTokenExpiresAt'] === 'number' &&
    Number.isFinite(v['accessTokenExpiresAt']) &&
    typeof v['refreshTokenExpiresAt'] === 'number' &&
    Number.isFinite(v['refreshTokenExpiresAt']) &&
    isUserInfo(v['user'])
  );
}

export interface SessionAccessControl {
  setAccessLevel(options: { accessLevel: 'TRUSTED_CONTEXTS' }): Promise<void>;
}

/**
 * Token storage backed by `chrome.storage.session`.
 *
 * - Memory-only: never written to disk, cleared when the browser closes.
 * - Restricted to trusted extension contexts (background + extension pages), so
 *   content scripts can never read it.
 * - Only the background worker reads or writes tokens; the side panel UI never
 *   receives them.
 */
export class TokenStore {
  constructor(
    private readonly storage: KeyValueStorage,
    private readonly accessControl: SessionAccessControl | null,
  ) {}

  /** Locks the session storage area to trusted contexts. Call once at worker start-up. */
  async init(): Promise<void> {
    await this.accessControl?.setAccessLevel({ accessLevel: 'TRUSTED_CONTEXTS' });
    for (const key of LEGACY_KEYS) {
      await this.storage.remove(key);
    }
  }

  async get(): Promise<AuthTokens | null> {
    const stored = (await this.storage.get(TOKEN_KEY))[TOKEN_KEY];
    if (!isAuthTokens(stored)) {
      if (stored !== undefined) {
        // Corrupt or tampered entry: drop it rather than trusting it.
        await this.storage.remove(TOKEN_KEY);
      }
      return null;
    }
    return stored;
  }

  async set(tokens: AuthTokens): Promise<void> {
    if (!isAuthTokens(tokens)) {
      throw new TypeError('Refusing to store malformed auth tokens.');
    }
    await this.storage.set({ [TOKEN_KEY]: tokens });
  }

  async clear(): Promise<void> {
    await this.storage.remove(TOKEN_KEY);
  }

  /** True when a token exists and has not expired (with a safety skew). */
  async hasValidAccessToken(nowMs: number = Date.now(), skewMs = 30_000): Promise<boolean> {
    const tokens = await this.get();
    return tokens !== null && tokens.accessTokenExpiresAt - skewMs > nowMs;
  }
}
