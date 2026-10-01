/**
 * Streamed questions between the side panel and the API (background worker).
 *
 * The side panel opens a long-lived `chrome.runtime.Port` named
 * {@link ANALYTICS_PORT} and sends `ask`/`cancel` commands; this handler answers
 * with `accepted`/`progress`/`result`/`error` events. Transport preference:
 *
 * 1. WebSocket ({@link AnalyticsSocket}) - progress events as the agent works;
 * 2. plain HTTP (`/analytics/query|follow-up|explain`) when the socket cannot be
 *    established (proxy without WebSocket support, older API) - result only.
 *
 * Only ports opened by this extension's own pages are accepted, commands are
 * validated, and a closed port cancels its running questions.
 */
import {
  ANALYTICS_PORT,
  STREAM_REF,
  type AskRequest,
  type QueryMode,
  type QueryResponse,
  type StreamCommand,
  type StreamError,
  type StreamEvent,
} from '../app/core/messaging/analytics-types';
import { parseQueryResponse } from './analytics-parsers';
import { SocketUnavailableError, type AnalyticsSocket } from './analytics-socket';
import { ApiError, type ApiClient } from './api-client';
import type { SessionManager } from './session-manager';

const ID = /^[A-Za-z0-9-]{1,64}$/;
const MAX_QUESTION = 1000;
export const HTTP_QUERY_TIMEOUT_MS = 150_000;

/** The subset of `chrome.runtime.Port` used here. */
export interface PortLike {
  readonly name: string;
  readonly sender?: { readonly id?: string | undefined } | undefined;
  postMessage(message: StreamEvent): void;
  disconnect(): void;
  readonly onMessage: { addListener(listener: (message: unknown) => void): void };
  readonly onDisconnect: { addListener(listener: () => void): void };
}

export class CommandError extends Error {}

function record(value: unknown): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new CommandError('Malformed command.');
  }
  return value as Record<string, unknown>;
}

function optionalId(value: unknown): string | null {
  if (value === null || value === undefined) {
    return null;
  }
  if (typeof value !== 'string' || !ID.test(value)) {
    throw new CommandError('Invalid identifier.');
  }
  return value;
}

/** Validates a side panel command. */
export function parseCommand(message: unknown): StreamCommand {
  const m = record(message);
  const ref = m['ref'];
  if (typeof ref !== 'string' || !STREAM_REF.test(ref)) {
    throw new CommandError('Invalid reference.');
  }
  if (m['kind'] === 'cancel') {
    return { kind: 'cancel', ref };
  }
  if (m['kind'] !== 'ask') {
    throw new CommandError('Unknown command.');
  }
  const r = record(m['request']);
  const question = typeof r['question'] === 'string' ? r['question'].trim() : '';
  if (question.length === 0 || question.length > MAX_QUESTION) {
    throw new CommandError('Enter a question of up to 1000 characters.');
  }
  const mode: QueryMode = r['mode'] === 'plan' ? 'plan' : 'execute';
  const request: AskRequest = {
    question,
    connectionId: optionalId(r['connectionId']),
    parentQueryId: optionalId(r['parentQueryId']),
    conversationId: optionalId(r['conversationId']),
    mode,
  };
  if (request.connectionId === null && request.parentQueryId === null) {
    throw new CommandError('Select a data source first.');
  }
  return { kind: 'ask', ref, request };
}

function streamErrorOf(error: unknown): StreamError {
  if (error instanceof ApiError) {
    const message =
      error.status >= 400 && error.status < 500
        ? error.message
        : error.status === 0
          ? error.message
          : 'The server could not answer the question. Try again later.';
    const result: { -readonly [K in keyof StreamError]: StreamError[K] } = {
      code: error.code,
      message,
      status: error.status,
    };
    if (error.retryAfterSeconds !== null) {
      result.retryAfterSeconds = error.retryAfterSeconds;
    }
    return result;
  }
  return { code: 'INTERNAL_ERROR', message: 'The question could not be answered.', status: 500 };
}

/** HTTP transport used when the WebSocket is unavailable. */
export class AnalyticsHttp {
  constructor(
    private readonly api: ApiClient,
    private readonly session: SessionManager,
  ) {}

  ask(request: AskRequest, idempotencyKey: string): Promise<QueryResponse> {
    let path = '/api/v1/analytics/query';
    const body: Record<string, unknown> = { question: request.question };
    if (request.mode === 'plan') {
      path = '/api/v1/analytics/explain';
    } else if (request.parentQueryId !== null) {
      path = '/api/v1/analytics/follow-up';
      body['parent_query_id'] = request.parentQueryId;
    }
    if (request.connectionId !== null) {
      body['connection_id'] = request.connectionId;
    }
    if (request.conversationId !== null) {
      body['conversation_id'] = request.conversationId;
    }
    return this.session.authorized((accessToken) =>
      this.api.request({
        method: 'POST',
        path,
        body,
        accessToken,
        headers: { 'Idempotency-Key': idempotencyKey },
        timeoutMs: HTTP_QUERY_TIMEOUT_MS,
        parse: parseQueryResponse,
      }),
    );
  }
}

export interface AnalyticsPortDeps {
  readonly extensionId: string;
  readonly socket: AnalyticsSocket;
  readonly http: AnalyticsHttp;
  readonly newIdempotencyKey: () => string;
  readonly logger?: Pick<Console, 'warn'>;
}

export class AnalyticsPortHandler {
  constructor(private readonly deps: AnalyticsPortDeps) {}

  attach(port: PortLike): void {
    if (port.name !== ANALYTICS_PORT) {
      return;
    }
    if (port.sender?.id !== this.deps.extensionId) {
      (this.deps.logger ?? console).warn('[eia] rejected analytics port from foreign sender');
      port.disconnect();
      return;
    }
    const running = new Set<string>();
    let open = true;
    const post = (event: StreamEvent): void => {
      if (!open) {
        return;
      }
      try {
        port.postMessage(event);
      } catch {
        open = false;
      }
    };
    port.onMessage.addListener((message) => {
      let command: StreamCommand;
      try {
        command = parseCommand(message);
      } catch (error: unknown) {
        const raw =
          typeof message === 'object' && message !== null
            ? (message as Record<string, unknown>)['ref']
            : undefined;
        const ref = typeof raw === 'string' ? raw : '';
        if (STREAM_REF.test(ref)) {
          post({
            kind: 'error',
            ref,
            error: {
              code: 'VALIDATION_ERROR',
              message: error instanceof CommandError ? error.message : 'Invalid command.',
              status: 400,
            },
            response: null,
          });
        }
        return;
      }
      if (command.kind === 'cancel') {
        this.deps.socket.cancel(command.ref);
        return;
      }
      if (running.has(command.ref)) {
        return;
      }
      running.add(command.ref);
      void this.ask(command.ref, command.request, post).finally(() => running.delete(command.ref));
    });
    port.onDisconnect.addListener(() => {
      open = false;
      for (const ref of running) {
        this.deps.socket.cancel(ref);
      }
    });
  }

  private async ask(
    ref: string,
    request: AskRequest,
    post: (event: StreamEvent) => void,
  ): Promise<void> {
    const key = this.deps.newIdempotencyKey();
    let announced = false;
    // The transport is announced before any other event of the question.
    const viaSocket = (event: StreamEvent): void => {
      if (!announced) {
        announced = true;
        post({ kind: 'transport', ref, transport: 'websocket' });
      }
      post(event);
    };
    const done = new Promise<void>((resolve) => {
      void this.deps.socket
        .submit(ref, request, key, {
          accepted: (queryId) => viaSocket({ kind: 'accepted', ref, queryId }),
          progress: (event) => viaSocket({ kind: 'progress', ref, event }),
          result: (response) => {
            viaSocket({ kind: 'result', ref, response });
            resolve();
          },
          error: (error, response) => {
            viaSocket({ kind: 'error', ref, error, response });
            resolve();
          },
        })
        .then(() => {
          if (!announced) {
            announced = true;
            post({ kind: 'transport', ref, transport: 'websocket' });
          }
        })
        .catch(async (error: unknown) => {
          if (!(error instanceof SocketUnavailableError)) {
            post({ kind: 'error', ref, error: streamErrorOf(error), response: null });
            resolve();
            return;
          }
          post({ kind: 'transport', ref, transport: 'http' });
          try {
            const response = await this.deps.http.ask(request, key);
            post({ kind: 'accepted', ref, queryId: response.queryId });
            post({ kind: 'result', ref, response });
          } catch (httpError: unknown) {
            post({ kind: 'error', ref, error: streamErrorOf(httpError), response: null });
          }
          resolve();
        });
    });
    await done;
  }
}
