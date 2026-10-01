/**
 * Build-time, non-secret configuration.
 *
 * Never place credentials, API keys or tokens in environment files: everything in
 * them ships inside the extension bundle and is readable by anyone who installs it.
 */
export interface Environment {
  readonly production: boolean;
  /** Default API base URL used until the user saves one in Settings. */
  readonly defaultApiBaseUrl: string;
  /** Interval for the connection status indicator. */
  readonly healthPollIntervalMs: number;
  /** Timeout for the background health check request. */
  readonly healthCheckTimeoutMs: number;
  /** Timeout for side panel -> background worker messages. */
  readonly bridgeTimeoutMs: number;
  /** Timeout for API calls made by the background worker. */
  readonly apiTimeoutMs: number;
}
