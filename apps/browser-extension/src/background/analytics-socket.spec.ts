import type { AskRequest, StreamError } from '../app/core/messaging/analytics-types';
import { apiResponse, CONNECTION_ID, QUERY_ID } from '../testing/analytics-fixtures';
import {
  AnalyticsSocket,
  SocketUnavailableError,
  socketUrl,
  type QueryHandlers,
  type SocketLike,
} from './analytics-socket';

class FakeSocket implements SocketLike {
  readyState = 0;
  onopen: ((event: unknown) => void) | null = null;
  onmessage: ((event: { readonly data: unknown }) => void) | null = null;
  onclose: ((event: { readonly code: number }) => void) | null = null;
  onerror: ((event: unknown) => void) | null = null;
  readonly sent: Record<string, unknown>[] = [];
  closed = false;

  constructor(readonly url: string) {}

  send(data: string): void {
    this.sent.push(JSON.parse(data) as Record<string, unknown>);
  }

  close(): void {
    this.closed = true;
    this.readyState = 3;
  }

  open(): void {
    this.readyState = 1;
    this.onopen?.({});
  }

  receive(frame: unknown): void {
    this.onmessage?.({ data: typeof frame === 'string' ? frame : JSON.stringify(frame) });
  }

  drop(): void {
    this.readyState = 3;
    this.onclose?.({ code: 1006 });
  }

  last(): Record<string, unknown> | undefined {
    return this.sent.at(-1);
  }
}

const REQUEST: AskRequest = {
  question: 'Total sales by region',
  connectionId: CONNECTION_ID,
  parentQueryId: null,
  conversationId: null,
  mode: 'execute',
};

async function flush(): Promise<void> {
  for (let i = 0; i < 5; i++) {
    await Promise.resolve();
  }
}

function setup(options: { tokens?: string[] } = {}) {
  const sockets: FakeSocket[] = [];
  const tokens = [...(options.tokens ?? ['tok-1', 'tok-2', 'tok-3'])];
  const getAccessToken = vi.fn<(force: boolean) => Promise<string>>(() =>
    Promise.resolve(tokens.shift() ?? 'tok-x'),
  );
  const timers: (() => void)[] = [];
  const socket = new AnalyticsSocket({
    getBaseUrl: () => Promise.resolve('https://api.example.test'),
    getAccessToken,
    createSocket: (url) => {
      const s = new FakeSocket(url);
      sockets.push(s);
      return s;
    },
    setTimer: (fn) => {
      timers.push(fn);
      return timers.length;
    },
    clearTimer: () => undefined,
  });
  const handlers = (): QueryHandlers & { events: string[]; errors: StreamError[] } => {
    const events: string[] = [];
    const errors: StreamError[] = [];
    return {
      events,
      errors,
      accepted: (id) => events.push(`accepted:${id}`),
      progress: (e) => events.push(`progress:${e.step}:${e.status}`),
      result: (r) => events.push(`result:${r.status}`),
      error: (e) => {
        events.push(`error:${e.code}`);
        errors.push(e);
      },
    };
  };
  return { socket, sockets, getAccessToken, handlers, timers };
}

async function connect(env: ReturnType<typeof setup>, ref = 'r1') {
  const h = env.handlers();
  const pending = env.socket.submit(ref, REQUEST, 'idem-1', h);
  await flush();
  const ws = env.sockets.at(-1)!;
  ws.open();
  await flush();
  expect(ws.last()).toEqual({ type: 'auth', access_token: 'tok-1' });
  ws.receive({ type: 'ready', user_id: 'u', tenant: 'acme', heartbeat_seconds: 20 });
  await pending;
  return { ws, h };
}

describe('socketUrl', () => {
  it('maps http(s) API URLs to ws(s) with the analytics path', () => {
    expect(socketUrl('https://api.example.test')).toBe(
      'wss://api.example.test/api/v1/analytics/ws',
    );
    expect(socketUrl('http://localhost:8080/')).toBe('ws://localhost:8080/api/v1/analytics/ws');
    expect(() => socketUrl('ftp://x')).toThrow(SocketUnavailableError);
  });
});

describe('AnalyticsSocket', () => {
  it('authenticates in-band and streams one question to its result', async () => {
    const env = setup();
    const { ws, h } = await connect(env);
    expect(ws.url).toBe('wss://api.example.test/api/v1/analytics/ws');
    expect(ws.url).not.toContain('tok');
    expect(ws.last()).toEqual({
      type: 'query',
      client_ref: 'r1',
      question: 'Total sales by region',
      mode: 'execute',
      idempotency_key: 'idem-1',
      connection_id: CONNECTION_ID,
    });
    ws.receive({ type: 'accepted', client_ref: 'r1', query_id: QUERY_ID });
    ws.receive({
      type: 'progress',
      client_ref: 'r1',
      query_id: QUERY_ID,
      event: {
        query_id: QUERY_ID,
        step: 'BUILD_QUERY_PLAN',
        status: 'started',
        message: 'Planning',
        elapsed_ms: 5,
      },
    });
    ws.receive({ type: 'ping' });
    ws.receive('not json');
    ws.receive({ type: 'result', client_ref: 'r1', query_id: QUERY_ID, response: apiResponse() });
    expect(h.events).toEqual([
      `accepted:${QUERY_ID}`,
      'progress:BUILD_QUERY_PLAN:started',
      'result:succeeded',
    ]);
    expect(env.socket.activeCount).toBe(0);
  });

  it('reuses the socket for further questions and closes it when idle', async () => {
    const env = setup();
    const { ws } = await connect(env);
    ws.receive({ type: 'result', client_ref: 'r1', query_id: QUERY_ID, response: apiResponse() });
    await env.socket.submit('r2', REQUEST, 'idem-2', env.handlers());
    expect(env.sockets).toHaveLength(1);
    expect(ws.last()?.['client_ref']).toBe('r2');
    ws.receive({ type: 'result', client_ref: 'r2', query_id: QUERY_ID, response: apiResponse() });
    env.timers.at(-1)!(); // idle timer fires
    expect(ws.closed).toBe(true);
  });

  it('cancels by query id, also when requested before acceptance', async () => {
    const env = setup();
    const { ws, h } = await connect(env);
    env.socket.cancel('r1');
    expect(ws.sent.some((f) => f['type'] === 'cancel')).toBe(false);
    ws.receive({ type: 'accepted', client_ref: 'r1', query_id: QUERY_ID });
    expect(ws.last()).toEqual({ type: 'cancel', query_id: QUERY_ID });
    ws.receive({
      type: 'error',
      client_ref: 'r1',
      query_id: QUERY_ID,
      error: { code: 'QUERY_CANCELLED', message: 'The query was cancelled.', status: 409 },
      response: apiResponse({ status: 'cancelled', result: null }),
    });
    expect(h.events.at(-1)).toBe('error:QUERY_CANCELLED');
  });

  it('refreshes the token and re-sends the question after TOKEN_EXPIRED', async () => {
    const env = setup();
    const { ws, h } = await connect(env);
    ws.receive({
      type: 'error',
      client_ref: 'r1',
      error: { code: 'TOKEN_EXPIRED', message: 'expired', status: 401 },
    });
    await flush();
    expect(env.getAccessToken).toHaveBeenLastCalledWith(true);
    expect(ws.last()).toEqual({ type: 'auth', access_token: 'tok-2' });
    ws.receive({ type: 'ready', user_id: 'u', tenant: 'acme', heartbeat_seconds: 20 });
    expect(ws.last()?.['type']).toBe('query');
    expect(h.events).toEqual([]);
  });

  it('fails over when the handshake is rejected or the socket never opens', async () => {
    const env = setup();
    const h = env.handlers();
    const pending = env.socket.submit('r1', REQUEST, 'k', h);
    await flush();
    const ws = env.sockets[0]!;
    ws.open();
    await flush();
    ws.receive({ type: 'error', error: { code: 'INVALID_TOKEN', message: 'no', status: 401 } });
    await expect(pending).rejects.toBeInstanceOf(SocketUnavailableError);
    expect(ws.closed).toBe(true);

    const again = env.socket.submit('r2', REQUEST, 'k', env.handlers());
    await flush();
    env.sockets[1]!.drop();
    await expect(again).rejects.toBeInstanceOf(SocketUnavailableError);
  });

  it('reports a lost connection to running questions', async () => {
    const env = setup();
    const { ws, h } = await connect(env);
    ws.drop();
    expect(h.errors[0]?.code).toBe('NETWORK_ERROR');
    expect(env.socket.activeCount).toBe(0);
  });

  it('turns malformed result frames into INVALID_RESPONSE', async () => {
    const env = setup();
    const { ws, h } = await connect(env);
    ws.receive({ type: 'result', client_ref: 'r1', response: { status: 'nope' } });
    expect(h.events).toEqual(['error:INVALID_RESPONSE']);
  });
});
