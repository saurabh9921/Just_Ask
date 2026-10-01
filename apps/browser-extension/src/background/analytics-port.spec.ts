import { ANALYTICS_PORT, type StreamEvent } from '../app/core/messaging/analytics-types';
import { CONNECTION_ID, QUERY_ID, queryResponse } from '../testing/analytics-fixtures';
import {
  AnalyticsPortHandler,
  CommandError,
  parseCommand,
  type AnalyticsHttp,
  type PortLike,
} from './analytics-port';
import {
  SocketUnavailableError,
  type AnalyticsSocket,
  type QueryHandlers,
} from './analytics-socket';
import { ApiError } from './api-client';

const EXTENSION_ID = 'abcdefghijklmnopabcdefghijklmnop';

class FakePort implements PortLike {
  readonly posted: StreamEvent[] = [];
  disconnected = false;
  private messageListener: ((message: unknown) => void) | null = null;
  private disconnectListener: (() => void) | null = null;
  readonly onMessage = {
    addListener: (fn: (message: unknown) => void) => {
      this.messageListener = fn;
    },
  };
  readonly onDisconnect = {
    addListener: (fn: () => void) => {
      this.disconnectListener = fn;
    },
  };

  constructor(
    readonly name = ANALYTICS_PORT,
    readonly sender: { id?: string } = { id: EXTENSION_ID },
  ) {}

  postMessage(message: StreamEvent): void {
    this.posted.push(message);
  }

  disconnect(): void {
    this.disconnected = true;
  }

  send(message: unknown): void {
    this.messageListener?.(message);
  }

  close(): void {
    this.disconnectListener?.();
  }
}

async function flush(): Promise<void> {
  for (let i = 0; i < 10; i++) {
    await Promise.resolve();
  }
}

function ask(ref = 'q_1', request: Record<string, unknown> = {}) {
  return {
    kind: 'ask',
    ref,
    request: {
      question: 'Total sales',
      connectionId: CONNECTION_ID,
      parentQueryId: null,
      mode: 'execute',
      ...request,
    },
  };
}

function setup(submit: (ref: string, handlers: QueryHandlers) => Promise<void>) {
  const socket = {
    submit: vi.fn((ref: string, _r: unknown, _k: string, handlers: QueryHandlers) =>
      submit(ref, handlers),
    ),
    cancel: vi.fn(),
  };
  const http = { ask: vi.fn(() => Promise.resolve(queryResponse())) };
  const handler = new AnalyticsPortHandler({
    extensionId: EXTENSION_ID,
    socket: socket as unknown as AnalyticsSocket,
    http: http as unknown as AnalyticsHttp,
    newIdempotencyKey: () => 'idem_1',
    logger: { warn: vi.fn() },
  });
  return { handler, socket, http };
}

describe('parseCommand', () => {
  it('validates ask and cancel commands', () => {
    expect(parseCommand(ask())).toEqual({
      kind: 'ask',
      ref: 'q_1',
      request: {
        question: 'Total sales',
        connectionId: CONNECTION_ID,
        parentQueryId: null,
        conversationId: null,
        mode: 'execute',
      },
    });
    expect(parseCommand(ask('q_1', { conversationId: QUERY_ID }))).toMatchObject({
      request: { conversationId: QUERY_ID },
    });
    expect(parseCommand({ kind: 'cancel', ref: 'q_1' })).toEqual({ kind: 'cancel', ref: 'q_1' });
  });

  it.each([
    ['bad ref', { ...ask(), ref: 'a b' }],
    ['empty question', ask('q', { question: '  ' })],
    ['long question', ask('q', { question: 'x'.repeat(1001) })],
    ['no source', ask('q', { connectionId: null })],
    ['injected id', ask('q', { connectionId: '../../admin' })],
    ['unknown kind', { kind: 'drop', ref: 'q' }],
    ['non-object', 'ask'],
  ])('rejects %s', (_label, message) => {
    expect(() => parseCommand(message)).toThrow(CommandError);
  });
});

describe('AnalyticsPortHandler', () => {
  it('relays socket events to the panel', async () => {
    const env = setup((ref, h) => {
      queueMicrotask(() => {
        h.accepted(QUERY_ID);
        h.result(queryResponse());
      });
      return Promise.resolve();
    });
    const port = new FakePort();
    env.handler.attach(port);
    port.send(ask());
    await flush();
    expect(port.posted.map((e) => e.kind)).toEqual(['transport', 'accepted', 'result']);
    expect(env.http.ask).not.toHaveBeenCalled();
  });

  it('falls back to HTTP when the socket is unavailable', async () => {
    const env = setup(() => Promise.reject(new SocketUnavailableError('down')));
    const port = new FakePort();
    env.handler.attach(port);
    port.send(ask());
    await flush();
    expect(env.http.ask).toHaveBeenCalledWith(
      expect.objectContaining({ question: 'Total sales' }),
      'idem_1',
    );
    expect(port.posted).toEqual([
      { kind: 'transport', ref: 'q_1', transport: 'http' },
      { kind: 'accepted', ref: 'q_1', queryId: QUERY_ID },
      { kind: 'result', ref: 'q_1', response: queryResponse() },
    ]);
  });

  it('maps HTTP failures to stream errors without leaking server text', async () => {
    const env = setup(() => Promise.reject(new SocketUnavailableError('down')));
    env.http.ask.mockImplementation(() =>
      Promise.reject(new ApiError(500, 'INTERNAL_ERROR', 'Traceback: secret')),
    );
    const port = new FakePort();
    env.handler.attach(port);
    port.send(ask());
    await flush();
    const last = port.posted.at(-1);
    expect(last?.kind).toBe('error');
    expect(JSON.stringify(last)).not.toContain('secret');
  });

  it('answers invalid commands with a validation error', async () => {
    const env = setup(() => Promise.resolve());
    const port = new FakePort();
    env.handler.attach(port);
    port.send(ask('q_2', { question: '' }));
    await flush();
    expect(port.posted[0]).toMatchObject({
      kind: 'error',
      ref: 'q_2',
      error: { code: 'VALIDATION_ERROR' },
    });
    expect(env.socket.submit).not.toHaveBeenCalled();
  });

  it('cancels running questions when the panel closes', async () => {
    const env = setup(() => new Promise<void>(() => undefined));
    const port = new FakePort();
    env.handler.attach(port);
    port.send(ask('q_3'));
    await flush();
    port.send({ kind: 'cancel', ref: 'q_3' });
    port.close();
    expect(env.socket.cancel).toHaveBeenCalledWith('q_3');
    expect(env.socket.cancel).toHaveBeenCalledTimes(2);
  });

  it('rejects ports from other extensions', () => {
    const env = setup(() => Promise.resolve());
    const port = new FakePort(ANALYTICS_PORT, { id: 'someone-else' });
    env.handler.attach(port);
    expect(port.disconnected).toBe(true);
  });
});
