import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';

import { AuthService } from '../core/auth/auth.service';
import { BackgroundBridgeService, BridgeError } from '../core/messaging/background-bridge.service';
import type { ConnectionSummary, ExtensionRequest } from '../core/messaging/protocol';
import { ConnectionsListComponent } from './connections-list.component';

const CONNECTION: ConnectionSummary = {
  id: 'c1',
  name: 'ERP prod',
  connectorType: 'postgresql',
  host: 'db.internal',
  port: 5432,
  databaseName: 'erp',
  schemaName: 'analytics',
  status: 'untested',
  lastTestedAt: null,
  readOnly: true,
  hasSecret: true,
};

async function render(test: () => unknown) {
  const request = vi.fn((message: ExtensionRequest) => {
    switch (message.type) {
      case 'connections.list':
        return of([CONNECTION]);
      case 'connections.test':
        return test();
      default:
        return of({ connectionId: 'c1' });
    }
  });
  TestBed.configureTestingModule({
    imports: [ConnectionsListComponent],
    providers: [
      { provide: BackgroundBridgeService, useValue: { request } },
      { provide: AuthService, useValue: { user: signal(null), sessionEnded: vi.fn() } },
    ],
  });
  const fixture = TestBed.createComponent(ConnectionsListComponent);
  fixture.detectChanges();
  await fixture.whenStable();
  fixture.detectChanges();
  const element = fixture.nativeElement as HTMLElement;
  const clickTest = async () => {
    Array.from(element.querySelectorAll('button'))
      .find((b) => b.textContent.includes('Test connection'))!
      .click();
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
  };
  return { element, clickTest, request };
}

describe('ConnectionsListComponent', () => {
  it('lists connections with location, status and read-only badge', async () => {
    const { element } = await render(() => of(null));
    expect(element.textContent).toContain('ERP prod');
    expect(element.textContent).toContain('db.internal:5432/erp');
    expect(element.textContent).toContain('Not tested');
    expect(element.textContent).toContain('Read-only');
  });

  it('runs a connection test and updates the status', async () => {
    const { element, clickTest, request } = await render(() =>
      of({ ok: true, latencyMs: 12.4, serverVersion: '18.6', errorCode: null, message: null }),
    );
    await clickTest();
    expect(request).toHaveBeenCalledWith({ type: 'connections.test', payload: { id: 'c1' } });
    expect(element.querySelector('[data-testid="test-c1"]')?.textContent).toContain(
      'Connection succeeded in 12 ms (18.6).',
    );
    expect(element.textContent).toContain('Connected');
  });

  it('reports a failed test from the server', async () => {
    const { element, clickTest } = await render(() =>
      of({
        ok: false,
        latencyMs: null,
        serverVersion: null,
        errorCode: 'CONNECTION_FAILED',
        message: 'Could not connect to the database.',
      }),
    );
    await clickTest();
    expect(element.textContent).toContain('Could not connect to the database.');
    expect(element.textContent).toContain('Connection error');
  });

  it('reports a transport failure', async () => {
    const { element, clickTest } = await render(() =>
      throwError(() => new BridgeError('PERMISSION_DENIED', 'You do not have permission.')),
    );
    await clickTest();
    expect(element.querySelector('[data-testid="test-c1"]')?.textContent).toContain(
      'You do not have permission.',
    );
  });
});
