import { checkHealth, parseChecks, type FetchFn } from './health';

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('checkHealth', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it('reports ok for HTTP 200 and parses dependency checks', async () => {
    const fetchFn = vi.fn<FetchFn>(() =>
      Promise.resolve(
        jsonResponse(200, {
          status: 'ok',
          checks: { database: { status: 'ok', latency_ms: 3 }, redis: 'ok' },
        }),
      ),
    );
    let t = 1_000;
    const result = await checkHealth('http://localhost:8080', {
      fetchFn,
      timeoutMs: 1_000,
      now: () => (t += 25),
    });
    expect(result.status).toBe('ok');
    expect(result.latencyMs).toBe(25);
    expect(result.checks).toEqual({ database: 'ok', redis: 'ok' });
    const [url, init] = fetchFn.mock.calls[0] ?? [];
    expect(url).toBe('http://localhost:8080/health/ready');
    expect(init?.credentials).toBe('omit');
  });

  it('reports degraded for HTTP 503', async () => {
    const fetchFn: FetchFn = () =>
      Promise.resolve(jsonResponse(503, { status: 'degraded', checks: { qdrant: 'error' } }));
    const result = await checkHealth('http://localhost:8080', { fetchFn, timeoutMs: 1_000 });
    expect(result.status).toBe('degraded');
    expect(result.checks).toEqual({ qdrant: 'error' });
    expect(result.detail).toContain('503');
  });

  it('reports degraded when the body is not JSON', async () => {
    const fetchFn: FetchFn = () => Promise.resolve(new Response('<html>', { status: 502 }));
    const result = await checkHealth('http://localhost:8080', { fetchFn, timeoutMs: 1_000 });
    expect(result.status).toBe('degraded');
    expect(result.checks).toEqual({});
  });

  it('reports offline when the request times out', async () => {
    vi.useFakeTimers();
    const fetchFn: FetchFn = (_url, init) =>
      new Promise((_resolve, reject) => {
        init?.signal?.addEventListener('abort', () =>
          reject(new DOMException('Aborted', 'AbortError')),
        );
      });
    const pending = checkHealth('http://localhost:8080', { fetchFn, timeoutMs: 500 });
    await vi.advanceTimersByTimeAsync(500);
    const result = await pending;
    expect(result.status).toBe('offline');
    expect(result.latencyMs).toBeNull();
    expect(result.detail).toContain('500 ms');
  });

  it('reports offline on network failure', async () => {
    const fetchFn: FetchFn = () => Promise.reject(new TypeError('Failed to fetch'));
    const result = await checkHealth('http://localhost:8080', { fetchFn, timeoutMs: 1_000 });
    expect(result.status).toBe('offline');
    expect(result.detail).toBe('API is unreachable.');
  });
});

describe('parseChecks', () => {
  it('parses the exact /health/ready payload returned by the FastAPI backend', () => {
    // Contract: apps/api/app/api/health.py ReadyResponse
    const backendBody = {
      status: 'not_ready',
      checks: {
        postgres: { status: 'down', latency_ms: 2001.4, required: true },
        redis: { status: 'up', latency_ms: 0.8, required: true },
        qdrant: { status: 'up', latency_ms: 3.1, required: true },
      },
    };
    expect(parseChecks(backendBody)).toEqual({ postgres: 'down', redis: 'up', qdrant: 'up' });
  });

  it('ignores non-object bodies and non-string statuses', () => {
    expect(parseChecks(null)).toEqual({});
    expect(parseChecks({ checks: ['ok'] })).toEqual({});
    expect(parseChecks({ checks: { a: 1, b: { status: 2 }, c: 'ok' } })).toEqual({ c: 'ok' });
  });

  it('truncates oversized values and caps the number of checks', () => {
    const many = Object.fromEntries(Array.from({ length: 50 }, (_, i) => [`dep${i}`, 'ok']));
    expect(Object.keys(parseChecks({ checks: many }))).toHaveLength(20);
    const long = parseChecks({ checks: { db: 'x'.repeat(500) } });
    expect(long['db']).toHaveLength(64);
  });
});
