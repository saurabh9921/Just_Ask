import type { FeedbackInput } from '../app/core/messaging/analytics-types';
import { QUERY_ID } from '../testing/analytics-fixtures';
import { createMessageRouter, type HistoryPort, type RouterDependencies } from './router';

const EXTENSION_ID = 'abcdefghijklmnopabcdefghijklmnop';

function makeRouter() {
  const history: HistoryPort = {
    list: vi.fn(() => Promise.resolve({ items: [], total: 0, limit: 20, offset: 0 })),
    get: vi.fn(() => Promise.reject(new Error('unused'))),
    setSaved: vi.fn(() => Promise.reject(new Error('unused'))),
    delete: vi.fn(() => Promise.resolve()),
    listConversations: vi.fn(() => Promise.resolve({ items: [], total: 0, limit: 20, offset: 0 })),
    getConversation: vi.fn(() => Promise.reject(new Error('unused'))),
    setConversationSaved: vi.fn(() => Promise.reject(new Error('unused'))),
    deleteConversation: vi.fn(() => Promise.resolve()),
    submitFeedback: vi.fn((input: FeedbackInput) =>
      Promise.resolve({
        queryId: input.queryId,
        rating: input.rating,
        categories: [...input.categories],
        remembered: true,
      }),
    ),
  };
  const deps = {
    extensionId: EXTENSION_ID,
    history,
    logger: { warn: vi.fn(), error: vi.fn() },
  } as unknown as RouterDependencies;
  return { router: createMessageRouter(deps), history };
}

const sender = { id: EXTENSION_ID };

describe('router: history and feedback', () => {
  it('validates and forwards history paging', async () => {
    const { router, history } = makeRouter();
    const ok = await router(
      { type: 'history.list', payload: { limit: 20, offset: 0, saved: true, search: '  sales ' } },
      sender,
    );
    expect(ok.ok).toBe(true);
    expect(history.list).toHaveBeenCalledWith({
      limit: 20,
      offset: 0,
      saved: true,
      search: 'sales',
    });
    for (const payload of [
      { limit: 0, offset: 0 },
      { limit: 1000, offset: 0 },
      { limit: 5, offset: -1 },
    ]) {
      const bad = await router({ type: 'history.list', payload }, sender);
      expect(bad).toMatchObject({ ok: false, error: { code: 'VALIDATION_ERROR' } });
    }
  });

  it('rejects malformed query ids', async () => {
    const { router, history } = makeRouter();
    const bad = await router({ type: 'history.delete', payload: { queryId: '../admin' } }, sender);
    expect(bad).toMatchObject({ ok: false, error: { code: 'VALIDATION_ERROR' } });
    expect(history.delete).not.toHaveBeenCalled();
    const ok = await router({ type: 'history.delete', payload: { queryId: QUERY_ID } }, sender);
    expect(ok).toEqual({ ok: true, data: null });
  });

  it('validates and forwards chat-history requests', async () => {
    const { router, history } = makeRouter();
    const page = await router(
      { type: 'history.conversations', payload: { limit: 20, offset: 0, search: ' north ' } },
      sender,
    );
    expect(page.ok).toBe(true);
    expect(history.listConversations).toHaveBeenCalledWith({
      limit: 20,
      offset: 0,
      search: 'north',
    });
    const badPage = await router(
      { type: 'history.conversations', payload: { limit: 1000, offset: 0 } },
      sender,
    );
    expect(badPage).toMatchObject({ ok: false, error: { code: 'VALIDATION_ERROR' } });

    const deleted = await router(
      { type: 'history.conversationDelete', payload: { conversationId: QUERY_ID } },
      sender,
    );
    expect(deleted).toEqual({ ok: true, data: null });
    expect(history.deleteConversation).toHaveBeenCalledWith(QUERY_ID);

    for (const message of [
      { type: 'history.conversation', payload: { conversationId: '../admin' } },
      { type: 'history.conversationDelete', payload: { conversationId: '' } },
      { type: 'history.conversationSave', payload: { conversationId: QUERY_ID, saved: 'yes' } },
      { type: 'history.conversationSave', payload: { conversationId: 42, saved: true } },
    ]) {
      const bad = await router(message, sender);
      expect(bad).toMatchObject({ ok: false, error: { code: 'VALIDATION_ERROR' } });
    }
    expect(history.getConversation).not.toHaveBeenCalled();
    expect(history.setConversationSaved).not.toHaveBeenCalled();
  });

  it('validates feedback before sending it', async () => {
    const { router, history } = makeRouter();
    const ok = await router(
      {
        type: 'feedback.submit',
        payload: {
          queryId: QUERY_ID,
          rating: -1,
          categories: ['slow', 'slow'],
          comment: '  late  ',
        },
      },
      sender,
    );
    expect(ok.ok).toBe(true);
    expect(history.submitFeedback).toHaveBeenCalledWith({
      queryId: QUERY_ID,
      rating: -1,
      categories: ['slow'],
      comment: 'late',
    });
    for (const payload of [
      { queryId: QUERY_ID, rating: 0, categories: [], comment: null },
      { queryId: QUERY_ID, rating: 1, categories: ['made_up'], comment: null },
      { queryId: QUERY_ID, rating: 1, categories: [], comment: 'x'.repeat(2001) },
    ]) {
      const bad = await router({ type: 'feedback.submit', payload }, sender);
      expect(bad).toMatchObject({ ok: false, error: { code: 'VALIDATION_ERROR' } });
    }
  });
});
