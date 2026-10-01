import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { of } from 'rxjs';

import { WorkspaceService } from '../../connections/workspace.service';
import { AuthService } from '../../core/auth/auth.service';
import { PORT_CONNECTOR, type RuntimePort } from '../../core/messaging/analytics-stream.service';
import type { StreamCommand, StreamEvent } from '../../core/messaging/analytics-types';
import { BackgroundBridgeService } from '../../core/messaging/background-bridge.service';
import {
  apiResponse,
  CONNECTION_ID,
  QUERY_ID,
  queryResponse,
} from '../../../testing/analytics-fixtures';
import { AnalyticsChatComponent } from './analytics-chat.component';

class FakePort implements RuntimePort {
  readonly commands: StreamCommand[] = [];
  private listener: ((message: unknown) => void) | null = null;
  readonly onMessage = {
    addListener: (fn: (message: unknown) => void) => {
      this.listener = fn;
    },
  };
  readonly onDisconnect = { addListener: () => undefined };
  postMessage(message: StreamCommand): void {
    this.commands.push(message);
  }
  disconnect(): void {
    // not used
  }
  emit(event: StreamEvent): void {
    this.listener?.(event);
  }
}

async function render(permissions: string[] = ['analytics.query']) {
  // jsdom has no layout engine and does not implement scrollIntoView.
  Element.prototype.scrollIntoView = vi.fn();
  const port = new FakePort();
  const selected = signal<string | null>(CONNECTION_ID);
  TestBed.configureTestingModule({
    imports: [AnalyticsChatComponent],
    providers: [
      provideRouter([]),
      { provide: PORT_CONNECTOR, useValue: () => port },
      {
        provide: BackgroundBridgeService,
        useValue: { request: vi.fn(() => of({ connectionId: CONNECTION_ID })) },
      },
      {
        provide: AuthService,
        useValue: {
          user: signal({
            displayName: 'Ana',
            tenant: { id: 't', slug: 'acme', name: 'Acme' },
            roles: ['analyst'],
            permissions,
          }),
          sessionEnded: vi.fn(),
        },
      },
      {
        provide: WorkspaceService,
        useValue: {
          selectedId: selected,
          selected: signal({ id: CONNECTION_ID, name: 'ERP prod' }),
          connections: signal([{ id: CONNECTION_ID, name: 'ERP prod' }]),
          loaded: signal(true),
          loading: signal(false),
          error: signal(null),
          load: vi.fn(),
          select: vi.fn(),
        },
      },
    ],
  });
  const fixture = TestBed.createComponent(AnalyticsChatComponent);
  fixture.detectChanges();
  await fixture.whenStable();
  const element = fixture.nativeElement as HTMLElement;
  const settle = async () => {
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
  };
  const type = async (text: string) => {
    const textarea = element.querySelector<HTMLTextAreaElement>('textarea#eia-question')!;
    textarea.value = text;
    textarea.dispatchEvent(new Event('input'));
    await settle();
    return textarea;
  };
  const button = (label: string) =>
    Array.from(element.querySelectorAll('button')).find((b) =>
      b.textContent.trim().includes(label),
    );
  return { fixture, element, port, settle, type, button };
}

describe('AnalyticsChatComponent', () => {
  it('shows example questions and the read-only context', async () => {
    const { element } = await render();
    expect(element.textContent).toContain('Top 5 customers by revenue this year');
    expect(element.textContent).toContain('ERP prod');
    expect(element.textContent).toContain('Read-only');
  });

  it('asks on Enter, streams progress and renders the governed answer', async () => {
    const { element, port, settle, type } = await render(['analytics.query', 'analytics.export']);
    const textarea = await type('Total sales by region');
    textarea.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter' }));
    await settle();
    await Promise.resolve();
    const ask = port.commands[0]!;
    expect(ask).toMatchObject({
      kind: 'ask',
      request: { question: 'Total sales by region', connectionId: CONNECTION_ID, mode: 'execute' },
    });
    const ref = ask.ref;
    port.emit({ kind: 'accepted', ref, queryId: QUERY_ID });
    port.emit({
      kind: 'progress',
      ref,
      event: {
        queryId: QUERY_ID,
        step: 'EXECUTE_READONLY_QUERY',
        status: 'started',
        message: 'Running read-only query',
        elapsedMs: 900,
      },
    });
    await settle();
    expect(element.textContent).toContain('Running read-only query');
    expect(element.querySelector('[aria-busy="true"]')).not.toBeNull();

    port.emit({ kind: 'result', ref, response: queryResponse() });
    await settle();
    const text = element.textContent;
    expect(text).toContain('South has the highest total sales'); // summary after the chart
    expect(text).toContain('CSV'); // export allowed
    expect(text).toContain('Was this answer right?');
    // A chart answer shows only the summary and the chart - no table/plan/details tabs.
    expect(element.querySelector('[data-testid="chart-answer"]')).not.toBeNull();
    expect(element.querySelector('[role="tablist"]')).toBeNull();
  });

  it('explains refusals and hides export without the permission', async () => {
    const { element, port, settle, type, button } = await render();
    await type('Show customer bank account numbers');
    button('Ask')!.click();
    await settle();
    await Promise.resolve();
    const ref = port.commands[0]!.ref;
    port.emit({
      kind: 'result',
      ref,
      response: queryResponse({
        status: 'refused',
        result: null,
        refusal_reason: 'FORBIDDEN_FIELD',
      }),
    });
    await settle();
    expect(element.textContent).toContain('restricted data');
    expect(element.textContent).not.toContain('CSV');
  });

  it('offers retry after a lost connection and supports plan-only questions', async () => {
    const { element, port, settle, type, button } = await render();
    await type('Monthly sales trend');
    button('Plan only')!.click();
    await settle();
    await Promise.resolve();
    expect(port.commands[0]).toMatchObject({ request: { mode: 'plan' } });
    port.emit({
      kind: 'error',
      ref: port.commands[0]!.ref,
      error: { code: 'NETWORK_ERROR', message: 'Lost connection.', status: 0 },
      response: null,
    });
    await settle();
    expect(element.textContent).toContain('Connection problem');
    button('Try again')!.click();
    await settle();
    await Promise.resolve();
    expect(port.commands).toHaveLength(2);
    expect(JSON.stringify(apiResponse())).toContain('tenant_code'); // fixture sanity
  });

  it('starts a new chat from the header', async () => {
    const { element, port, settle, type, button } = await render();
    expect(button('Clear')).toBeUndefined(); // replaced by "New chat"
    expect(button('New chat')!.disabled).toBe(true); // nothing to start over from yet

    await type('Total sales by region');
    button('Ask')!.click();
    await settle();
    await Promise.resolve();
    expect(button('New chat')!.disabled).toBe(true); // still answering
    port.emit({ kind: 'result', ref: port.commands[0]!.ref, response: queryResponse() });
    await settle();
    expect(button('New chat')!.disabled).toBe(false);

    button('New chat')!.click();
    await settle();
    expect(element.textContent).toContain('Ask a question about your ERP data');
    expect(element.textContent).not.toContain('South has the highest total sales');

    await type('Top 5 customers by revenue this year');
    button('Ask')!.click();
    await settle();
    await Promise.resolve();
    const asks = port.commands.filter(
      (c): c is Extract<StreamCommand, { kind: 'ask' }> => c.kind === 'ask',
    );
    expect(asks).toHaveLength(2);
    expect(asks[1]!.request.conversationId).not.toBe(asks[0]!.request.conversationId);
  });
});
