import { Injectable, computed, inject } from '@angular/core';
import { Observable, tap } from 'rxjs';

import { AuthService } from '../core/auth/auth.service';
import { BackgroundBridgeService } from '../core/messaging/background-bridge.service';
import type {
  ConnectionChanges,
  ConnectionInput,
  ConnectionSummary,
  DataSourceStatus,
} from '../core/messaging/protocol';
import { WorkspaceService } from './workspace.service';

export const MANAGE_PERMISSION = 'connections.manage';

/**
 * Data-source administration for users with `connections.manage` (tenant and data
 * admins). The server enforces the permission; hiding the controls is only UX.
 */
@Injectable({ providedIn: 'root' })
export class DataSourceAdminService {
  private readonly bridge = inject(BackgroundBridgeService);
  private readonly auth = inject(AuthService);
  private readonly workspace = inject(WorkspaceService);

  readonly canManage = computed(
    () => this.auth.user()?.permissions.includes(MANAGE_PERMISSION) ?? false,
  );

  create(input: ConnectionInput): Observable<ConnectionSummary> {
    return this.bridge
      .request<'connections.create'>({ type: 'connections.create', payload: input })
      .pipe(tap(() => this.workspace.load()));
  }

  update(id: string, changes: ConnectionChanges): Observable<ConnectionSummary> {
    return this.bridge
      .request<'connections.update'>({ type: 'connections.update', payload: { id, changes } })
      .pipe(tap(() => this.workspace.load()));
  }

  remove(id: string): Observable<null> {
    return this.bridge
      .request<'connections.delete'>({ type: 'connections.delete', payload: { id } })
      .pipe(tap(() => this.workspace.load()));
  }

  sync(id: string): Observable<DataSourceStatus> {
    return this.bridge.request<'connections.sync'>({ type: 'connections.sync', payload: { id } });
  }

  index(id: string): Observable<DataSourceStatus> {
    return this.bridge.request<'connections.index'>({
      type: 'connections.index',
      payload: { id },
    });
  }

  status(id: string): Observable<DataSourceStatus> {
    return this.bridge.request<'connections.status'>({
      type: 'connections.status',
      payload: { id },
    });
  }
}

/** A sync or index job is still working. */
export function isBusy(status: DataSourceStatus): boolean {
  const running = new Set(['pending', 'running', 'building']);
  return running.has(status.metadata.status ?? '') || running.has(status.semantic.status ?? '');
}

/** One-line, user-facing summary of a data source's readiness. */
export function describeStatus(status: DataSourceStatus): string {
  const m = status.metadata;
  const s = status.semantic;
  if (m.status === 'failed') return `Table discovery failed (${m.errorCode ?? 'error'}).`;
  if (m.status === 'pending' || m.status === 'running') return 'Discovering tables…';
  if (m.status === null) return 'Tables not discovered yet - run "Sync tables".';
  const tables = `${m.objectCount ?? 0} tables/views discovered`;
  if (s.status === 'pending' || s.status === 'building') return `${tables}; building the index…`;
  if (s.status === 'failed') {
    return s.errorCode === 'NO_ENTITY_BOUND'
      ? `${tables}; no semantic entities describe this database yet.`
      : `${tables}; index build failed (${s.errorCode ?? 'error'}).`;
  }
  if (s.activeVersion === null) return `${tables}; index not built yet - run "Build index".`;
  return `${tables}; ready for questions (${s.entitiesBound} entities, index v${s.activeVersion}).`;
}
