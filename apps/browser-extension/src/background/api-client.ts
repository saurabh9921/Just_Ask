import {
  TLS_MODES,
  type ConnectionHealth,
  type ConnectionSummary,
  type ConnectionTestResult,
  type DataSourceStatus,
  type TlsModeName,
  type UserInfo,
} from '../app/core/messaging/protocol';

export type FetchFn = (input: string, init?: RequestInit) => Promise<Response>;

/** Typed failure of an API call. `status` is 0 for network errors and timeouts. */
export class ApiError extends Error {
  override readonly name = 'ApiError';

  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly requestId: string | null = null,
    readonly retryAfterSeconds: number | null = null,
  ) {
    super(message);
  }
}

/** Response bodies are untrusted: every parser validates shape and throws on mismatch. */
export class ApiResponseError extends ApiError {
  constructor(what: string) {
    super(502, 'INVALID_RESPONSE', `The server returned an unexpected ${what} response.`);
  }
}

export interface ApiClientOptions {
  readonly getBaseUrl: () => Promise<string>;
  readonly fetchFn: FetchFn;
  readonly timeoutMs: number;
  readonly randomBytes?: (length: number) => Uint8Array;
}

export interface ApiRequest<T> {
  readonly method: 'GET' | 'POST' | 'PATCH' | 'DELETE';
  readonly path: string;
  readonly body?: unknown;
  readonly accessToken?: string;
  /** Validates the JSON body. Omit for endpoints returning 204. */
  readonly parse?: (body: unknown) => T;
  /** Overrides the client timeout (long-running analytics requests). */
  readonly timeoutMs?: number;
  /** Extra request headers (e.g. `Idempotency-Key`). Never credentials. */
  readonly headers?: Readonly<Record<string, string>>;
}

const MAX_MESSAGE_LENGTH = 300;
const MAX_RETRY_AFTER_SECONDS = 3600;

function defaultRandomBytes(length: number): Uint8Array {
  return crypto.getRandomValues(new Uint8Array(length));
}

export function newRequestId(randomBytes: (length: number) => Uint8Array = defaultRandomBytes) {
  const hex = Array.from(randomBytes(12), (b) => b.toString(16).padStart(2, '0')).join('');
  return `ext_${hex}`;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function parseRetryAfter(header: string | null): number | null {
  if (header === null || !/^\d{1,6}$/.test(header.trim())) {
    return null;
  }
  return Math.min(Number(header.trim()), MAX_RETRY_AFTER_SECONDS);
}

/** Converts a non-2xx response with the standard error envelope into an ApiError. */
export async function toApiError(response: Response): Promise<ApiError> {
  let code = `HTTP_${response.status}`;
  let message = 'The request could not be completed.';
  let requestId = response.headers.get('x-request-id');
  try {
    const body: unknown = await response.json();
    const error = isRecord(body) ? body['error'] : undefined;
    if (isRecord(error)) {
      if (typeof error['code'] === 'string' && /^[A-Z0-9_]{1,64}$/.test(error['code'])) {
        code = error['code'];
      }
      if (typeof error['message'] === 'string') {
        message = error['message'].slice(0, MAX_MESSAGE_LENGTH);
      }
      if (typeof error['request_id'] === 'string') {
        requestId = error['request_id'].slice(0, 64);
      }
    }
  } catch {
    // Non-JSON error body: keep the generic message.
  }
  return new ApiError(
    response.status,
    code,
    message,
    requestId,
    parseRetryAfter(response.headers.get('retry-after')),
  );
}

/**
 * Minimal typed HTTP client for the ERP Insight AI API.
 *
 * - Bearer auth only; cookies are never sent (`credentials: 'omit'`).
 * - Every request carries a correlation id (`X-Request-ID: ext_<24 hex>`).
 * - Hard timeout via AbortController.
 * - Never logs request bodies, tokens or response data.
 */
export class ApiClient {
  private readonly randomBytes: (length: number) => Uint8Array;

  constructor(private readonly options: ApiClientOptions) {
    this.randomBytes = options.randomBytes ?? defaultRandomBytes;
  }

  async request<T>(req: ApiRequest<T>): Promise<T> {
    const baseUrl = await this.options.getBaseUrl();
    const headers: Record<string, string> = {
      ...req.headers,
      Accept: 'application/json',
      'X-Request-ID': newRequestId(this.randomBytes),
    };
    if (req.body !== undefined) {
      headers['Content-Type'] = 'application/json';
    }
    if (req.accessToken !== undefined) {
      headers['Authorization'] = `Bearer ${req.accessToken}`;
    }

    const controller = new AbortController();
    const timer = setTimeout(() => {
      controller.abort();
    }, req.timeoutMs ?? this.options.timeoutMs);
    let response: Response;
    try {
      response = await this.options.fetchFn(`${baseUrl}${req.path}`, {
        method: req.method,
        headers,
        body: req.body === undefined ? null : JSON.stringify(req.body),
        credentials: 'omit',
        cache: 'no-store',
        redirect: 'error',
        signal: controller.signal,
      });
    } catch {
      if (controller.signal.aborted) {
        throw new ApiError(0, 'TIMEOUT', 'The server did not respond in time.');
      }
      throw new ApiError(0, 'NETWORK_ERROR', 'Cannot reach the ERP Insight AI server.');
    } finally {
      clearTimeout(timer);
    }

    if (!response.ok) {
      throw await toApiError(response);
    }
    if (req.parse === undefined) {
      return undefined as T;
    }
    let body: unknown;
    try {
      body = await response.json();
    } catch {
      throw new ApiResponseError('non-JSON');
    }
    return req.parse(body);
  }
}

// ------------------------------------------------------------------ parsers

export interface TokenResponse {
  readonly accessToken: string;
  readonly expiresIn: number;
  readonly refreshToken: string;
  readonly refreshExpiresIn: number;
  readonly user: UserInfo;
}

function str(value: unknown, what: string, max = 1024): string {
  if (typeof value !== 'string' || value === '' || value.length > max) {
    throw new ApiResponseError(what);
  }
  return value;
}

function optStr(value: unknown, what: string, max = 1024): string | null {
  return value === null || value === undefined ? null : str(value, what, max);
}

function positiveInt(value: unknown, what: string): number {
  if (typeof value !== 'number' || !Number.isInteger(value) || value <= 0) {
    throw new ApiResponseError(what);
  }
  return value;
}

function stringList(value: unknown, what: string): readonly string[] {
  if (!Array.isArray(value) || value.length > 200) {
    throw new ApiResponseError(what);
  }
  return value.map((item) => str(item, what, 128));
}

export function parseUser(body: unknown): UserInfo {
  if (!isRecord(body) || !isRecord(body['tenant'])) {
    throw new ApiResponseError('user');
  }
  const tenant = body['tenant'];
  return {
    id: str(body['id'], 'user', 64),
    email: str(body['email'], 'user', 320),
    displayName: str(body['display_name'], 'user', 200),
    tenant: {
      id: str(tenant['id'], 'user', 64),
      slug: str(tenant['slug'], 'user', 63),
      name: str(tenant['name'], 'user', 200),
    },
    roles: stringList(body['roles'], 'user'),
    permissions: stringList(body['permissions'], 'user'),
  };
}

export function parseTokenResponse(body: unknown): TokenResponse {
  if (!isRecord(body) || (body['token_type'] ?? 'bearer') !== 'bearer') {
    throw new ApiResponseError('token');
  }
  return {
    accessToken: str(body['access_token'], 'token', 8192),
    expiresIn: positiveInt(body['expires_in'], 'token'),
    refreshToken: str(body['refresh_token'], 'token', 1024),
    refreshExpiresIn: positiveInt(body['refresh_expires_in'], 'token'),
    user: parseUser(body['user']),
  };
}

const CONNECTION_STATUSES: readonly ConnectionHealth[] = ['untested', 'ok', 'error'];

function parseConnection(item: unknown): ConnectionSummary {
  if (!isRecord(item)) {
    throw new ApiResponseError('connection');
  }
  const port = item['port'];
  if (port !== null && port !== undefined && typeof port !== 'number') {
    throw new ApiResponseError('connection');
  }
  const status = item['status'];
  return {
    id: str(item['id'], 'connection', 64),
    name: str(item['name'], 'connection', 120),
    connectorType: str(item['connector_type'], 'connection', 32),
    host: optStr(item['host'], 'connection', 255),
    port: typeof port === 'number' ? port : null,
    databaseName: optStr(item['database_name'], 'connection', 255),
    schemaName: optStr(item['schema_name'], 'connection', 255),
    status: CONNECTION_STATUSES.includes(status as ConnectionHealth)
      ? (status as ConnectionHealth)
      : 'untested',
    lastTestedAt: optStr(item['last_tested_at'], 'connection', 64),
    readOnly: true,
    hasSecret: item['has_secret'] === true,
    username: optStr(item['username'], 'connection', 255),
    tlsMode: (TLS_MODES as readonly string[]).includes(item['tls_mode'] as string)
      ? (item['tls_mode'] as TlsModeName)
      : 'verify-full',
    maxRows: typeof item['max_rows'] === 'number' ? item['max_rows'] : 1000,
    queryTimeoutSeconds:
      typeof item['query_timeout_seconds'] === 'number' ? item['query_timeout_seconds'] : 30,
    readReplicaHost: optStr(item['read_replica_host'], 'connection', 255),
    readReplicaPort:
      typeof item['read_replica_port'] === 'number' ? item['read_replica_port'] : null,
  };
}

export function parseConnectionItem(body: unknown): ConnectionSummary {
  return parseConnection(body);
}

function num(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

/** Combines `GET /sync-status` and `GET /semantic/status` into one view. */
export function parseDataSourceStatus(sync: unknown, semantic: unknown): DataSourceStatus {
  if (!isRecord(sync) || !isRecord(semantic)) {
    throw new ApiResponseError('data source status');
  }
  const meta = isRecord(sync['latest']) ? sync['latest'] : {};
  const index = isRecord(semantic['latest']) ? semantic['latest'] : {};
  const bindings = isRecord(index['entity_bindings']) ? index['entity_bindings'] : {};
  return {
    metadata: {
      status: optStr(meta['status'], 'data source status', 32),
      objectCount: num(meta['object_count']),
      syncedAt: optStr(sync['synced_at'], 'data source status', 64),
      errorCode: optStr(meta['error_code'], 'data source status', 64),
    },
    semantic: {
      status: optStr(index['status'], 'data source status', 32),
      activeVersion: num(semantic['active_version']),
      entitiesBound: Object.values(bindings).filter((b) => isRecord(b) && b['bound'] === true)
        .length,
      errorCode: optStr(index['error_code'], 'data source status', 64),
    },
  };
}

export function parseConnectionList(body: unknown): readonly ConnectionSummary[] {
  if (!isRecord(body) || !Array.isArray(body['items'])) {
    throw new ApiResponseError('connection list');
  }
  return body['items'].map(parseConnection);
}

export function parseConnectionTest(body: unknown): ConnectionTestResult {
  if (!isRecord(body) || typeof body['ok'] !== 'boolean') {
    throw new ApiResponseError('connection test');
  }
  const latency = body['latency_ms'];
  return {
    ok: body['ok'],
    latencyMs: typeof latency === 'number' && Number.isFinite(latency) ? latency : null,
    serverVersion: optStr(body['server_version'], 'connection test', 200),
    errorCode: optStr(body['error_code'], 'connection test', 64),
    message:
      typeof body['message'] === 'string' ? body['message'].slice(0, MAX_MESSAGE_LENGTH) : null,
  };
}
