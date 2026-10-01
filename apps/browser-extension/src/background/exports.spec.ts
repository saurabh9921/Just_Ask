import { apiResponse, QUERY_ID } from '../testing/analytics-fixtures';
import { parseDownloadToken, parseExportJob, parseQueryResponse } from './analytics-parsers';
import { ApiClient, ApiResponseError, type FetchFn } from './api-client';
import { ExportsService, safeFileName } from './exports';
import { createMessageRouter, type ExportsPort, type RouterDependencies } from './router';
import type { SessionManager } from './session-manager';

const EXPORT_ID = '9d2f7c1a-4b3e-4f5a-8c6d-1e2f3a4b5c6d';
const TOKEN = 'A'.repeat(43);

function exportJson(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    export_id: EXPORT_ID,
    query_id: QUERY_ID,
    format: 'csv',
    scope: 'all_rows',
    status: 'succeeded',
    row_count: 1200,
    byte_size: 40960,
    truncated: false,
    file_name: 'erp-insight-total-sales-2026-09-23.csv',
    error_code: null,
    created_at: '2026-09-23T10:00:00+00:00',
    finished_at: '2026-09-23T10:00:05+00:00',
    expires_at: '2026-09-23T11:00:05+00:00',
    downloaded_at: null,
    ...overrides,
  };
}

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('export parsers', () => {
  it('parses export jobs and the cache marker', () => {
    const job = parseExportJob(exportJson());
    expect(job).toMatchObject({ exportId: EXPORT_ID, scope: 'all_rows', rowCount: 1200 });
    expect(() => parseExportJob(exportJson({ status: 'weird' }))).toThrow(ApiResponseError);
    expect(
      parseQueryResponse(apiResponse({ cache: { hit: true, age_seconds: 42 } })).cache,
    ).toEqual({ hit: true, ageSeconds: 42 });
    expect(parseQueryResponse(apiResponse({ cache: undefined })).cache.hit).toBe(false);
  });

  it('only accepts same-API one-time download paths', () => {
    expect(parseDownloadToken({ url: `/api/v1/exports/download/${TOKEN}` }).path).toContain(TOKEN);
    for (const url of [
      `https://evil.example/api/v1/exports/download/${TOKEN}`,
      `//evil.example/api/v1/exports/download/${TOKEN}`,
      `/api/v1/exports/download/${TOKEN}/../../admin`,
      '/api/v1/exports/download/short',
      'javascript:alert(1)',
    ]) {
      expect(() => parseDownloadToken({ url })).toThrow(ApiResponseError);
    }
  });

  it('flattens file names', () => {
    expect(safeFileName('../../etc/passwd', 'x.csv')).toBe('etc-passwd');
    expect(safeFileName('report 2026.csv', 'x.csv')).toBe('report-2026.csv');
    expect(safeFileName(null, 'x.csv')).toBe('x.csv');
    expect(safeFileName('...', 'x.csv')).toBe('x.csv');
  });
});

describe('ExportsService', () => {
  function service(fetchFn: FetchFn) {
    const api = new ApiClient({
      getBaseUrl: () => Promise.resolve('https://erp.example.com'),
      fetchFn,
      timeoutMs: 1_000,
    });
    const session = {
      authorized: <T>(call: (token: string) => Promise<T>) => call('access-1'),
    } as unknown as SessionManager;
    const startDownload = vi.fn(() => Promise.resolve());
    const exports = new ExportsService(api, session, {
      getBaseUrl: () => Promise.resolve('https://erp.example.com/'),
      startDownload,
    });
    return { exports, startDownload };
  }

  it('always sends the confirmation the side panel collected', async () => {
    const fetchFn = vi.fn<FetchFn>(() =>
      Promise.resolve(json(202, exportJson({ status: 'pending' }))),
    );
    const { exports } = service(fetchFn);
    const job = await exports.create({ queryId: QUERY_ID, format: 'csv', scope: 'all_rows' });
    expect(job.status).toBe('pending');
    const [url, init] = fetchFn.mock.calls[0]!;
    expect(url).toBe('https://erp.example.com/api/v1/exports');
    expect(JSON.parse(init!.body as string)).toEqual({
      query_id: QUERY_ID,
      format: 'csv',
      scope: 'all_rows',
      confirm: true,
    });
  });

  it('downloads through a one-time link without a bearer token in the URL', async () => {
    const fetchFn = vi.fn<FetchFn>((url) =>
      Promise.resolve(
        url.endsWith('/download-token')
          ? json(200, { url: `/api/v1/exports/download/${TOKEN}`, expires_in: 120 })
          : json(200, exportJson()),
      ),
    );
    const { exports, startDownload } = service(fetchFn);
    const { fileName } = await exports.download(EXPORT_ID);
    expect(fileName).toBe('erp-insight-total-sales-2026-09-23.csv');
    expect(startDownload).toHaveBeenCalledWith(
      `https://erp.example.com/api/v1/exports/download/${TOKEN}`,
      fileName,
    );
    const [downloadUrl] = startDownload.mock.calls[0] as unknown as [string];
    expect(downloadUrl).not.toContain('access-1');
  });
});

describe('router: exports', () => {
  const EXTENSION_ID = 'abcdefghijklmnopabcdefghijklmnop';
  function makeRouter() {
    const exports: ExportsPort = {
      create: vi.fn(() => Promise.resolve(parseExportJob(exportJson({ status: 'pending' })))),
      get: vi.fn(() => Promise.resolve(parseExportJob(exportJson()))),
      download: vi.fn(() => Promise.resolve({ fileName: 'f.csv' })),
    };
    const deps = {
      extensionId: EXTENSION_ID,
      exports,
      logger: { warn: vi.fn(), error: vi.fn() },
    } as unknown as RouterDependencies;
    return { router: createMessageRouter(deps), exports };
  }
  const sender = { id: EXTENSION_ID };

  it('validates export requests before calling the API', async () => {
    const { router, exports } = makeRouter();
    const ok = await router(
      { type: 'exports.create', payload: { queryId: QUERY_ID, format: 'json', scope: 'all_rows' } },
      sender,
    );
    expect(ok.ok).toBe(true);
    expect(exports.create).toHaveBeenCalledWith({
      queryId: QUERY_ID,
      format: 'json',
      scope: 'all_rows',
    });
    for (const payload of [
      { queryId: QUERY_ID, format: 'xlsx', scope: 'all_rows' },
      { queryId: QUERY_ID, format: 'csv', scope: 'everything' },
      { queryId: '../x', format: 'csv', scope: 'result' },
    ]) {
      const bad = await router({ type: 'exports.create', payload }, sender);
      expect(bad).toMatchObject({ ok: false, error: { code: 'VALIDATION_ERROR' } });
    }
    const badId = await router({ type: 'exports.download', payload: { exportId: 'a/b' } }, sender);
    expect(badId).toMatchObject({ ok: false, error: { code: 'VALIDATION_ERROR' } });
    expect(exports.download).not.toHaveBeenCalled();
  });

  it('rejects export messages from other extensions', async () => {
    const { router, exports } = makeRouter();
    const denied = await router(
      { type: 'exports.download', payload: { exportId: EXPORT_ID } },
      { id: 'someone-else' },
    );
    expect(denied).toMatchObject({ ok: false, error: { code: 'FORBIDDEN_SENDER' } });
    expect(exports.download).not.toHaveBeenCalled();
  });
});
