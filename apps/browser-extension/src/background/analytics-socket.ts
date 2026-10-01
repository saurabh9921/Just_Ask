/**
 * WebSocket coordination for streamed analytics questions (background worker only).
 *
 * One socket to `/api/v1/analytics/ws` carries every question of this browser
 * profile, multiplexed by `client_ref`. Protocol: see the API module
 * `app/api/analytics_ws.py`.
 *
 * - Authentication is in-band: the access token is the first frame (never in the
 *   URL). When the server reports `TOKEN_EXPIRED`, the token is refreshed through the
 *   session manager and the question is re-sent once on the same socket.
 * - The socket is opened lazily and closed after `idleCloseMs` without running
 *   questions, so the service worker can go idle. While questions run, server
 *   heartbeats keep the worker alive (Chrome >= 116 extends a worker's lifetime on
 *   WebSocket activity).
 * - If the socket cannot be opened or authenticated, {@link SocketUnavailableError}
 *   tells the caller to fall back to plain HTTP.
 * - Frames are untrusted input: unknown or malformed frames are ignored, bodies are
 *   validated by the analytics parsers.
 */
import type {
  AskRequest,
  ProgressEvent,
  QueryResponse,
  StreamError,
} from '../app/core/messaging/analytics-types';
import { parseProgressEvent, parseQueryResponse } from './analytics-parsers';

export const WS_PATH = '/api/v1/analytics/ws';
const OPEN = 1;

/** The subset of the DOM `WebSocket` used here; injectable for tests. */
export interface SocketLike {
  readonly readyState: number;
  onopen: ((event: unknown) => void) | null;
  onmessage: ((event: { readonly data: unknown }) => void) | null;
  onclose: ((event: { readonly code: number }) => void) | null;
  onerror: ((event: unknown) => void) | null;
  send(data: string): void;
  close(code?: number, reason?: string): void;
}

export type SocketFactory = (url: string) => SocketLike;

export interface QueryHandlers {
  accepted(queryId: string): void;
  progress(event: ProgressEvent): void;
  result(response: QueryResponse): void;
  error(error: StreamError, response: QueryResponse | null): void;
}

export class SocketUnavailableError extends Error {
  override readonly name = 'SocketUnavailableError';
}

export interface AnalyticsSocketOptions {
  readonly getBaseUrl: () => Promise<string>;
  readonly getAccessToken: (forceRefresh: boolean) => Promise<string>;
  readonly createSocket: SocketFactory;
  readonly connectTimeoutMs?: number;
  readonly idleCloseMs?: number;
  readonly setTimer?: (fn: () => void, ms: number) => unknown;
  readonly clearTimer?: (handle: unknown) => void;
}

interface ActiveQuery {
  readonly handlers: QueryHandlers;
  readonly frame: Record<string, unknown>;
  queryId: string | null;
  cancelRequested: boolean;
  reauthenticated: boolean;
}

interface Handshake {
  resolve(): void;
  reject(error: Error): void;
  refreshed: boolean;
}

/** `http(s)://host[:port]` → `ws(s)://host[:port]/api/v1/analytics/ws`. */
export function socketUrl(apiBaseUrl: string): string {
  const url = new URL(apiBaseUrl);
  if (url.protocol !== 'http:' && url.protocol !== 'https:') {
    throw new SocketUnavailableError('Unsupported API URL scheme.');
  }
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  url.pathname = `${url.pathname.replace(/\/$/, '')}${WS_PATH}`;
  url.search = '';
  url.hash = '';
  return url.toString();
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function toStreamError(value: unknown): StreamError {
  const e = isRecord(value) ? value : {};
  return {
    code: typeof e['code'] === 'string' ? e['code'].slice(0, 64) : 'API_ERROR',
    message:
      typeof e['message'] === 'string'
        ? e['message'].slice(0, 300)
        : 'The question could not be answered.',
    status: typeof e['status'] === 'number' ? e['status'] : 500,
  };
}

export class AnalyticsSocket {
  private socket: SocketLike | null = null;
  private ready: Promise<void> | null = null;
  private handshake: Handshake | null = null;
  private idleTimer: unknown = null;
  private readonly active = new Map<string, ActiveQuery>();
  private readonly setTimer: (fn: () => void, ms: number) => unknown;
  private readonly clearTimer: (handle: unknown) => void;

  constructor(private readonly options: AnalyticsSocketOptions) {
    this.setTimer = options.setTimer ?? ((fn, ms) => setTimeout(fn, ms));
    this.clearTimer =
      options.clearTimer ?? ((handle) => clearTimeout(handle as ReturnType<typeof setTimeout>));
  }

  get activeCount(): number {
    return this.active.size;
  }

  /**
   * Sends a question. Rejects with {@link SocketUnavailableError} when no
   * authenticated socket could be established (the caller then uses HTTP).
   */
  async submit(
    ref: string,
    request: AskRequest,
    idempotencyKey: string,
    handlers: QueryHandlers,
  ): Promise<void> {
    this.cancelIdleClose();
    await this.connect();
    const frame: Record<string, unknown> = {
      type: 'query',
      client_ref: ref,
      question: request.question,
      mode: request.mode,
      idempotency_key: idempotencyKey,
    };
    if (request.connectionId !== null) {
      frame['connection_id'] = request.connectionId;
    }
    if (request.parentQueryId !== null) {
      frame['parent_query_id'] = request.parentQueryId;
    }
    if (request.conversationId !== null) {
      frame['conversation_id'] = request.conversationId;
    }
    this.active.set(ref, {
      handlers,
      frame,
      queryId: null,
      cancelRequested: false,
      reauthenticated: false,
    });
    this.send(frame);
  }

  /** Requests cancellation; the question then ends with a `QUERY_CANCELLED` error. */
  cancel(ref: string): void {
    const query = this.active.get(ref);
    if (query === undefined) {
      return;
    }
    query.cancelRequested = true;
    if (query.queryId !== null) {
      this.send({ type: 'cancel', query_id: query.queryId });
    }
  }

  /** Drops a question's handlers without cancelling it server-side. */
  forget(ref: string): void {
    this.active.delete(ref);
    this.scheduleIdleClose();
  }

  close(): void {
    const socket = this.socket;
    this.reset();
    socket?.close(1000, 'client closing');
  }

  // ---------------------------------------------------------------- connection
  private connect(): Promise<void> {
    if (this.ready !== null) {
      return this.ready;
    }
    const ready = new Promise<void>((resolve, reject) => {
      this.handshake = { resolve, reject, refreshed: false };
      void this.open().catch((error: unknown) => {
        this.failHandshake(
          error instanceof SocketUnavailableError
            ? error
            : new SocketUnavailableError('The analytics socket could not be opened.'),
        );
      });
    });
    this.ready = ready;
    return ready;
  }

  private async open(): Promise<void> {
    const url = socketUrl(await this.options.getBaseUrl());
    const socket = this.options.createSocket(url);
    this.socket = socket;
    const timer = this.setTimer(() => {
      this.failHandshake(new SocketUnavailableError('The analytics socket timed out.'));
    }, this.options.connectTimeoutMs ?? 5000);
    socket.onopen = () => {
      void this.options
        .getAccessToken(false)
        .then((token) => {
          this.send({ type: 'auth', access_token: token });
        })
        .catch(() => {
          this.failHandshake(new SocketUnavailableError('Not signed in.'));
        });
    };
    socket.onmessage = (event) => {
      if (this.handshake === null || this.socket !== socket) {
        this.onFrame(event.data);
        return;
      }
      this.onHandshakeFrame(event.data, timer);
    };
    socket.onerror = () => undefined; // followed by onclose
    socket.onclose = () => {
      this.clearTimer(timer);
      if (this.socket === socket) {
        this.onClosed();
      }
    };
  }

  private onHandshakeFrame(data: unknown, timer: unknown): void {
    const frame = this.parseFrame(data);
    const handshake = this.handshake;
    if (frame === null || handshake === null) {
      return;
    }
    if (frame['type'] === 'ready') {
      this.clearTimer(timer);
      this.handshake = null;
      handshake.resolve();
      return;
    }
    if (frame['type'] === 'error') {
      const error = toStreamError(frame['error']);
      if (error.code === 'TOKEN_EXPIRED' && !handshake.refreshed) {
        handshake.refreshed = true;
        void this.options
          .getAccessToken(true)
          .then((token) => {
            this.send({ type: 'auth', access_token: token });
          })
          .catch(() => {
            this.failHandshake(new SocketUnavailableError('Session expired.'));
          });
        return;
      }
      this.failHandshake(new SocketUnavailableError(error.message));
    }
  }

  private failHandshake(error: SocketUnavailableError): void {
    const handshake = this.handshake;
    const socket = this.socket;
    this.reset();
    socket?.close(1000, 'handshake failed');
    handshake?.reject(error);
  }

  private onClosed(): void {
    const handshake = this.handshake;
    const queries = [...this.active.values()];
    this.reset();
    handshake?.reject(new SocketUnavailableError('The analytics socket closed.'));
    for (const query of queries) {
      query.handlers.error(
        {
          code: 'NETWORK_ERROR',
          message: 'The connection to the server was lost. Try again.',
          status: 0,
        },
        null,
      );
    }
  }

  private reset(): void {
    this.cancelIdleClose();
    this.socket = null;
    this.ready = null;
    this.handshake = null;
    this.active.clear();
  }

  private send(frame: Record<string, unknown>): void {
    if (this.socket?.readyState === OPEN) {
      this.socket.send(JSON.stringify(frame));
    }
  }

  // ---------------------------------------------------------------- frames
  private parseFrame(data: unknown): Record<string, unknown> | null {
    if (typeof data !== 'string' || data.length > 16_000_000) {
      return null;
    }
    try {
      const parsed: unknown = JSON.parse(data);
      return isRecord(parsed) && typeof parsed['type'] === 'string' ? parsed : null;
    } catch {
      return null;
    }
  }

  private onFrame(data: unknown): void {
    const frame = this.parseFrame(data);
    if (frame === null) {
      return;
    }
    if (frame['type'] === 'ready') {
      this.resendReauthenticated();
      return;
    }
    const ref = frame['client_ref'];
    const query = typeof ref === 'string' ? this.active.get(ref) : undefined;
    if (typeof ref !== 'string' || query === undefined) {
      return; // heartbeat, pong, cancelled acknowledgements, stale refs
    }
    try {
      this.dispatch(ref, query, frame);
    } catch {
      this.finish(ref);
      query.handlers.error(
        {
          code: 'INVALID_RESPONSE',
          message: 'The server returned an unexpected response.',
          status: 502,
        },
        null,
      );
    }
  }

  private dispatch(ref: string, query: ActiveQuery, frame: Record<string, unknown>): void {
    switch (frame['type']) {
      case 'accepted': {
        const queryId = typeof frame['query_id'] === 'string' ? frame['query_id'] : null;
        query.queryId = queryId;
        if (queryId !== null) {
          query.handlers.accepted(queryId);
          if (query.cancelRequested) {
            this.send({ type: 'cancel', query_id: queryId });
          }
        }
        return;
      }
      case 'progress':
        query.handlers.progress(parseProgressEvent(frame['event']));
        return;
      case 'result':
        this.finish(ref);
        query.handlers.result(parseQueryResponse(frame['response']));
        return;
      case 'error': {
        const error = toStreamError(frame['error']);
        if (error.code === 'TOKEN_EXPIRED' && !query.reauthenticated) {
          query.reauthenticated = true;
          void this.options
            .getAccessToken(true)
            .then((token) => {
              this.send({ type: 'auth', access_token: token });
            })
            .catch(() => {
              this.finish(ref);
              query.handlers.error(
                { code: 'SESSION_EXPIRED', message: 'Your session has ended.', status: 401 },
                null,
              );
            });
          return;
        }
        this.finish(ref);
        const response = frame['response'];
        query.handlers.error(
          error,
          response === undefined || response === null ? null : parseQueryResponse(response),
        );
        return;
      }
      default:
        return;
    }
  }

  /** After a re-authentication, re-send the questions that failed with TOKEN_EXPIRED. */
  private resendReauthenticated(): void {
    for (const query of this.active.values()) {
      if (query.reauthenticated && query.queryId === null) {
        this.send(query.frame);
      }
    }
  }

  private finish(ref: string): void {
    this.active.delete(ref);
    this.scheduleIdleClose();
  }

  private scheduleIdleClose(): void {
    if (this.active.size > 0 || this.socket === null) {
      return;
    }
    this.cancelIdleClose();
    this.idleTimer = this.setTimer(() => {
      if (this.active.size === 0) {
        this.close();
      }
    }, this.options.idleCloseMs ?? 60_000);
  }

  private cancelIdleClose(): void {
    if (this.idleTimer !== null) {
      this.clearTimer(this.idleTimer);
      this.idleTimer = null;
    }
  }
}
