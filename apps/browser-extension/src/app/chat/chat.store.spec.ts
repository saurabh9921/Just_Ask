import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { Subject, of } from 'rxjs';

import { AuthService } from '../core/auth/auth.service';
import { AnalyticsStreamService } from '../core/messaging/analytics-stream.service';
import type { AskRequest, StreamEvent } from '../core/messaging/analytics-types';
import { BackgroundBridgeService } from '../core/messaging/background-bridge.service';
import { CONNECTION_ID, QUERY_ID, queryResponse } from '../../testing/analytics-fixtures';
import { ChatStore } from './chat.store';

function setup(permissions: string[] = ['analytics.query']) {
  const streams: { ref: string; request: AskRequest; events: Subject<StreamEvent> }[] = [];
  let n = 0;
  const stream = {
    ask: vi.fn((request: AskRequest) => {
      const events = new Subject<StreamEvent>();
      const ref = `q_${++n}`;
      streams.push({ ref, request, events });
      return { ref, events };
    }),
    cancel: vi.fn(),
  };
  const request = vi.fn(() => of({}));
  const sessionEnded = vi.fn();
  TestBed.configureTestingModule({
    providers: [
      { provide: AnalyticsStreamService, useValue: stream },
      { provide: BackgroundBridgeService, useValue: { request } },
      {
        provide: AuthService,
        useValue: {
          user: signal({ permissions }),
          sessionEnded,
        },
      },
    ],
  });
  return { store: TestBed.inject(ChatStore), streams, stream, request, sessionEnded };
}

describe('ChatStore', () => {
  it('tracks a question from progress to result', () => {
    const { store, streams } = setup();
    const turn = store.ask('  Total sales by region ', { connectionId: CONNECTION_ID })!;
    expect(turn.question).toBe('Total sales by region');
    expect(store.running()).toBe(true);
    const { events, request } = streams[0]!;
    expect(request).toEqual({
      question: 'Total sales by region',
      connectionId: CONNECTION_ID,
      parentQueryId: null,
      conversationId: request.conversationId,
      mode: 'execute',
    });
    expect(request.conversationId).toMatch(/^[0-9a-f-]{36}$/);
    events.next({ kind: 'transport', ref: turn.id, transport: 'websocket' });
    events.next({ kind: 'accepted', ref: turn.id, queryId: QUERY_ID });
    const progress = (step: string, status: 'started' | 'completed') =>
      events.next({
        kind: 'progress',
        ref: turn.id,
        event: { queryId: QUERY_ID, step, status, message: step, elapsedMs: 10 },
      });
    progress('BUILD_QUERY_PLAN', 'started');
    progress('BUILD_QUERY_PLAN', 'completed');
    progress('VALIDATE_QUERY_PLAN', 'started');
    const running = store.get(turn.id)!;
    expect(running.queryId).toBe(QUERY_ID);
    expect(running.steps.map((s) => `${s.step}:${s.status}`)).toEqual([
      'BUILD_QUERY_PLAN:completed',
      'VALIDATE_QUERY_PLAN:started',
    ]);
    events.next({ kind: 'result', ref: turn.id, response: queryResponse() });
    const done = store.get(turn.id)!;
    expect(done.phase).toBe('done');
    expect(done.response?.rowsReturned).toBe(4);
    expect(store.running()).toBe(false);
  });

  it('sends follow-ups with the parent query and supports cancel/retry', () => {
    const { store, streams, stream } = setup();
    const first = store.ask('Sales by region', { connectionId: CONNECTION_ID })!;
    streams[0]!.events.next({ kind: 'result', ref: first.id, response: queryResponse() });
    const follow = store.ask('now by branch', {
      connectionId: CONNECTION_ID,
      followUpOf: store.get(first.id)!,
    })!;
    expect(streams[1]!.request.parentQueryId).toBe(QUERY_ID);
    expect(follow.parentQuestion).toBe('Sales by region');

    store.cancel(store.get(follow.id)!);
    expect(stream.cancel).toHaveBeenCalledWith(follow.id);
    expect(store.get(follow.id)!.cancelling).toBe(true);
    streams[1]!.events.next({
      kind: 'error',
      ref: follow.id,
      error: { code: 'QUERY_CANCELLED', message: 'The query was cancelled.', status: 409 },
      response: null,
    });
    expect(store.get(follow.id)!.phase).toBe('failed');

    const retried = store.retry(store.get(follow.id)!)!;
    expect(streams[2]!.request).toMatchObject({
      question: 'now by branch',
      parentQueryId: QUERY_ID,
    });
    expect(retried.id).not.toBe(follow.id);
  });

  it('keeps one conversation id per chat; "New chat" starts another', () => {
    const { store, streams } = setup();
    const first = store.ask('Total sales by region', { connectionId: CONNECTION_ID })!;
    streams[0]!.events.next({ kind: 'result', ref: first.id, response: queryResponse() });
    store.ask('now by branch', {
      connectionId: CONNECTION_ID,
      followUpOf: store.get(first.id)!,
    });
    store.ask('top customers', { connectionId: CONNECTION_ID });
    const chat = streams[0]!.request.conversationId;
    expect(chat).not.toBeNull();
    expect(streams[1]!.request.conversationId).toBe(chat); // follow-up
    expect(streams[2]!.request.conversationId).toBe(chat); // another question, same chat

    streams[1]!.events.next({ kind: 'result', ref: streams[1]!.ref, response: queryResponse() });
    streams[2]!.events.next({ kind: 'result', ref: streams[2]!.ref, response: queryResponse() });
    store.clear();
    store.ask('total sales last month', { connectionId: CONNECTION_ID });
    expect(streams[3]!.request.conversationId).not.toBe(chat);
  });

  it('ends the session on authentication errors', () => {
    const { store, streams, sessionEnded } = setup();
    const turn = store.ask('q', { connectionId: CONNECTION_ID })!;
    streams[0]!.events.next({
      kind: 'error',
      ref: turn.id,
      error: { code: 'SESSION_EXPIRED', message: 'ended', status: 401 },
      response: null,
    });
    expect(sessionEnded).toHaveBeenCalled();
  });

  it('refuses empty questions and questions without a source', () => {
    const { store, stream } = setup();
    expect(store.ask('   ', { connectionId: CONNECTION_ID })).toBeNull();
    expect(store.ask('sales', { connectionId: null })).toBeNull();
    expect(stream.ask).not.toHaveBeenCalled();
  });

  it('saves questions and records feedback through the background worker', () => {
    const { store, streams, request } = setup(['analytics.query', 'analytics.export']);
    expect(store.canExport()).toBe(true);
    const turn = store.ask('q', { connectionId: CONNECTION_ID })!;
    streams[0]!.events.next({ kind: 'result', ref: turn.id, response: queryResponse() });
    store.setSaved(store.get(turn.id)!, true).subscribe();
    expect(request).toHaveBeenCalledWith({
      type: 'history.save',
      payload: { queryId: QUERY_ID, saved: true },
    });
    expect(store.get(turn.id)!.saved).toBe(true);
    request.mockReturnValueOnce(
      of({ queryId: QUERY_ID, rating: 1, categories: [], remembered: true }),
    );
    store
      .submitFeedback(store.get(turn.id)!, {
        queryId: QUERY_ID,
        rating: 1,
        categories: [],
        comment: null,
      })
      .subscribe();
    expect(store.get(turn.id)!.feedback).toBe(1);
  });
});
