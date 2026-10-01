import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { Router, provideRouter } from '@angular/router';
import { of } from 'rxjs';

import { AuthService } from '../core/auth/auth.service';
import type {
  ConversationItem,
  HistoryDetail,
  HistoryItem,
} from '../core/messaging/analytics-types';
import { BackgroundBridgeService } from '../core/messaging/background-bridge.service';
import type { ExtensionRequest } from '../core/messaging/protocol';
import { CONNECTION_ID, QUERY_ID, queryResponse } from '../../testing/analytics-fixtures';
import { QueryHistoryComponent } from './query-history.component';

const CHAT_ID = '7b0c6a52-3f43-4a46-9a0b-1c2d3e4f5a6b';
const FOLLOW_ID = '0f8e7d6c-5b4a-4392-8a1b-2c3d4e5f6a7b';

const CHAT: ConversationItem = {
  conversationId: CHAT_ID,
  title: 'Total sales by region',
  questionCount: 2,
  lastQuestion: 'Only for the North',
  lastStatus: 'succeeded',
  connectionId: CONNECTION_ID,
  connectionName: 'ERP prod',
  saved: false,
  startedAt: '2026-09-23T08:00:00Z',
  lastActivityAt: '2026-09-23T08:05:00Z',
};

const FIRST: HistoryItem = {
  queryId: QUERY_ID,
  conversationId: CHAT_ID,
  question: 'Total sales by region',
  status: 'succeeded',
  intent: 'aggregation',
  connectionId: CONNECTION_ID,
  connectionName: 'ERP prod',
  rowCount: 4,
  totalMs: 412,
  errorCode: null,
  saved: false,
  feedbackRating: 1,
  createdAt: '2026-09-23T08:00:00Z',
  completedAt: '2026-09-23T08:00:01Z',
};

function message(item: HistoryItem, cached: boolean): HistoryDetail {
  return {
    ...item,
    response: cached ? queryResponse() : null,
    responseExpired: !cached,
  };
}

async function render(firstAnswer: 'cached' | 'expired' = 'cached') {
  const request = vi.fn((m: ExtensionRequest) => {
    switch (m.type) {
      case 'history.conversations':
        return of({ items: [CHAT], total: 1, limit: 20, offset: 0 });
      case 'history.conversation':
        return of({
          ...CHAT,
          messages: [
            message(FIRST, firstAnswer === 'cached'),
            message(
              {
                ...FIRST,
                queryId: FOLLOW_ID,
                question: 'Only for the North',
                rowCount: 1,
                feedbackRating: null,
                createdAt: '2026-09-23T08:05:00Z',
              },
              false,
            ),
          ],
        });
      case 'history.conversationSave':
        return of({ ...CHAT, saved: true });
      case 'history.conversationDelete':
        return of(null);
      default:
        return of(null);
    }
  });
  TestBed.configureTestingModule({
    imports: [QueryHistoryComponent],
    providers: [
      provideRouter([]),
      { provide: BackgroundBridgeService, useValue: { request } },
      {
        provide: AuthService,
        useValue: { user: signal({ permissions: [] }), sessionEnded: vi.fn() },
      },
    ],
  });
  const fixture = TestBed.createComponent(QueryHistoryComponent);
  fixture.detectChanges();
  await fixture.whenStable();
  fixture.detectChanges();
  const element = fixture.nativeElement as HTMLElement;
  const buttons = (label: string) =>
    Array.from(element.querySelectorAll('button')).filter((b) =>
      b.textContent.trim().startsWith(label),
    );
  const button = (label: string) => buttons(label)[0]!;
  const settle = async () => {
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
  };
  return { element, request, button, buttons, settle };
}

describe('QueryHistoryComponent', () => {
  it('lists one entry per chat with title, question count and last activity', async () => {
    const { element, request } = await render();
    expect(request).toHaveBeenCalledWith({
      type: 'history.conversations',
      payload: { limit: 20, offset: 0 },
    });
    const items = element.querySelectorAll('.item');
    expect(items.length).toBe(1);
    const text = items[0]!.textContent;
    expect(text).toContain('Total sales by region');
    expect(text).toContain('Latest: Only for the North');
    expect(text).toContain('2 questions');
    expect(text).toContain('ERP prod');
    expect(text).toContain('Last activity');
  });

  it('opens the whole chat in order, with cached and expired answers', async () => {
    const { element, request, button, settle } = await render('cached');
    button('Total sales by region').click();
    await settle();
    expect(request).toHaveBeenCalledWith({
      type: 'history.conversation',
      payload: { conversationId: CHAT_ID },
    });
    const questions = Array.from(element.querySelectorAll('.message__question')).map((q) =>
      q.textContent.replace('Question:', '').trim(),
    );
    expect(questions).toEqual(['Total sales by region', 'Only for the North']);
    const answers = element.querySelectorAll('.message__answer');
    expect(answers[0]!.textContent).toContain('South has the highest total sales');
    expect(answers[1]!.textContent).toContain('no longer cached');
    expect(element.querySelector('[aria-label="Rated correct"]')).not.toBeNull();
  });

  it('searches chats and filters saved ones', async () => {
    vi.useFakeTimers();
    try {
      const { element, request, settle } = await render();
      const search = element.querySelector<HTMLInputElement>('#history-search')!;
      search.value = 'north';
      search.dispatchEvent(new Event('input'));
      await vi.advanceTimersByTimeAsync(350);
      expect(request).toHaveBeenLastCalledWith({
        type: 'history.conversations',
        payload: { limit: 20, offset: 0, search: 'north' },
      });

      element.querySelector<HTMLButtonElement>('button[role="radio"]:nth-child(2)')!.click();
      await settle();
      expect(request).toHaveBeenLastCalledWith({
        type: 'history.conversations',
        payload: { limit: 20, offset: 0, saved: true, search: 'north' },
      });
    } finally {
      vi.useRealTimers();
    }
  });

  it('saves and deletes a whole chat, deleting only after confirmation', async () => {
    const { element, request, button, settle } = await render();
    Array.from(element.querySelectorAll('button'))
      .find((b) => b.textContent.trim() === 'Save')!
      .click();
    await settle();
    expect(request).toHaveBeenCalledWith({
      type: 'history.conversationSave',
      payload: { conversationId: CHAT_ID, saved: true },
    });
    expect(element.querySelector('[aria-label="Saved chat"]')).not.toBeNull();

    button('Delete').click();
    await settle();
    expect(element.textContent).toContain('Delete this chat and its 2 questions?');
    expect(request.mock.calls.some(([m]) => m.type === 'history.conversationDelete')).toBe(false);
    Array.from(element.querySelectorAll('button'))
      .find((b) => b.textContent.trim() === 'Delete')!
      .click();
    await settle();
    expect(request).toHaveBeenCalledWith({
      type: 'history.conversationDelete',
      payload: { conversationId: CHAT_ID },
    });
    expect(element.textContent).not.toContain('Total sales by region');
    expect(element.textContent).toContain('No chats yet');
  });

  it('runs any question of the chat again in the chat panel', async () => {
    const { button, buttons, settle } = await render();
    button('Total sales by region').click();
    await settle();
    const router = TestBed.inject(Router);
    const navigate = vi.spyOn(router, 'navigate').mockResolvedValue(true);
    const again = buttons('Run again');
    expect(again.length).toBe(2);
    again[1]!.click();
    expect(navigate).toHaveBeenCalledWith(['/chat'], {
      queryParams: { q: 'Only for the North', connection: CONNECTION_ID },
    });
  });
});
