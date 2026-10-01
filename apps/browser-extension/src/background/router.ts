import {
  CONNECTOR_KINDS,
  TLS_MODES,
  errorResponse,
  hasMessageShape,
  isMessageType,
  okResponse,
  type ConnectionChanges,
  type ConnectionInput,
  type ConnectionSummary,
  type ConnectorKind,
  type DataSourceStatus,
  type TlsModeName,
  type ConnectionTestResult,
  type ExtensionResponse,
  type ExtensionSettings,
  type HealthCheckResult,
  type LoginHints,
  type LoginRequest,
  type ProtocolErrorCode,
  type SessionInfo,
  type WorkspaceSelection,
} from '../app/core/messaging/protocol';
import {
  FEEDBACK_CATEGORIES,
  type ConversationDetail,
  type ConversationItem,
  type ConversationPage,
  type ExportJob,
  type ExportRequest,
  type FeedbackCategory,
  type FeedbackInput,
  type FeedbackResult,
  type HistoryDetail,
  type HistoryItem,
  type HistoryPage,
  type HistoryQuery,
} from '../app/core/messaging/analytics-types';
import { ApiError } from './api-client';
import { CONNECTION_ID, isTenantSlug } from './preferences';
import { SettingsValidationError } from './settings';

/** The subset of `chrome.runtime.MessageSender` the router relies on. */
export interface MessageSenderInfo {
  readonly id?: string | undefined;
  readonly url?: string | undefined;
}

export interface AuthPort {
  login(request: LoginRequest): Promise<SessionInfo>;
  logout(): Promise<void>;
  getSession(): Promise<SessionInfo | null>;
}

export interface ConnectionsPort {
  list(): Promise<readonly ConnectionSummary[]>;
  test(id: string): Promise<ConnectionTestResult>;
  create(input: ConnectionInput): Promise<ConnectionSummary>;
  update(id: string, changes: ConnectionChanges): Promise<ConnectionSummary>;
  remove(id: string): Promise<void>;
  sync(id: string): Promise<DataSourceStatus>;
  index(id: string): Promise<DataSourceStatus>;
  status(id: string): Promise<DataSourceStatus>;
}

export interface HistoryPort {
  list(query: HistoryQuery): Promise<HistoryPage>;
  get(queryId: string): Promise<HistoryDetail>;
  setSaved(queryId: string, saved: boolean): Promise<HistoryItem>;
  delete(queryId: string): Promise<void>;
  listConversations(query: HistoryQuery): Promise<ConversationPage>;
  getConversation(conversationId: string): Promise<ConversationDetail>;
  setConversationSaved(conversationId: string, saved: boolean): Promise<ConversationItem>;
  deleteConversation(conversationId: string): Promise<void>;
  submitFeedback(input: FeedbackInput): Promise<FeedbackResult>;
}

export interface ExportsPort {
  create(request: ExportRequest): Promise<ExportJob>;
  get(exportId: string): Promise<ExportJob>;
  download(exportId: string): Promise<{ readonly fileName: string }>;
}

export interface PreferencesPort {
  getLoginHints(): Promise<LoginHints | null>;
  setLoginHints(hints: LoginHints): Promise<void>;
  getWorkspace(userId: string): Promise<WorkspaceSelection>;
  setWorkspace(userId: string, selection: WorkspaceSelection): Promise<WorkspaceSelection>;
}

export interface RouterDependencies {
  /** This extension's own id (`chrome.runtime.id`). */
  readonly extensionId: string;
  readonly getSettings: () => Promise<ExtensionSettings>;
  readonly setSettings: (input: unknown) => Promise<ExtensionSettings>;
  readonly checkHealth: (apiBaseUrl: string) => Promise<HealthCheckResult>;
  readonly auth: AuthPort;
  readonly connections: ConnectionsPort;
  readonly preferences: PreferencesPort;
  readonly history: HistoryPort;
  readonly exports: ExportsPort;
  readonly logger?: Pick<Console, 'warn' | 'error'>;
}

export type MessageHandler = (
  message: unknown,
  sender: MessageSenderInfo,
) => Promise<ExtensionResponse<unknown>>;

class PayloadError extends Error {}

const MAX_EMAIL = 320;
const MAX_PASSWORD = 1024;

function record(payload: unknown): Record<string, unknown> {
  if (typeof payload !== 'object' || payload === null || Array.isArray(payload)) {
    throw new PayloadError('Payload is missing.');
  }
  return payload as Record<string, unknown>;
}

/** Validates the login payload. The password is only length-checked, never logged. */
function parseLogin(payload: unknown): LoginRequest {
  const p = record(payload);
  const tenant = typeof p['tenant'] === 'string' ? p['tenant'].trim().toLowerCase() : '';
  const email = typeof p['email'] === 'string' ? p['email'].trim() : '';
  const password = p['password'];
  if (
    !isTenantSlug(tenant) ||
    email.length < 3 ||
    email.length > MAX_EMAIL ||
    !email.includes('@') ||
    typeof password !== 'string' ||
    password.length === 0 ||
    password.length > MAX_PASSWORD
  ) {
    throw new PayloadError('Enter a valid tenant, email and password.');
  }
  return { tenant, email, password };
}

function parseConnectionId(payload: unknown): string {
  const id = record(payload)['id'];
  if (typeof id !== 'string' || !CONNECTION_ID.test(id)) {
    throw new PayloadError('Invalid connection id.');
  }
  return id;
}

const CONNECTION_NAME = /^[\p{L}\p{N}_ .\-()]{1,120}$/u;
const HOST = /^[A-Za-z0-9.\-_:[\]]{1,255}$/;
const IDENT = /^[^\u0000-\u001f;]{1,255}$/;

function optText(value: unknown, pattern: RegExp, what: string): string | null {
  if (value === null || value === undefined || value === '') {
    return null;
  }
  if (typeof value !== 'string' || !pattern.test(value.trim())) {
    throw new PayloadError(`Invalid ${what}.`);
  }
  return value.trim();
}

function text(value: unknown, pattern: RegExp, what: string): string {
  const parsed = optText(value, pattern, what);
  if (parsed === null) {
    throw new PayloadError(`${what.charAt(0).toUpperCase()}${what.slice(1)} is required.`);
  }
  return parsed;
}

function intIn(value: unknown, min: number, max: number, what: string): number {
  if (typeof value !== 'number' || !Number.isInteger(value) || value < min || value > max) {
    throw new PayloadError(`Invalid ${what}.`);
  }
  return value;
}

function optPort(value: unknown, what: string): number | null {
  return value === null || value === undefined ? null : intIn(value, 1, 65535, what);
}

function password(value: unknown): string | undefined {
  if (value === undefined || value === null || value === '') {
    return undefined;
  }
  // Length-checked only; never logged or echoed.
  if (typeof value !== 'string' || value.length > MAX_PASSWORD) {
    throw new PayloadError('Invalid database password.');
  }
  return value;
}

function oneOf<T extends string>(value: unknown, allowed: readonly T[], what: string): T {
  if (typeof value !== 'string' || !(allowed as readonly string[]).includes(value)) {
    throw new PayloadError(`Invalid ${what}.`);
  }
  return value as T;
}

function parseConnectionInput(payload: unknown): ConnectionInput {
  const p = record(payload);
  const input: { -readonly [K in keyof ConnectionInput]: ConnectionInput[K] } = {
    name: text(p['name'], CONNECTION_NAME, 'name'),
    connectorType: oneOf<ConnectorKind>(p['connectorType'], CONNECTOR_KINDS, 'database type'),
    host: text(p['host'], HOST, 'host'),
    port: optPort(p['port'], 'port'),
    databaseName: text(p['databaseName'], IDENT, 'database name'),
    schemaName: optText(p['schemaName'], IDENT, 'schema'),
    username: text(p['username'], IDENT, 'database user'),
    tlsMode: oneOf<TlsModeName>(p['tlsMode'], TLS_MODES, 'TLS mode'),
    readReplicaHost: optText(p['readReplicaHost'], HOST, 'replica host'),
    readReplicaPort: optPort(p['readReplicaPort'], 'replica port'),
    maxRows: intIn(p['maxRows'], 1, 100_000, 'row limit'),
    queryTimeoutSeconds: intIn(p['queryTimeoutSeconds'], 1, 600, 'query timeout'),
  };
  const secret = password(p['password']);
  if (secret !== undefined) {
    input.password = secret;
  }
  return input;
}

function parseConnectionChanges(payload: unknown): { id: string; changes: ConnectionChanges } {
  const p = record(payload);
  const id = parseConnectionId(payload);
  const raw = record(p['changes']);
  const changes: { -readonly [K in keyof ConnectionChanges]: ConnectionChanges[K] } = {};
  if ('name' in raw) changes.name = text(raw['name'], CONNECTION_NAME, 'name');
  if ('host' in raw) changes.host = text(raw['host'], HOST, 'host');
  if ('port' in raw) changes.port = optPort(raw['port'], 'port');
  if ('databaseName' in raw)
    changes.databaseName = text(raw['databaseName'], IDENT, 'database name');
  if ('schemaName' in raw) changes.schemaName = optText(raw['schemaName'], IDENT, 'schema');
  if ('username' in raw) changes.username = text(raw['username'], IDENT, 'database user');
  if ('tlsMode' in raw) changes.tlsMode = oneOf<TlsModeName>(raw['tlsMode'], TLS_MODES, 'TLS mode');
  if ('readReplicaHost' in raw)
    changes.readReplicaHost = optText(raw['readReplicaHost'], HOST, 'replica host');
  if ('readReplicaPort' in raw)
    changes.readReplicaPort = optPort(raw['readReplicaPort'], 'replica port');
  if ('maxRows' in raw) changes.maxRows = intIn(raw['maxRows'], 1, 100_000, 'row limit');
  if ('queryTimeoutSeconds' in raw)
    changes.queryTimeoutSeconds = intIn(raw['queryTimeoutSeconds'], 1, 600, 'query timeout');
  const secret = password(raw['password']);
  if (secret !== undefined) changes.password = secret;
  return { id, changes };
}

function parseWorkspace(payload: unknown): WorkspaceSelection {
  const id = record(payload)['connectionId'];
  if (id === null) {
    return { connectionId: null };
  }
  if (typeof id !== 'string' || !CONNECTION_ID.test(id)) {
    throw new PayloadError('Invalid connection id.');
  }
  return { connectionId: id };
}

const QUERY_ID = /^[A-Za-z0-9-]{1,64}$/;
const MAX_HISTORY_PAGE = 100;
const MAX_SEARCH = 200;
const MAX_COMMENT = 2000;

function parseQueryId(payload: unknown): string {
  const id = record(payload)['queryId'];
  if (typeof id !== 'string' || !QUERY_ID.test(id)) {
    throw new PayloadError('Invalid query id.');
  }
  return id;
}

function parseConversationId(payload: unknown): string {
  const id = record(payload)['conversationId'];
  if (typeof id !== 'string' || !QUERY_ID.test(id)) {
    throw new PayloadError('Invalid conversation id.');
  }
  return id;
}

function parseSavedFlag(payload: unknown): boolean {
  const saved = record(payload)['saved'];
  if (typeof saved !== 'boolean') {
    throw new PayloadError('Invalid save flag.');
  }
  return saved;
}

function parseHistoryQuery(payload: unknown): HistoryQuery {
  const p = record(payload);
  const limit = p['limit'];
  const offset = p['offset'];
  if (
    typeof limit !== 'number' ||
    !Number.isInteger(limit) ||
    limit < 1 ||
    limit > MAX_HISTORY_PAGE ||
    typeof offset !== 'number' ||
    !Number.isInteger(offset) ||
    offset < 0
  ) {
    throw new PayloadError('Invalid history page.');
  }
  const query: { -readonly [K in keyof HistoryQuery]: HistoryQuery[K] } = { limit, offset };
  if (typeof p['saved'] === 'boolean') {
    query.saved = p['saved'];
  }
  if (typeof p['search'] === 'string' && p['search'].trim() !== '') {
    query.search = p['search'].trim().slice(0, MAX_SEARCH);
  }
  return query;
}

function parseFeedbackInput(payload: unknown): FeedbackInput {
  const p = record(payload);
  const rating = p['rating'];
  const categories = p['categories'];
  const comment = p['comment'];
  if (
    (rating !== 1 && rating !== -1) ||
    !Array.isArray(categories) ||
    categories.length > 5 ||
    !categories.every(
      (c): c is FeedbackCategory =>
        typeof c === 'string' && (FEEDBACK_CATEGORIES as readonly string[]).includes(c),
    ) ||
    (comment !== null && (typeof comment !== 'string' || comment.length > MAX_COMMENT))
  ) {
    throw new PayloadError('Invalid feedback.');
  }
  const text = typeof comment === 'string' ? comment.trim() : '';
  return {
    queryId: parseQueryId(payload),
    rating,
    categories: [...new Set(categories)],
    comment: text === '' ? null : text,
  };
}

function parseExportId(payload: unknown): string {
  const id = record(payload)['exportId'];
  if (typeof id !== 'string' || !QUERY_ID.test(id)) {
    throw new PayloadError('Invalid export id.');
  }
  return id;
}

function parseExportRequest(payload: unknown): ExportRequest {
  const p = record(payload);
  const format = p['format'];
  const scope = p['scope'];
  if ((format !== 'csv' && format !== 'json') || (scope !== 'result' && scope !== 'all_rows')) {
    throw new PayloadError('Invalid export request.');
  }
  return { queryId: parseQueryId(payload), format, scope };
}

const PASSTHROUGH_CODES = new Set<ProtocolErrorCode>([
  'INVALID_CREDENTIALS',
  'RATE_LIMITED',
  'PERMISSION_DENIED',
  'SESSION_EXPIRED',
  'UNAUTHENTICATED',
  'NOT_FOUND',
  'NETWORK_ERROR',
  'TIMEOUT',
  'VALIDATION_ERROR',
]);
const SESSION_CODES = new Set(['TOKEN_EXPIRED', 'SESSION_REVOKED', 'REFRESH_TOKEN_INVALID']);

/** Maps an API failure onto the protocol, keeping only user-safe information. */
export function apiErrorResponse(error: ApiError): ExtensionResponse<never> {
  let code: ProtocolErrorCode;
  if (PASSTHROUGH_CODES.has(error.code as ProtocolErrorCode)) {
    code = error.code as ProtocolErrorCode;
  } else if (SESSION_CODES.has(error.code)) {
    code = 'SESSION_EXPIRED';
  } else if (error.status === 429) {
    code = 'RATE_LIMITED';
  } else if (error.status === 403) {
    code = 'PERMISSION_DENIED';
  } else if (error.status === 404) {
    code = 'NOT_FOUND';
  } else if (error.status === 401) {
    code = 'UNAUTHENTICATED';
  } else if (error.status === 409) {
    code = 'CONFLICT';
  } else {
    code = 'API_ERROR';
  }
  // API 4xx messages are written for end users; anything else gets a generic text.
  const message =
    (error.status >= 400 && error.status < 500) || error.status === 0
      ? error.message
      : 'The server could not complete the request. Try again later.';
  return errorResponse(code, message, {
    retryAfterSeconds: error.retryAfterSeconds ?? undefined,
    requestId: error.requestId ?? undefined,
  });
}

/**
 * Creates the background message router.
 *
 * Security: only messages from this extension's own pages are accepted. Messages
 * from other extensions or web pages are rejected before any handler runs. The
 * manifest also declares no `externally_connectable`, so web pages cannot message
 * the extension at all; this check is defence in depth. Payloads are validated
 * here; handler errors are logged by message type only (never payloads, which may
 * contain a password).
 */
export function createMessageRouter(deps: RouterDependencies): MessageHandler {
  const logger = deps.logger ?? console;

  async function requireUserId(): Promise<string> {
    const session = await deps.auth.getSession();
    if (session === null) {
      throw new ApiError(401, 'UNAUTHENTICATED', 'Sign in to continue.');
    }
    return session.user.id;
  }

  return async (message, sender) => {
    if (sender.id !== deps.extensionId) {
      logger.warn('[eia] rejected message from foreign sender');
      return errorResponse('FORBIDDEN_SENDER', 'Sender is not permitted.');
    }
    if (!hasMessageShape(message)) {
      return errorResponse('INVALID_MESSAGE', 'Message is malformed.');
    }
    if (!isMessageType(message.type)) {
      return errorResponse('UNKNOWN_MESSAGE', 'Unsupported message type.');
    }

    try {
      switch (message.type) {
        case 'settings.get':
          return okResponse(await deps.getSettings());
        case 'settings.set':
          return okResponse(await deps.setSettings(message.payload));
        case 'health.check': {
          const settings = await deps.getSettings();
          return okResponse(await deps.checkHealth(settings.apiBaseUrl));
        }
        case 'auth.login': {
          const request = parseLogin(message.payload);
          const session = await deps.auth.login(request);
          await deps.preferences
            .setLoginHints({ tenant: request.tenant, email: request.email })
            .catch(() => undefined);
          return okResponse(session);
        }
        case 'auth.logout':
          await deps.auth.logout();
          return okResponse(null);
        case 'auth.session':
          return okResponse(await deps.auth.getSession());
        case 'auth.loginHints':
          return okResponse(await deps.preferences.getLoginHints());
        case 'connections.list':
          return okResponse(await deps.connections.list());
        case 'connections.test':
          return okResponse(await deps.connections.test(parseConnectionId(message.payload)));
        case 'connections.create':
          return okResponse(await deps.connections.create(parseConnectionInput(message.payload)));
        case 'connections.update': {
          const { id, changes } = parseConnectionChanges(message.payload);
          return okResponse(await deps.connections.update(id, changes));
        }
        case 'connections.delete':
          await deps.connections.remove(parseConnectionId(message.payload));
          return okResponse(null);
        case 'connections.sync':
          return okResponse(await deps.connections.sync(parseConnectionId(message.payload)));
        case 'connections.index':
          return okResponse(await deps.connections.index(parseConnectionId(message.payload)));
        case 'connections.status':
          return okResponse(await deps.connections.status(parseConnectionId(message.payload)));
        case 'workspace.get':
          return okResponse(await deps.preferences.getWorkspace(await requireUserId()));
        case 'workspace.set': {
          const selection = parseWorkspace(message.payload);
          return okResponse(await deps.preferences.setWorkspace(await requireUserId(), selection));
        }
        case 'history.list':
          return okResponse(await deps.history.list(parseHistoryQuery(message.payload)));
        case 'history.get':
          return okResponse(await deps.history.get(parseQueryId(message.payload)));
        case 'history.save': {
          const saved = parseSavedFlag(message.payload);
          return okResponse(await deps.history.setSaved(parseQueryId(message.payload), saved));
        }
        case 'history.delete':
          await deps.history.delete(parseQueryId(message.payload));
          return okResponse(null);
        case 'history.conversations':
          return okResponse(
            await deps.history.listConversations(parseHistoryQuery(message.payload)),
          );
        case 'history.conversation':
          return okResponse(
            await deps.history.getConversation(parseConversationId(message.payload)),
          );
        case 'history.conversationSave': {
          const saved = parseSavedFlag(message.payload);
          return okResponse(
            await deps.history.setConversationSaved(parseConversationId(message.payload), saved),
          );
        }
        case 'history.conversationDelete':
          await deps.history.deleteConversation(parseConversationId(message.payload));
          return okResponse(null);
        case 'feedback.submit':
          return okResponse(await deps.history.submitFeedback(parseFeedbackInput(message.payload)));
        case 'exports.create':
          return okResponse(await deps.exports.create(parseExportRequest(message.payload)));
        case 'exports.get':
          return okResponse(await deps.exports.get(parseExportId(message.payload)));
        case 'exports.download':
          return okResponse(await deps.exports.download(parseExportId(message.payload)));
      }
    } catch (error: unknown) {
      if (error instanceof SettingsValidationError || error instanceof PayloadError) {
        return errorResponse('VALIDATION_ERROR', error.message);
      }
      if (error instanceof ApiError) {
        return apiErrorResponse(error);
      }
      // Never forward raw error details (may contain URLs or server output) to the UI.
      logger.error('[eia] message handler failed', message.type);
      return errorResponse('INTERNAL_ERROR', 'The request could not be completed.');
    }
  };
}
