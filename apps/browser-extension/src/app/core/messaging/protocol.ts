/**
 * Typed message protocol between the side panel (Angular app) and the background
 * service worker. This file is imported by both bundles, so it must stay free of
 * DOM-only and Angular-only dependencies.
 *
 * The side panel never talks to the API directly and never holds tokens: every
 * API interaction is a message to the background worker, which is the single
 * place that owns authentication state (Phase 2) and network access.
 */

import type {
  ConversationDetail,
  ConversationItem,
  ConversationPage,
  ExportJob,
  ExportRequest,
  FeedbackInput,
  FeedbackResult,
  HistoryDetail,
  HistoryItem,
  HistoryPage,
  HistoryQuery,
} from './analytics-types';

export interface ExtensionSettings {
  /** Base URL of the ERP Insight AI API gateway, without a trailing slash. */
  readonly apiBaseUrl: string;
}

export type HealthStatus = 'ok' | 'degraded' | 'offline';

export interface HealthCheckResult {
  readonly status: HealthStatus;
  /** Round-trip latency of the readiness probe, or null when unreachable. */
  readonly latencyMs: number | null;
  /** Per-dependency status reported by the API (e.g. database, redis, qdrant). */
  readonly checks: Readonly<Record<string, string>>;
  /** ISO-8601 timestamp of when the check completed. */
  readonly checkedAt: string;
  /** Human-readable reason when the status is not `ok`. */
  readonly detail?: string;
}

// ---------------------------------------------------------------- Phase 2 types

export interface TenantInfo {
  readonly id: string;
  readonly slug: string;
  readonly name: string;
}

/** The signed-in principal. Never contains tokens. */
export interface UserInfo {
  readonly id: string;
  readonly email: string;
  readonly displayName: string;
  readonly tenant: TenantInfo;
  readonly roles: readonly string[];
  readonly permissions: readonly string[];
}

export interface SessionInfo {
  readonly user: UserInfo;
}

export interface LoginRequest {
  readonly tenant: string;
  readonly email: string;
  /** Sent once to the background worker, forwarded to the API, never stored. */
  readonly password: string;
}

/** Non-secret values remembered to prefill the sign-in form. */
export interface LoginHints {
  readonly tenant: string;
  readonly email: string;
}

export type ConnectionHealth = 'untested' | 'ok' | 'error';

export interface ConnectionSummary {
  readonly id: string;
  readonly name: string;
  readonly connectorType: string;
  readonly host: string | null;
  readonly port: number | null;
  readonly databaseName: string | null;
  readonly schemaName: string | null;
  readonly status: ConnectionHealth;
  readonly lastTestedAt: string | null;
  /** Always true in the MVP: connections are analytics-only. */
  readonly readOnly: true;
  readonly hasSecret: boolean;
  // Configuration shown to data admins (present when the API returns it).
  readonly username?: string | null;
  readonly tlsMode?: TlsModeName;
  readonly maxRows?: number;
  readonly queryTimeoutSeconds?: number;
  readonly readReplicaHost?: string | null;
  readonly readReplicaPort?: number | null;
}

/** Connector types a data admin can create from the extension. */
export const CONNECTOR_KINDS = ['postgresql', 'mysql', 'sqlserver'] as const;
export type ConnectorKind = (typeof CONNECTOR_KINDS)[number];
export const TLS_MODES = ['verify-full', 'verify-ca', 'require', 'prefer', 'disable'] as const;
export type TlsModeName = (typeof TLS_MODES)[number];

/**
 * A data source definition. The password is sent once to the background worker and
 * the API, which stores it encrypted; it is never stored in the browser and never
 * returned by the API (connections expose only ``hasSecret``).
 */
export interface ConnectionInput {
  readonly name: string;
  readonly connectorType: ConnectorKind;
  readonly host: string;
  readonly port: number | null;
  readonly databaseName: string;
  readonly schemaName: string | null;
  readonly username: string;
  readonly password?: string;
  readonly tlsMode: TlsModeName;
  readonly readReplicaHost: string | null;
  readonly readReplicaPort: number | null;
  readonly maxRows: number;
  readonly queryTimeoutSeconds: number;
}

/** Changes to an existing data source (the type cannot change; omit password to keep it). */
export type ConnectionChanges = Partial<Omit<ConnectionInput, 'connectorType'>>;

/** Metadata-sync and semantic-index state of one data source. */
export interface DataSourceStatus {
  readonly metadata: {
    readonly status: string | null;
    readonly objectCount: number | null;
    readonly syncedAt: string | null;
    readonly errorCode: string | null;
  };
  readonly semantic: {
    readonly status: string | null;
    readonly activeVersion: number | null;
    readonly entitiesBound: number;
    readonly errorCode: string | null;
  };
}

export interface ConnectionTestResult {
  readonly ok: boolean;
  readonly latencyMs: number | null;
  readonly serverVersion: string | null;
  readonly errorCode: string | null;
  readonly message: string | null;
}

export interface WorkspaceSelection {
  readonly connectionId: string | null;
}

/** Request payload per message type (`undefined` = no payload). */
export interface RequestPayloads {
  'settings.get': undefined;
  'settings.set': ExtensionSettings;
  'health.check': undefined;
  'auth.login': LoginRequest;
  'auth.logout': undefined;
  'auth.session': undefined;
  'auth.loginHints': undefined;
  'connections.list': undefined;
  'connections.test': { readonly id: string };
  'connections.create': ConnectionInput;
  'connections.update': { readonly id: string; readonly changes: ConnectionChanges };
  'connections.delete': { readonly id: string };
  'connections.sync': { readonly id: string };
  'connections.index': { readonly id: string };
  'connections.status': { readonly id: string };
  'workspace.get': undefined;
  'workspace.set': WorkspaceSelection;
  'history.list': HistoryQuery;
  'history.get': { readonly queryId: string };
  'history.save': { readonly queryId: string; readonly saved: boolean };
  'history.delete': { readonly queryId: string };
  'history.conversations': HistoryQuery;
  'history.conversation': { readonly conversationId: string };
  'history.conversationSave': { readonly conversationId: string; readonly saved: boolean };
  'history.conversationDelete': { readonly conversationId: string };
  'feedback.submit': FeedbackInput;
  'exports.create': ExportRequest;
  'exports.get': { readonly exportId: string };
  'exports.download': { readonly exportId: string };
}

/** Response data per message type. */
export interface ResponsePayloads {
  'settings.get': ExtensionSettings;
  'settings.set': ExtensionSettings;
  'health.check': HealthCheckResult;
  'auth.login': SessionInfo;
  'auth.logout': null;
  'auth.session': SessionInfo | null;
  'auth.loginHints': LoginHints | null;
  'connections.list': readonly ConnectionSummary[];
  'connections.test': ConnectionTestResult;
  'connections.create': ConnectionSummary;
  'connections.update': ConnectionSummary;
  'connections.delete': null;
  'connections.sync': DataSourceStatus;
  'connections.index': DataSourceStatus;
  'connections.status': DataSourceStatus;
  'workspace.get': WorkspaceSelection;
  'workspace.set': WorkspaceSelection;
  'history.list': HistoryPage;
  'history.get': HistoryDetail;
  'history.save': HistoryItem;
  'history.delete': null;
  'history.conversations': ConversationPage;
  'history.conversation': ConversationDetail;
  'history.conversationSave': ConversationItem;
  'history.conversationDelete': null;
  'feedback.submit': FeedbackResult;
  'exports.create': ExportJob;
  'exports.get': ExportJob;
  /** The browser download was started (the file never passes through the side panel). */
  'exports.download': { readonly fileName: string };
}

export type MessageType = keyof RequestPayloads;

export type ExtensionRequestOf<K extends MessageType> = RequestPayloads[K] extends undefined
  ? { readonly type: K }
  : { readonly type: K; readonly payload: RequestPayloads[K] };

export type ExtensionRequest = { [K in MessageType]: ExtensionRequestOf<K> }[MessageType];

export type ProtocolErrorCode =
  | 'UNKNOWN_MESSAGE'
  | 'INVALID_MESSAGE'
  | 'FORBIDDEN_SENDER'
  | 'VALIDATION_ERROR'
  | 'INTERNAL_ERROR'
  | 'NOT_IN_EXTENSION'
  | 'TIMEOUT'
  // Surfaced from the API / network layer (Phase 2)
  | 'NETWORK_ERROR'
  | 'UNAUTHENTICATED'
  | 'INVALID_CREDENTIALS'
  | 'SESSION_EXPIRED'
  | 'PERMISSION_DENIED'
  | 'NOT_FOUND'
  | 'RATE_LIMITED'
  | 'CONFLICT'
  | 'API_ERROR';

export interface ProtocolError {
  readonly code: ProtocolErrorCode;
  readonly message: string;
  /** Seconds to wait before retrying (RATE_LIMITED). */
  readonly retryAfterSeconds?: number;
  /** API correlation id, safe to show for support. */
  readonly requestId?: string;
}

/** Events pushed from the background worker to extension pages. */
export interface AuthChangedEvent {
  readonly event: 'auth.changed';
  readonly session: SessionInfo | null;
}

export function isAuthChangedEvent(value: unknown): value is AuthChangedEvent {
  return (
    isRecord(value) &&
    value['event'] === 'auth.changed' &&
    (value['session'] === null || isRecord(value['session']))
  );
}

export type ExtensionResponse<T> =
  { readonly ok: true; readonly data: T } | { readonly ok: false; readonly error: ProtocolError };

export const MESSAGE_TYPES: readonly MessageType[] = [
  'settings.get',
  'settings.set',
  'health.check',
  'auth.login',
  'auth.logout',
  'auth.session',
  'auth.loginHints',
  'connections.list',
  'connections.test',
  'connections.create',
  'connections.update',
  'connections.delete',
  'connections.sync',
  'connections.index',
  'connections.status',
  'workspace.get',
  'workspace.set',
  'history.list',
  'history.get',
  'history.save',
  'history.delete',
  'history.conversations',
  'history.conversation',
  'history.conversationSave',
  'history.conversationDelete',
  'feedback.submit',
  'exports.create',
  'exports.get',
  'exports.download',
];

export function isMessageType(value: unknown): value is MessageType {
  return typeof value === 'string' && (MESSAGE_TYPES as readonly string[]).includes(value);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/** Structural check for an incoming message. Payload contents are validated by handlers. */
export function hasMessageShape(value: unknown): value is { type: unknown; payload?: unknown } {
  return isRecord(value) && 'type' in value;
}

export function isExtensionResponse(value: unknown): value is ExtensionResponse<unknown> {
  if (!isRecord(value) || typeof value['ok'] !== 'boolean') {
    return false;
  }
  if (value['ok']) {
    return 'data' in value;
  }
  const error = value['error'];
  return (
    isRecord(error) && typeof error['code'] === 'string' && typeof error['message'] === 'string'
  );
}

export function okResponse<T>(data: T): ExtensionResponse<T> {
  return { ok: true, data };
}

export function errorResponse(
  code: ProtocolErrorCode,
  message: string,
  extra: { retryAfterSeconds?: number | undefined; requestId?: string | undefined } = {},
): ExtensionResponse<never> {
  const error: { -readonly [K in keyof ProtocolError]: ProtocolError[K] } = { code, message };
  if (extra.retryAfterSeconds !== undefined) {
    error.retryAfterSeconds = extra.retryAfterSeconds;
  }
  if (extra.requestId !== undefined) {
    error.requestId = extra.requestId;
  }
  return { ok: false, error };
}
