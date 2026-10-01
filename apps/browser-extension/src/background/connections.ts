import type {
  ConnectionChanges,
  ConnectionInput,
  ConnectionSummary,
  ConnectionTestResult,
  DataSourceStatus,
} from '../app/core/messaging/protocol';
import {
  ApiError,
  parseConnectionItem,
  parseConnectionList,
  parseConnectionTest,
  parseDataSourceStatus,
  type ApiClient,
} from './api-client';
import { CONNECTION_ID } from './preferences';
import type { SessionManager } from './session-manager';

type Body = Record<string, unknown>;

/** Maps extension field names to the API's; only fields that are present are sent. */
export function connectionBody(input: ConnectionChanges & { connectorType?: string }): Body {
  const mapping: readonly (readonly [keyof ConnectionInput, string])[] = [
    ['name', 'name'],
    ['connectorType', 'connector_type'],
    ['host', 'host'],
    ['port', 'port'],
    ['databaseName', 'database_name'],
    ['schemaName', 'schema_name'],
    ['username', 'username'],
    ['tlsMode', 'tls_mode'],
    ['readReplicaHost', 'read_replica_host'],
    ['readReplicaPort', 'read_replica_port'],
    ['maxRows', 'max_rows'],
    ['queryTimeoutSeconds', 'query_timeout_seconds'],
  ];
  const body: Body = {};
  const source = input as Record<string, unknown>;
  for (const [from, to] of mapping) {
    if (source[from] !== undefined) {
      body[to] = source[from];
    }
  }
  // Write-only: sent only when the admin typed one, never echoed back by the API.
  if (typeof input.password === 'string' && input.password !== '') {
    body['password'] = input.password;
  }
  return body;
}

function checkId(id: string): void {
  if (!CONNECTION_ID.test(id)) {
    throw new ApiError(400, 'VALIDATION_ERROR', 'Invalid connection id.');
  }
}

/** Data sources on behalf of the signed-in user (management needs `connections.manage`). */
export class ConnectionsService {
  constructor(
    private readonly api: ApiClient,
    private readonly session: SessionManager,
  ) {}

  list(): Promise<readonly ConnectionSummary[]> {
    return this.session.authorized((accessToken) =>
      this.api.request({
        method: 'GET',
        path: '/api/v1/connections',
        accessToken,
        parse: parseConnectionList,
      }),
    );
  }

  async test(id: string): Promise<ConnectionTestResult> {
    checkId(id);
    return this.session.authorized((accessToken) =>
      this.api.request({
        method: 'POST',
        path: `/api/v1/connections/${encodeURIComponent(id)}/test`,
        accessToken,
        parse: parseConnectionTest,
      }),
    );
  }

  create(input: ConnectionInput): Promise<ConnectionSummary> {
    return this.session.authorized((accessToken) =>
      this.api.request({
        method: 'POST',
        path: '/api/v1/connections',
        body: connectionBody(input),
        accessToken,
        parse: parseConnectionItem,
      }),
    );
  }

  async update(id: string, changes: ConnectionChanges): Promise<ConnectionSummary> {
    checkId(id);
    return this.session.authorized((accessToken) =>
      this.api.request({
        method: 'PATCH',
        path: `/api/v1/connections/${encodeURIComponent(id)}`,
        body: connectionBody(changes),
        accessToken,
        parse: parseConnectionItem,
      }),
    );
  }

  async remove(id: string): Promise<void> {
    checkId(id);
    await this.session.authorized((accessToken) =>
      this.api.request<undefined>({
        method: 'DELETE',
        path: `/api/v1/connections/${encodeURIComponent(id)}`,
        accessToken,
      }),
    );
  }

  /** Starts a metadata sync (discover tables/views) and returns the current status. */
  async sync(id: string): Promise<DataSourceStatus> {
    checkId(id);
    await this.session.authorized((accessToken) =>
      this.api.request({
        method: 'POST',
        path: `/api/v1/connections/${encodeURIComponent(id)}/metadata/sync`,
        accessToken,
        parse: (body) => body,
      }),
    );
    return this.status(id);
  }

  /** Starts building the semantic index and returns the current status. */
  async index(id: string): Promise<DataSourceStatus> {
    checkId(id);
    await this.session.authorized((accessToken) =>
      this.api.request({
        method: 'POST',
        path: `/api/v1/connections/${encodeURIComponent(id)}/semantic/ingest`,
        accessToken,
        parse: (body) => body,
      }),
    );
    return this.status(id);
  }

  async status(id: string): Promise<DataSourceStatus> {
    checkId(id);
    const base = `/api/v1/connections/${encodeURIComponent(id)}`;
    const [sync, semantic] = await Promise.all([
      this.session.authorized((accessToken) =>
        this.api.request({
          method: 'GET',
          path: `${base}/sync-status`,
          accessToken,
          parse: (b) => b,
        }),
      ),
      this.session.authorized((accessToken) =>
        this.api.request({
          method: 'GET',
          path: `${base}/semantic/status`,
          accessToken,
          parse: (b) => b,
        }),
      ),
    ]);
    return parseDataSourceStatus(sync, semantic);
  }
}
