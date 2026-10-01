import type { AuthChangedEvent, LoginRequest, SessionInfo } from '../app/core/messaging/protocol';
import { ApiError, parseTokenResponse, type ApiClient, type TokenResponse } from './api-client';
import type { AuthTokens, TokenStore } from './token-store';

/** Refresh the access token when it expires within this window. */
export const REFRESH_SKEW_MS = 60_000;

/** 401 codes meaning the session is over and must be discarded. */
const SESSION_ENDING_CODES = new Set([
  'REFRESH_TOKEN_INVALID',
  'SESSION_REVOKED',
  'UNAUTHENTICATED',
]);

export interface SessionManagerOptions {
  readonly now?: () => number;
  /** Pushes `auth.changed` to extension pages. Must never throw. */
  readonly broadcast: (event: AuthChangedEvent) => void;
  readonly refreshSkewMs?: number;
}

/**
 * Owns the authenticated session inside the background service worker.
 *
 * - Tokens live only in {@link TokenStore} (`chrome.storage.session`); callers of
 *   the public API receive {@link SessionInfo} (the user), never tokens.
 * - Access tokens are refreshed lazily shortly before expiry. Refreshes are
 *   single-flight: concurrent callers share one in-flight refresh, which matters
 *   because the server rotates refresh tokens and treats reuse as theft.
 * - A 401 `TOKEN_EXPIRED` from an API call triggers exactly one refresh + retry.
 * - A rejected refresh (revoked/invalid) clears the session and broadcasts
 *   `auth.changed` so the side panel returns to sign-in.
 */
export class SessionManager {
  private refreshing: Promise<AuthTokens> | null = null;
  private readonly now: () => number;
  private readonly skewMs: number;

  constructor(
    private readonly tokens: TokenStore,
    private readonly api: ApiClient,
    private readonly options: SessionManagerOptions,
  ) {
    this.now = options.now ?? (() => Date.now());
    this.skewMs = options.refreshSkewMs ?? REFRESH_SKEW_MS;
  }

  async login(request: LoginRequest): Promise<SessionInfo> {
    const response = await this.api.request({
      method: 'POST',
      path: '/api/v1/auth/login',
      body: { tenant: request.tenant, email: request.email, password: request.password },
      parse: parseTokenResponse,
    });
    const stored = await this.store(response);
    const session = { user: stored.user };
    this.options.broadcast({ event: 'auth.changed', session });
    return session;
  }

  /** Current session, or null. An expired refresh token ends the session. */
  async getSession(): Promise<SessionInfo | null> {
    const tokens = await this.tokens.get();
    if (tokens === null) {
      return null;
    }
    if (tokens.refreshTokenExpiresAt <= this.now()) {
      await this.tokens.clear();
      return null;
    }
    return { user: tokens.user };
  }

  /**
   * Secure logout: revoke server-side on a best-effort basis, and always clear the
   * local session even if the network call fails.
   */
  async logout(): Promise<void> {
    const tokens = await this.tokens.get();
    try {
      if (tokens !== null) {
        await this.api.request({
          method: 'POST',
          path: '/api/v1/auth/logout',
          body: { refresh_token: tokens.refreshToken },
        });
      }
    } catch {
      // Best effort: the refresh token also expires server-side.
    } finally {
      this.refreshing = null;
      await this.tokens.clear();
      this.options.broadcast({ event: 'auth.changed', session: null });
    }
  }

  /**
   * Runs an authenticated API call, refreshing the access token when needed and
   * retrying exactly once on 401 `TOKEN_EXPIRED`.
   */
  async authorized<T>(call: (accessToken: string) => Promise<T>): Promise<T> {
    const token = await this.validAccessToken();
    try {
      return await call(token);
    } catch (error: unknown) {
      if (!(error instanceof ApiError) || error.status !== 401) {
        throw error;
      }
      if (error.code !== 'TOKEN_EXPIRED') {
        if (SESSION_ENDING_CODES.has(error.code)) {
          await this.endSession();
        }
        throw error;
      }
    }
    const refreshed = await this.refresh();
    try {
      return await call(refreshed.accessToken);
    } catch (error: unknown) {
      if (error instanceof ApiError && error.status === 401) {
        await this.endSession();
      }
      throw error;
    }
  }

  /**
   * An access token for channels that authenticate in-band (the analytics WebSocket).
   * `forceRefresh` rotates it after the server reported `TOKEN_EXPIRED`.
   */
  async accessToken(forceRefresh = false): Promise<string> {
    if (!forceRefresh) {
      return this.validAccessToken();
    }
    return (await this.refresh()).accessToken;
  }

  private async validAccessToken(): Promise<string> {
    const tokens = await this.tokens.get();
    if (tokens === null) {
      throw new ApiError(401, 'UNAUTHENTICATED', 'Sign in to continue.');
    }
    if (tokens.accessTokenExpiresAt - this.skewMs > this.now()) {
      return tokens.accessToken;
    }
    return (await this.refresh()).accessToken;
  }

  /** Single-flight refresh. */
  private refresh(): Promise<AuthTokens> {
    this.refreshing ??= this.doRefresh().finally(() => {
      this.refreshing = null;
    });
    return this.refreshing;
  }

  private async doRefresh(): Promise<AuthTokens> {
    const current = await this.tokens.get();
    if (current === null || current.refreshTokenExpiresAt <= this.now()) {
      await this.endSession();
      throw new ApiError(401, 'SESSION_EXPIRED', 'Your session has ended. Sign in again.');
    }
    try {
      const response = await this.api.request({
        method: 'POST',
        path: '/api/v1/auth/refresh',
        body: { refresh_token: current.refreshToken },
        parse: parseTokenResponse,
      });
      return await this.store(response);
    } catch (error: unknown) {
      if (error instanceof ApiError && (error.status === 401 || error.status === 403)) {
        // Rotated/revoked/invalid refresh token: the session is over.
        await this.endSession();
        throw new ApiError(401, 'SESSION_EXPIRED', 'Your session has ended. Sign in again.');
      }
      // Network/5xx errors keep the session so a later attempt can succeed.
      throw error;
    }
  }

  private async endSession(): Promise<void> {
    const hadSession = (await this.tokens.get()) !== null;
    await this.tokens.clear();
    if (hadSession) {
      this.options.broadcast({ event: 'auth.changed', session: null });
    }
  }

  private async store(response: TokenResponse): Promise<AuthTokens> {
    const now = this.now();
    const tokens: AuthTokens = {
      accessToken: response.accessToken,
      refreshToken: response.refreshToken,
      accessTokenExpiresAt: now + response.expiresIn * 1000,
      refreshTokenExpiresAt: now + response.refreshExpiresIn * 1000,
      user: response.user,
    };
    await this.tokens.set(tokens);
    return tokens;
  }
}
