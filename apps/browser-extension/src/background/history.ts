import type {
  ConversationDetail,
  ConversationItem,
  ConversationPage,
  FeedbackInput,
  FeedbackResult,
  HistoryDetail,
  HistoryItem,
  HistoryPage,
  HistoryQuery,
} from '../app/core/messaging/analytics-types';
import {
  parseConversationDetail,
  parseConversationItem,
  parseConversationPage,
  parseFeedback,
  parseHistoryDetail,
  parseHistoryItem,
  parseHistoryPage,
} from './analytics-parsers';
import type { ApiClient } from './api-client';
import type { SessionManager } from './session-manager';

/** Query history (per question and per chat) and feedback for the signed-in user. */
export class HistoryService {
  constructor(
    private readonly api: ApiClient,
    private readonly session: SessionManager,
  ) {}

  list(query: HistoryQuery): Promise<HistoryPage> {
    return this.session.authorized((accessToken) =>
      this.api.request({
        method: 'GET',
        path: `/api/v1/history?${pageParams(query)}`,
        accessToken,
        parse: parseHistoryPage,
      }),
    );
  }

  listConversations(query: HistoryQuery): Promise<ConversationPage> {
    return this.session.authorized((accessToken) =>
      this.api.request({
        method: 'GET',
        path: `/api/v1/history/conversations?${pageParams(query)}`,
        accessToken,
        parse: parseConversationPage,
      }),
    );
  }

  getConversation(conversationId: string): Promise<ConversationDetail> {
    return this.session.authorized((accessToken) =>
      this.api.request({
        method: 'GET',
        path: `/api/v1/history/conversations/${encodeURIComponent(conversationId)}`,
        accessToken,
        parse: parseConversationDetail,
      }),
    );
  }

  setConversationSaved(conversationId: string, saved: boolean): Promise<ConversationItem> {
    return this.session.authorized((accessToken) =>
      this.api.request({
        method: 'PATCH',
        path: `/api/v1/history/conversations/${encodeURIComponent(conversationId)}`,
        body: { saved },
        accessToken,
        parse: parseConversationItem,
      }),
    );
  }

  deleteConversation(conversationId: string): Promise<void> {
    return this.session.authorized((accessToken) =>
      this.api.request<undefined>({
        method: 'DELETE',
        path: `/api/v1/history/conversations/${encodeURIComponent(conversationId)}`,
        accessToken,
      }),
    );
  }

  get(queryId: string): Promise<HistoryDetail> {
    return this.session.authorized((accessToken) =>
      this.api.request({
        method: 'GET',
        path: `/api/v1/history/${encodeURIComponent(queryId)}`,
        accessToken,
        parse: parseHistoryDetail,
      }),
    );
  }

  setSaved(queryId: string, saved: boolean): Promise<HistoryItem> {
    return this.session.authorized((accessToken) =>
      this.api.request({
        method: 'PATCH',
        path: `/api/v1/history/${encodeURIComponent(queryId)}`,
        body: { saved },
        accessToken,
        parse: parseHistoryItem,
      }),
    );
  }

  delete(queryId: string): Promise<void> {
    return this.session.authorized((accessToken) =>
      this.api.request<undefined>({
        method: 'DELETE',
        path: `/api/v1/history/${encodeURIComponent(queryId)}`,
        accessToken,
      }),
    );
  }

  submitFeedback(input: FeedbackInput): Promise<FeedbackResult> {
    return this.session.authorized((accessToken) =>
      this.api.request({
        method: 'POST',
        path: '/api/v1/feedback',
        body: {
          query_id: input.queryId,
          rating: input.rating,
          categories: input.categories,
          comment: input.comment,
        },
        accessToken,
        parse: parseFeedback,
      }),
    );
  }
}

function pageParams(query: HistoryQuery): string {
  const params = new URLSearchParams({
    limit: String(query.limit),
    offset: String(query.offset),
  });
  if (query.saved !== undefined) {
    params.set('saved', String(query.saved));
  }
  if (query.search !== undefined && query.search !== '') {
    params.set('q', query.search);
  }
  return params.toString();
}
