import type { HealthCheckResult } from '../app/core/messaging/protocol';

export type FetchFn = (input: string, init?: RequestInit) => Promise<Response>;

export interface HealthCheckOptions {
  readonly fetchFn: FetchFn;
  readonly timeoutMs: number;
  readonly now?: () => number;
}

const MAX_CHECKS = 20;
const MAX_CHECK_VALUE_LENGTH = 64;

/**
 * Extracts per-dependency statuses from the readiness body. Accepts either
 * `{checks: {db: "ok"}}` or `{checks: {db: {status: "ok", ...}}}`. Anything else is
 * ignored: the payload is untrusted and only short status strings are kept.
 */
export function parseChecks(body: unknown): Record<string, string> {
  if (typeof body !== 'object' || body === null) {
    return {};
  }
  const checks = (body as Record<string, unknown>)['checks'];
  if (typeof checks !== 'object' || checks === null || Array.isArray(checks)) {
    return {};
  }
  const result: Record<string, string> = {};
  for (const [name, value] of Object.entries(checks).slice(0, MAX_CHECKS)) {
    let status: unknown = value;
    if (typeof value === 'object' && value !== null) {
      status = (value as Record<string, unknown>)['status'];
    }
    if (typeof status === 'string') {
      result[name.slice(0, MAX_CHECK_VALUE_LENGTH)] = status.slice(0, MAX_CHECK_VALUE_LENGTH);
    }
  }
  return result;
}

/**
 * Calls `GET {apiBaseUrl}/health/ready`.
 *
 * - 2xx                    -> `ok`
 * - any other HTTP status  -> `degraded` (API reachable, a dependency is unhealthy)
 * - network error/timeout  -> `offline`
 */
export async function checkHealth(
  apiBaseUrl: string,
  options: HealthCheckOptions,
): Promise<HealthCheckResult> {
  const now = options.now ?? (() => Date.now());
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), options.timeoutMs);
  const started = now();
  try {
    const response = await options.fetchFn(`${apiBaseUrl}/health/ready`, {
      method: 'GET',
      signal: controller.signal,
      credentials: 'omit',
      cache: 'no-store',
      headers: { Accept: 'application/json' },
    });
    const latencyMs = Math.max(0, Math.round(now() - started));
    let body: unknown = null;
    try {
      body = await response.json();
    } catch {
      body = null;
    }
    const checkedAt = new Date(now()).toISOString();
    const checks = parseChecks(body);
    if (response.ok) {
      return { status: 'ok', latencyMs, checks, checkedAt };
    }
    return {
      status: 'degraded',
      latencyMs,
      checks,
      checkedAt,
      detail: `API readiness check returned HTTP ${response.status}.`,
    };
  } catch {
    const timedOut = controller.signal.aborted;
    return {
      status: 'offline',
      latencyMs: null,
      checks: {},
      checkedAt: new Date(now()).toISOString(),
      detail: timedOut
        ? `API did not respond within ${options.timeoutMs} ms.`
        : 'API is unreachable.',
    };
  } finally {
    clearTimeout(timer);
  }
}
