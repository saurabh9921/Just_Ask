import { signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { of, type Observable } from 'rxjs';

import { AuthService } from '../core/auth/auth.service';
import { BackgroundBridgeService } from '../core/messaging/background-bridge.service';
import type {
  ConnectionSummary,
  DataSourceStatus,
  ExtensionRequest,
  UserInfo,
} from '../core/messaging/protocol';
import { ConnectionsListComponent, STATUS_POLL_MS } from './connections-list.component';

const CONNECTION: ConnectionSummary = {
  id: 'c1',
  name: 'ERP prod',
  connectorType: 'postgresql',
  host: 'db.internal',
  port: 5432,
  databaseName: 'erp',
  schemaName: 'reporting',
  status: 'ok',
  lastTestedAt: null,
  readOnly: true,
  hasSecret: true,
  username: 'erp_ro',
  tlsMode: 'verify-full',
  maxRows: 1000,
  queryTimeoutSeconds: 30,
  readReplicaHost: null,
  readReplicaPort: null,
};

function status(
  metadata: string | null,
  semantic: string | null,
  extra: Partial<DataSourceStatus['semantic']> = {},
): DataSourceStatus {
  return {
    metadata: { status: metadata, objectCount: 12, syncedAt: null, errorCode: null },
    semantic: {
      status: semantic,
      activeVersion: null,
      entitiesBound: 0,
      errorCode: null,
      ...extra,
    },
  };
}

const TEST_OK = {
  ok: true,
  latencyMs: 5,
  serverVersion: '17',
  errorCode: null,
  message: null,
};

function user(permissions: string[]): UserInfo {
  return {
    id: 'u1',
    email: 'admin@demo.test',
    displayName: 'Admin',
    tenant: { id: 't1', slug: 'demo', name: 'Demo' },
    roles: [],
    permissions,
  };
}

async function render(
  permissions: string[],
  handler: (message: ExtensionRequest) => Observable<unknown> | undefined = () => undefined,
) {
  const request = vi.fn(
    (message: ExtensionRequest) =>
      handler(message) ??
      (message.type === 'connections.list' ? of([CONNECTION]) : of({ connectionId: 'c1' })),
  );
  TestBed.configureTestingModule({
    imports: [ConnectionsListComponent],
    providers: [
      { provide: BackgroundBridgeService, useValue: { request } },
      {
        provide: AuthService,
        useValue: { user: signal(user(permissions)), sessionEnded: vi.fn() },
      },
      { provide: STATUS_POLL_MS, useValue: 1 },
    ],
  });
  const fixture = TestBed.createComponent(ConnectionsListComponent);
  fixture.detectChanges();
  await fixture.whenStable();
  fixture.detectChanges();
  const element = fixture.nativeElement as HTMLElement;
  const button = (label: string) =>
    Array.from(element.querySelectorAll('button')).find((b) => b.textContent.trim() === label);
  const settle = async () => {
    for (let i = 0; i < 10; i++) {
      await new Promise((resolve) => setTimeout(resolve, 5));
      fixture.detectChanges();
    }
  };
  return { request, element, button, fixture, settle };
}

describe('Data sources administration', () => {
  it('shows no management controls without connections.manage', async () => {
    const { button, element } = await render(['analytics.query', 'connections.read']);
    expect(button('Add data source')).toBeUndefined();
    expect(button('Sync tables')).toBeUndefined();
    expect(button('Edit')).toBeUndefined();
    expect(button('Test connection')).toBeDefined();
    expect(element.textContent).toContain('configured by your data admin');
  });

  it('adds a data source, then tests, syncs and indexes it without further clicks', async () => {
    let stage: 'start' | 'sync' | 'index' = 'start';
    let polls = 0;
    const added: ConnectionSummary = { ...CONNECTION, id: 'c2', name: 'ERP test' };
    let created = false;
    const { request, element, button, fixture, settle } = await render(
      ['connections.read', 'connections.manage'],
      (m) => {
        switch (m.type) {
          case 'connections.list':
            return of(created ? [CONNECTION, added] : [CONNECTION]);
          case 'connections.create':
            created = true;
            return of(added);
          case 'connections.test':
            return of(TEST_OK);
          case 'connections.sync':
            stage = 'sync';
            polls = 0;
            return of(status('running', null));
          case 'connections.index':
            stage = 'index';
            polls = 0;
            return of(status('succeeded', 'building'));
          case 'connections.status':
            polls += 1;
            if (stage === 'sync') return of(status(polls < 2 ? 'running' : 'succeeded', null));
            if (stage === 'index') {
              return of(
                status('succeeded', polls < 2 ? 'building' : 'active', {
                  activeVersion: 1,
                  entitiesBound: 31,
                }),
              );
            }
            return of(status(null, null));
          default:
            return undefined;
        }
      },
    );
    button('Add data source')!.click();
    fixture.detectChanges();
    const set = (name: string, value: string) => {
      const input = element.querySelector<HTMLInputElement>(`[formcontrolname="${name}"]`)!;
      input.value = value;
      input.dispatchEvent(new Event('input'));
    };
    set('name', 'ERP test');
    set('host', 'host.docker.internal');
    set('databaseName', 'erp');
    set('username', 'erp_ro');
    set('password', 'pw-123456789');
    fixture.detectChanges();
    element.querySelector<HTMLFormElement>('form')!.dispatchEvent(new Event('submit'));
    await settle();

    const create = request.mock.calls.find(([m]) => m.type === 'connections.create')![0];
    expect(create).toMatchObject({
      payload: {
        name: 'ERP test',
        connectorType: 'postgresql',
        host: 'host.docker.internal',
        port: 5432,
        databaseName: 'erp',
        username: 'erp_ro',
        password: 'pw-123456789',
        tlsMode: 'verify-full',
      },
    });
    const types = request.mock.calls.map(([m]) => m.type);
    const order = (['connections.test', 'connections.sync', 'connections.index'] as const).map(
      (t) => types.indexOf(t),
    );
    expect(order.every((i) => i >= 0)).toBe(true);
    expect([...order].sort((a, b) => a - b)).toEqual(order); // test → sync → index
    expect(types.filter((t) => t === 'connections.sync')).toHaveLength(1);
    expect(types.filter((t) => t === 'connections.index')).toHaveLength(1);
    expect(request.mock.calls.find(([m]) => m.type === 'connections.sync')![0]).toEqual({
      type: 'connections.sync',
      payload: { id: 'c2' },
    });
    const pipeline = element.querySelector('[data-testid="pipeline-c2"]')!.textContent;
    expect(pipeline).toContain('Connection tested');
    expect(pipeline).toContain('Tables synced');
    expect(pipeline).toContain('Index built successfully');
    expect(pipeline).toContain('ready for questions (31 entities)');
  });

  it('shows each stage while the tables sync', async () => {
    let syncing = false; // unsynced at first, then the sync keeps running
    const { request, element, button, settle } = await render(
      ['connections.read', 'connections.manage'],
      (m) => {
        if (m.type === 'connections.test') return of(TEST_OK);
        if (m.type === 'connections.sync') {
          syncing = true;
          return of(status('running', null));
        }
        if (m.type === 'connections.status') return of(status(syncing ? 'running' : null, null));
        return undefined;
      },
    );
    button('Test connection')!.click();
    await settle();
    const pipeline = element.querySelector('[data-testid="pipeline-c1"]')!;
    expect(pipeline.textContent).toContain('Connection tested');
    expect(pipeline.textContent).toContain('Syncing tables…');
    expect(pipeline.querySelector('.step--running')).not.toBeNull();
    expect(request.mock.calls.some(([m]) => m.type === 'connections.index')).toBe(false);
    expect(button('Sync tables')!.disabled).toBe(true); // no duplicate run meanwhile
  });

  it('does not sync or index again when the source is already synced and indexed', async () => {
    const { request, element, button, settle } = await render(
      ['connections.read', 'connections.manage'],
      (m) => {
        if (m.type === 'connections.test') return of(TEST_OK);
        if (m.type === 'connections.status') {
          return of(status('succeeded', 'active', { activeVersion: 3, entitiesBound: 5 }));
        }
        return undefined;
      },
    );
    button('Test connection')!.click();
    await settle();
    const types = request.mock.calls.map(([m]) => m.type);
    expect(types).not.toContain('connections.sync');
    expect(types).not.toContain('connections.index');
    const pipeline = element.querySelector('[data-testid="pipeline-c1"]')!.textContent;
    expect(pipeline).toContain('Tables already synced');
    expect(pipeline).toContain('Index already built');
    expect(pipeline).toContain('Already synced and indexed');
  });

  it('builds only the missing index when the tables are already synced', async () => {
    let indexed = false;
    const { request, element, button, settle } = await render(
      ['connections.read', 'connections.manage'],
      (m) => {
        if (m.type === 'connections.test') return of(TEST_OK);
        if (m.type === 'connections.index') {
          indexed = true;
          return of(status('succeeded', 'active', { activeVersion: 1, entitiesBound: 4 }));
        }
        if (m.type === 'connections.status') {
          return of(
            indexed
              ? status('succeeded', 'active', { activeVersion: 1, entitiesBound: 4 })
              : status('succeeded', null),
          );
        }
        return undefined;
      },
    );
    button('Test connection')!.click();
    await settle();
    const types = request.mock.calls.map(([m]) => m.type);
    expect(types).not.toContain('connections.sync');
    expect(types).toContain('connections.index');
    const pipeline = element.querySelector('[data-testid="pipeline-c1"]')!.textContent;
    expect(pipeline).toContain('Tables already synced');
    expect(pipeline).toContain('Index built successfully');
  });

  it('stops with a clear message when the table sync fails', async () => {
    const failed: DataSourceStatus = {
      ...status('failed', null),
      metadata: { status: 'failed', objectCount: null, syncedAt: null, errorCode: 'AUTH_FAILED' },
    };
    const { request, element, button, settle } = await render(
      ['connections.read', 'connections.manage'],
      (m) => {
        if (m.type === 'connections.test') return of(TEST_OK);
        if (m.type === 'connections.sync') return of(failed);
        if (m.type === 'connections.status') return of(status(null, null));
        return undefined;
      },
    );
    button('Test connection')!.click();
    await settle();
    const pipeline = element.querySelector('[data-testid="pipeline-c1"]')!;
    expect(pipeline.textContent).toContain('Table sync failed');
    expect(pipeline.textContent).toContain('AUTH_FAILED');
    expect(pipeline.querySelector('.pipeline__message--error')).not.toBeNull();
    expect(request.mock.calls.some(([m]) => m.type === 'connections.index')).toBe(false);
    expect(button('Sync tables')!.disabled).toBe(false); // the user can retry
  });

  it('explains a missing semantic layer when the index has nothing to bind', async () => {
    const { element, button, settle } = await render(
      ['connections.read', 'connections.manage'],
      (m) => {
        if (m.type === 'connections.test') return of(TEST_OK);
        if (m.type === 'connections.index' || m.type === 'connections.status') {
          return of(status('succeeded', 'failed', { errorCode: 'NO_ENTITY_BOUND' }));
        }
        return undefined;
      },
    );
    button('Test connection')!.click();
    await settle();
    const pipeline = element.querySelector('[data-testid="pipeline-c1"]')!.textContent;
    expect(pipeline).toContain('Index build failed');
    expect(pipeline).toContain('no semantic entities describe this database yet');
    expect(element.querySelector('[data-testid="guide-c1"]')).not.toBeNull();
  });

  it('prepares nothing after a failed test or for users who cannot manage sources', async () => {
    const failing = await render(['connections.read', 'connections.manage'], (m) =>
      m.type === 'connections.test'
        ? of({ ...TEST_OK, ok: false, errorCode: 'AUTH_FAILED', message: 'Authentication failed.' })
        : undefined,
    );
    failing.button('Test connection')!.click();
    await failing.settle();
    expect(failing.request.mock.calls.map(([m]) => m.type)).not.toContain('connections.sync');
    expect(failing.element.querySelector('[data-testid="pipeline-c1"]')).toBeNull();

    TestBed.resetTestingModule();
    const viewer = await render(['analytics.query', 'connections.read'], (m) =>
      m.type === 'connections.test' ? of(TEST_OK) : undefined,
    );
    viewer.button('Test connection')!.click();
    await viewer.settle();
    const types = viewer.request.mock.calls.map(([m]) => m.type);
    expect(types).toContain('connections.test');
    expect(types).not.toContain('connections.status');
    expect(types).not.toContain('connections.sync');
  });

  it('asks for confirmation before deleting', async () => {
    const { request, button, fixture, element } = await render([
      'connections.read',
      'connections.manage',
    ]);
    button('Delete')!.click();
    fixture.detectChanges();
    expect(request.mock.calls.some(([m]) => m.type === 'connections.delete')).toBe(false);
    expect(element.textContent).toContain('Delete "ERP prod"?');
    Array.from(element.querySelectorAll('button'))
      .filter((b) => b.textContent.trim() === 'Delete')
      .at(-1)!
      .click();
    expect(request.mock.calls.some(([m]) => m.type === 'connections.delete')).toBe(true);
  });

  it('explains how to create the semantic layer when nothing describes the database', async () => {
    const { button, element, settle } = await render(
      ['connections.read', 'connections.manage'],
      (m) =>
        m.type === 'connections.index'
          ? of(status('succeeded', 'failed', { errorCode: 'NO_ENTITY_BOUND' }))
          : undefined,
    );
    button('Build index')!.click();
    await settle();
    expect(element.textContent).toContain('no semantic entities describe this database yet');
    const guide = element.querySelector('[data-testid="guide-c1"]')!;
    expect(guide.textContent).toContain('--tenant demo');
    expect(guide.textContent).toContain('draft --name "ERP prod"');
  });

  it('edits without resending the password unless one is typed', async () => {
    const { request, button, element, fixture, settle } = await render(
      ['connections.read', 'connections.manage'],
      (m) => (m.type === 'connections.update' ? of(CONNECTION) : undefined),
    );
    button('Edit')!.click();
    fixture.detectChanges();
    const rows = element.querySelector<HTMLInputElement>('[formcontrolname="maxRows"]')!;
    rows.value = '250';
    rows.dispatchEvent(new Event('input'));
    fixture.detectChanges();
    element.querySelector<HTMLFormElement>('form')!.dispatchEvent(new Event('submit'));
    await settle();
    const update = request.mock.calls.find(([m]) => m.type === 'connections.update')![0] as {
      payload: { id: string; changes: Record<string, unknown> };
    };
    expect(update.payload.id).toBe('c1');
    expect(update.payload.changes['maxRows']).toBe(250);
    expect('password' in update.payload.changes).toBe(false);
    expect('connectorType' in update.payload.changes).toBe(false);
  });
});
