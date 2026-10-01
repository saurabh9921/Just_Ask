import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';

import { AuthService } from '../core/auth/auth.service';
import { BackgroundBridgeService, BridgeError } from '../core/messaging/background-bridge.service';
import type { ConnectionSummary, ExtensionRequest, UserInfo } from '../core/messaging/protocol';
import { WorkspaceSelectorComponent } from './workspace-selector.component';

function connection(id: string, name: string, status: ConnectionSummary['status']) {
  return {
    id,
    name,
    connectorType: 'postgresql',
    host: 'db',
    port: 5432,
    databaseName: 'erp',
    schemaName: null,
    status,
    lastTestedAt: null,
    readOnly: true,
    hasSecret: true,
  } satisfies ConnectionSummary;
}

const USER = {
  id: 'u1',
  email: 'ana@acme.test',
  displayName: 'Ana',
  tenant: { id: 't1', slug: 'acme', name: 'Acme Manufacturing' },
  roles: ['analyst'],
  permissions: [],
} satisfies UserInfo;

async function render(
  connections: readonly ConnectionSummary[] | Error,
  storedId: string | null = null,
) {
  const request = vi.fn((message: ExtensionRequest) => {
    switch (message.type) {
      case 'connections.list':
        return connections instanceof Error ? throwError(() => connections) : of(connections);
      case 'workspace.get':
        return of({ connectionId: storedId });
      case 'workspace.set':
        return of(message.payload);
      default:
        return of(null);
    }
  });
  const sessionEnded = vi.fn();
  TestBed.configureTestingModule({
    imports: [WorkspaceSelectorComponent],
    providers: [
      { provide: BackgroundBridgeService, useValue: { request } },
      { provide: AuthService, useValue: { user: signal(USER), sessionEnded } },
    ],
  });
  const fixture = TestBed.createComponent(WorkspaceSelectorComponent);
  fixture.detectChanges();
  await fixture.whenStable();
  fixture.detectChanges();
  const element = fixture.nativeElement as HTMLElement;
  return { fixture, element, request, sessionEnded };
}

const CONNECTIONS = [connection('c1', 'ERP prod', 'ok'), connection('c2', 'ERP replica', 'error')];

describe('WorkspaceSelectorComponent', () => {
  it('shows the tenant and a labelled data source picker', async () => {
    const { element } = await render(CONNECTIONS, 'c2');
    expect(element.querySelector('[data-testid="tenant-name"]')?.textContent).toContain(
      'Acme Manufacturing',
    );
    const select = element.querySelector<HTMLSelectElement>('select')!;
    expect(element.querySelector(`label[for="${select.id}"]`)?.textContent).toContain(
      'Data source',
    );
    expect(Array.from(select.options).map((o) => o.textContent.trim())).toEqual([
      'ERP prod (postgresql)',
      'ERP replica (postgresql)',
    ]);
    expect(select.value).toBe('c2');
    expect(element.textContent).toContain('Connection error');
    expect(element.textContent).toContain('Read-only');
  });

  it('falls back to the first connection and persists it when nothing is stored', async () => {
    const { element, request } = await render(CONNECTIONS, 'deleted-connection');
    expect(element.querySelector('select')!.value).toBe('c1');
    expect(request).toHaveBeenCalledWith({
      type: 'workspace.set',
      payload: { connectionId: 'c1' },
    });
  });

  it('persists a new selection', async () => {
    const { fixture, element, request } = await render(CONNECTIONS, 'c1');
    const select = element.querySelector<HTMLSelectElement>('select')!;
    select.value = 'c2';
    select.dispatchEvent(new Event('change'));
    fixture.detectChanges();
    expect(request).toHaveBeenCalledWith({
      type: 'workspace.set',
      payload: { connectionId: 'c2' },
    });
    expect(element.textContent).toContain('Connection error');
  });

  it('shows an empty state when no connections are available', async () => {
    const { element } = await render([]);
    expect(element.querySelector('[data-testid="no-connections"]')?.textContent).toContain(
      'Ask your data admin to add a connection',
    );
    expect(element.querySelector('select')).toBeNull();
  });

  it('shows an error with retry and ends the session on SESSION_EXPIRED', async () => {
    const { element, sessionEnded } = await render(
      new BridgeError('SESSION_EXPIRED', 'Your session has ended. Sign in again.'),
    );
    expect(element.querySelector('[role="alert"]')?.textContent).toContain(
      'Your session has ended',
    );
    expect(sessionEnded).toHaveBeenCalled();
  });
});
