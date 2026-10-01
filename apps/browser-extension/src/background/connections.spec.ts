import type { ConnectionInput } from '../app/core/messaging/protocol';
import { ApiClient, type FetchFn } from './api-client';
import { ConnectionsService, connectionBody } from './connections';
import { createMessageRouter, type ConnectionsPort, type RouterDependencies } from './router';
import type { SessionManager } from './session-manager';

const ID = '0b8f7d8e-1c2d-4e5f-8a9b-0c1d2e3f4a5b';
const EXTENSION_ID = 'abcdefghijklmnopabcdefghijklmnop';

const INPUT: ConnectionInput = {
  name: 'ERP production',
  connectorType: 'sqlserver',
  host: 'erp-sql.internal',
  port: 1433,
  databaseName: 'ERP',
  schemaName: 'reporting',
  username: 'erp_insight_ro',
  password: 's3cret-Pa55',
  tlsMode: 'verify-full',
  readReplicaHost: null,
  readReplicaPort: null,
  maxRows: 1000,
  queryTimeoutSeconds: 30,
};

const CONNECTION_JSON = {
  id: ID,
  name: 'ERP production',
  connector_type: 'sqlserver',
  host: 'erp-sql.internal',
  port: 1433,
  database_name: 'ERP',
  schema_name: 'reporting',
  username: 'erp_insight_ro',
  tls_mode: 'verify-full',
  max_rows: 1000,
  query_timeout_seconds: 30,
  status: 'untested',
  last_tested_at: null,
  has_secret: true,
};

function json(status: number, body: unknown): Response {
  return new Response(body === undefined ? null : JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function service(fetchFn: FetchFn) {
  const api = new ApiClient({
    getBaseUrl: () => Promise.resolve('https://erp.example.com'),
    fetchFn,
    timeoutMs: 1_000,
  });
  const session = {
    authorized: <T>(call: (token: string) => Promise<T>) => call('access-1'),
  } as unknown as SessionManager;
  return new ConnectionsService(api, session);
}

describe('connectionBody', () => {
  it('maps fields to the API names and sends the password only when typed', () => {
    expect(connectionBody(INPUT)).toEqual({
      name: 'ERP production',
      connector_type: 'sqlserver',
      host: 'erp-sql.internal',
      port: 1433,
      database_name: 'ERP',
      schema_name: 'reporting',
      username: 'erp_insight_ro',
      tls_mode: 'verify-full',
      read_replica_host: null,
      read_replica_port: null,
      max_rows: 1000,
      query_timeout_seconds: 30,
      password: 's3cret-Pa55',
    });
    expect(connectionBody({ name: 'Renamed', password: '' })).toEqual({ name: 'Renamed' });
  });
});

describe('ConnectionsService', () => {
  it('creates, updates and deletes through the connections API', async () => {
    const fetchFn = vi.fn<FetchFn>((_url, init) =>
      Promise.resolve(
        init?.method === 'DELETE' ? json(204, undefined) : json(200, CONNECTION_JSON),
      ),
    );
    const connections = service(fetchFn);
    const created = await connections.create(INPUT);
    expect(created).toMatchObject({ id: ID, tlsMode: 'verify-full', hasSecret: true });
    await connections.update(ID, { maxRows: 500 });
    await connections.remove(ID);
    const calls = fetchFn.mock.calls.map(([url, init]) => [init?.method, url]);
    expect(calls).toEqual([
      ['POST', 'https://erp.example.com/api/v1/connections'],
      ['PATCH', `https://erp.example.com/api/v1/connections/${ID}`],
      ['DELETE', `https://erp.example.com/api/v1/connections/${ID}`],
    ]);
    expect(JSON.parse(fetchFn.mock.calls[1]![1]!.body as string)).toEqual({ max_rows: 500 });
    expect(JSON.stringify(created)).not.toContain('s3cret');
  });

  it('starts a table sync and reports the combined status', async () => {
    const fetchFn = vi.fn<FetchFn>((url) => {
      if (url.endsWith('/metadata/sync')) return Promise.resolve(json(202, { status: 'pending' }));
      if (url.endsWith('/sync-status')) {
        return Promise.resolve(json(200, { latest: { status: 'running' }, synced_at: null }));
      }
      return Promise.resolve(json(200, { latest: null, active_version: null }));
    });
    const status = await service(fetchFn).sync(ID);
    expect(status.metadata.status).toBe('running');
    expect(status.semantic.activeVersion).toBeNull();
  });

  it('rejects malformed ids before calling the API', async () => {
    const fetchFn = vi.fn<FetchFn>();
    await expect(service(fetchFn).remove('../admin')).rejects.toThrow('Invalid connection id.');
    expect(fetchFn).not.toHaveBeenCalled();
  });
});

describe('router: data source management', () => {
  function makeRouter() {
    const connections = {
      list: vi.fn(),
      test: vi.fn(),
      create: vi.fn((input: ConnectionInput) => Promise.resolve({ id: ID, ...input })),
      update: vi.fn(() => Promise.resolve({ id: ID })),
      remove: vi.fn(() => Promise.resolve()),
      sync: vi.fn(() => Promise.resolve({})),
      index: vi.fn(() => Promise.resolve({})),
      status: vi.fn(() => Promise.resolve({})),
    } as unknown as ConnectionsPort;
    const logger = { warn: vi.fn(), error: vi.fn() };
    const deps = {
      extensionId: EXTENSION_ID,
      connections,
      logger,
    } as unknown as RouterDependencies;
    return { router: createMessageRouter(deps), connections, logger };
  }
  const sender = { id: EXTENSION_ID };

  it('forwards a valid data source', async () => {
    const { router, connections } = makeRouter();
    const ok = await router({ type: 'connections.create', payload: INPUT }, sender);
    expect(ok.ok).toBe(true);
    expect(connections.create).toHaveBeenCalledWith(INPUT);
  });

  it.each([
    [{ name: '' }, 'name'],
    [{ connectorType: 'oracle' }, 'database type'],
    [{ host: 'db;Trusted_Connection=yes' }, 'host'],
    [{ host: 'db host' }, 'host'],
    [{ port: 70000 }, 'port'],
    [{ tlsMode: 'none' }, 'TLS mode'],
    [{ maxRows: 0 }, 'row limit'],
    [{ queryTimeoutSeconds: 601 }, 'query timeout'],
    [{ password: 'x'.repeat(2000) }, 'password'],
  ])('rejects %o', async (override, what) => {
    const { router, connections, logger } = makeRouter();
    const result = await router(
      { type: 'connections.create', payload: { ...INPUT, ...override } },
      sender,
    );
    expect(result).toMatchObject({ ok: false, error: { code: 'VALIDATION_ERROR' } });
    expect(!result.ok && result.error.message.toLowerCase()).toContain(what.toLowerCase());
    expect(connections.create).not.toHaveBeenCalled();
    expect(JSON.stringify([logger.warn.mock.calls, logger.error.mock.calls])).not.toContain(
      's3cret',
    );
  });

  it('validates partial updates and ids', async () => {
    const { router, connections } = makeRouter();
    const ok = await router(
      { type: 'connections.update', payload: { id: ID, changes: { maxRows: 250 } } },
      sender,
    );
    expect(ok.ok).toBe(true);
    expect(connections.update).toHaveBeenCalledWith(ID, { maxRows: 250 });
    const bad = await router({ type: 'connections.delete', payload: { id: 'x/../y' } }, sender);
    expect(bad).toMatchObject({ ok: false, error: { code: 'VALIDATION_ERROR' } });
  });
});
