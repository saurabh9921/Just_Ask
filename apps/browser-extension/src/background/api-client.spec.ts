import {
  ApiClient,
  ApiError,
  newRequestId,
  parseConnectionList,
  parseConnectionTest,
  parseDataSourceStatus,
  parseTokenResponse,
  type FetchFn,
} from './api-client';

function json(status: number, body: unknown, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json', ...headers },
  });
}

function client(fetchFn: FetchFn, timeoutMs = 1_000) {
  return new ApiClient({
    getBaseUrl: () => Promise.resolve('https://erp.example.com'),
    fetchFn,
    timeoutMs,
    randomBytes: (n) => new Uint8Array(n).fill(0xab),
  });
}

const USER_JSON = {
  id: 'u-1',
  email: 'ana@acme.test',
  display_name: 'Ana',
  tenant: { id: 't-1', slug: 'acme', name: 'Acme' },
  roles: ['analyst'],
  permissions: ['analytics.query'],
};

function tokenJson(access = 'access-1', refresh = 'refresh-1') {
  return {
    access_token: access,
    token_type: 'bearer',
    expires_in: 600,
    refresh_token: refresh,
    refresh_expires_in: 43_200,
    user: USER_JSON,
  };
}

describe('ApiClient', () => {
  it('sends bearer auth, JSON body, correlation id and never cookies', async () => {
    const fetchFn = vi.fn<FetchFn>(() => Promise.resolve(json(200, { ok: true })));
    await client(fetchFn).request({
      method: 'POST',
      path: '/api/v1/x',
      body: { a: 1 },
      accessToken: 'tok',
      parse: (b) => b,
    });
    const [url, init] = fetchFn.mock.calls[0]!;
    expect(url).toBe('https://erp.example.com/api/v1/x');
    const headers = init!.headers as Record<string, string>;
    expect(headers['Authorization']).toBe('Bearer tok');
    expect(headers['Content-Type']).toBe('application/json');
    expect(headers['X-Request-ID']).toBe(`ext_${'ab'.repeat(12)}`);
    expect(init!.credentials).toBe('omit');
    expect(init!.body).toBe('{"a":1}');
  });

  it('generates 24-hex request ids', () => {
    expect(newRequestId()).toMatch(/^ext_[0-9a-f]{24}$/);
  });

  it('parses the error envelope and Retry-After', async () => {
    const fetchFn = vi.fn<FetchFn>(() =>
      Promise.resolve(
        json(
          429,
          {
            error: {
              code: 'RATE_LIMITED',
              message: 'Slow down.',
              request_id: 'req_9',
              details: {},
            },
          },
          { 'Retry-After': '42' },
        ),
      ),
    );
    const error = await client(fetchFn)
      .request({ method: 'GET', path: '/x', parse: (b) => b })
      .catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({
      status: 429,
      code: 'RATE_LIMITED',
      message: 'Slow down.',
      requestId: 'req_9',
      retryAfterSeconds: 42,
    });
  });

  it('falls back to a generic error for non-JSON bodies and rejects odd codes', async () => {
    const fetchFn = vi.fn<FetchFn>(() =>
      Promise.resolve(new Response('<html>bad gateway</html>', { status: 502 })),
    );
    await expect(client(fetchFn).request({ method: 'GET', path: '/x' })).rejects.toMatchObject({
      status: 502,
      code: 'HTTP_502',
      message: 'The request could not be completed.',
    });
    const weird = vi.fn<FetchFn>(() =>
      Promise.resolve(json(400, { error: { code: '<script>', message: 'x' } })),
    );
    await expect(client(weird).request({ method: 'GET', path: '/x' })).rejects.toMatchObject({
      code: 'HTTP_400',
    });
  });

  it('reports network failures', async () => {
    const fetchFn = vi.fn<FetchFn>(() => Promise.reject(new TypeError('Failed to fetch')));
    await expect(client(fetchFn).request({ method: 'GET', path: '/x' })).rejects.toMatchObject({
      status: 0,
      code: 'NETWORK_ERROR',
    });
  });

  it('times out slow requests', async () => {
    vi.useFakeTimers();
    try {
      const fetchFn = vi.fn<FetchFn>(
        (_url, init) =>
          new Promise((_resolve, reject) => {
            init?.signal?.addEventListener('abort', () => {
              reject(new DOMException('aborted', 'AbortError'));
            });
          }),
      );
      const pending = client(fetchFn, 500).request({ method: 'GET', path: '/x' });
      const assertion = expect(pending).rejects.toMatchObject({ status: 0, code: 'TIMEOUT' });
      await vi.advanceTimersByTimeAsync(600);
      await assertion;
    } finally {
      vi.useRealTimers();
    }
  });

  it('returns undefined for 204 responses', async () => {
    const fetchFn = vi.fn<FetchFn>(() => Promise.resolve(new Response(null, { status: 204 })));
    await expect(
      client(fetchFn).request({ method: 'POST', path: '/api/v1/auth/logout', body: {} }),
    ).resolves.toBeUndefined();
  });
});

describe('response parsers', () => {
  it('parses a token response into camelCase', () => {
    const parsed = parseTokenResponse(tokenJson());
    expect(parsed.accessToken).toBe('access-1');
    expect(parsed.expiresIn).toBe(600);
    expect(parsed.user.displayName).toBe('Ana');
    expect(parsed.user.tenant.slug).toBe('acme');
  });

  it.each([
    { ...tokenJson(), access_token: '' },
    { ...tokenJson(), expires_in: -1 },
    { ...tokenJson(), token_type: 'mac' },
    { ...tokenJson(), user: { ...USER_JSON, tenant: null } },
    null,
  ])('rejects a malformed token response %#', (body) => {
    expect(() => parseTokenResponse(body)).toThrow(ApiError);
  });

  it('parses connections and forces read-only', () => {
    const list = parseConnectionList({
      items: [
        {
          id: 'c1',
          name: 'ERP',
          connector_type: 'postgresql',
          host: 'db',
          port: 5432,
          database_name: 'erp',
          schema_name: null,
          status: 'weird',
          last_tested_at: null,
          read_only: false,
          has_secret: true,
          secret_ref: 'vault://should-not-be-kept',
        },
      ],
      total: 1,
    });
    expect(list).toEqual([
      {
        id: 'c1',
        name: 'ERP',
        connectorType: 'postgresql',
        host: 'db',
        port: 5432,
        databaseName: 'erp',
        schemaName: null,
        status: 'untested',
        lastTestedAt: null,
        readOnly: true,
        hasSecret: true,
        username: null,
        tlsMode: 'verify-full',
        maxRows: 1000,
        queryTimeoutSeconds: 30,
        readReplicaHost: null,
        readReplicaPort: null,
      },
    ]);
    expect(JSON.stringify(list)).not.toContain('vault://');
  });

  it('combines metadata and index status for a data source', () => {
    const status = parseDataSourceStatus(
      {
        connection_id: 'c1',
        latest: { status: 'succeeded', object_count: 42, error_code: null },
        synced_at: '2026-09-27T08:00:00Z',
      },
      {
        connection_id: 'c1',
        active_version: 3,
        latest: {
          status: 'active',
          error_code: null,
          entity_bindings: {
            SalesOrders: { bound: true },
            Customers: { bound: true },
            Legacy: { bound: false },
          },
        },
      },
    );
    expect(status).toEqual({
      metadata: {
        status: 'succeeded',
        objectCount: 42,
        syncedAt: '2026-09-27T08:00:00Z',
        errorCode: null,
      },
      semantic: { status: 'active', activeVersion: 3, entitiesBound: 2, errorCode: null },
    });
    const empty = parseDataSourceStatus({ latest: null }, { latest: null, active_version: null });
    expect(empty.metadata.status).toBeNull();
    expect(empty.semantic.entitiesBound).toBe(0);
  });

  it('parses connection test results and truncates messages', () => {
    const result = parseConnectionTest({
      ok: false,
      latency_ms: null,
      server_version: null,
      error_code: 'CONNECTION_FAILED',
      message: 'x'.repeat(1000),
    });
    expect(result.ok).toBe(false);
    expect(result.errorCode).toBe('CONNECTION_FAILED');
    expect(result.message).toHaveLength(300);
  });
});
