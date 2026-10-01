import { DatePipe } from '@angular/common';
import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  InjectionToken,
  computed,
  inject,
  signal,
  type OnInit,
} from '@angular/core';
import { MatButtonModule } from '@angular/material/button';
import {
  EmptyError,
  Subscription,
  first,
  map,
  of,
  switchMap,
  take,
  takeWhile,
  tap,
  timer,
  type Observable,
} from 'rxjs';

import { AuthService } from '../core/auth/auth.service';
import { describeBridgeError } from '../core/messaging/background-bridge.service';
import type {
  ConnectionSummary,
  ConnectionTestResult,
  DataSourceStatus,
} from '../core/messaging/protocol';
import { ErrorStateComponent } from '../shared/error-state/error-state.component';
import { ConnectionHealthBadgeComponent } from './connection-health-badge.component';
import { DataSourceAdminService, describeStatus, isBusy } from './data-source-admin.service';
import { DataSourceFormComponent } from './data-source-form.component';
import { WorkspaceService } from './workspace.service';

/** Poll interval while a sync/index job runs (injectable for tests). */
export const STATUS_POLL_MS = new InjectionToken<number>('STATUS_POLL_MS', {
  providedIn: 'root',
  factory: () => 2000,
});
const MAX_POLLS = 450; // 15 minutes at 2 s

type TestState =
  | { readonly kind: 'running' }
  | { readonly kind: 'done'; readonly result: ConnectionTestResult }
  | { readonly kind: 'failed'; readonly message: string };

type StatusState =
  | { readonly kind: 'loading' }
  | { readonly kind: 'ready'; readonly status: DataSourceStatus }
  | { readonly kind: 'failed'; readonly message: string };

type StepState = 'pending' | 'running' | 'done' | 'skipped' | 'failed';

/** Automatic preparation after a successful test: sync tables, then build the index. */
interface PipelineState {
  readonly sync: StepState;
  readonly index: StepState;
  readonly message: string;
}

const RUNNING = new Set(['pending', 'running', 'building']);
const IDENTITY_FIELDS = [
  'connectorType',
  'host',
  'port',
  'databaseName',
  'schemaName',
  'username',
] as const;

class PipelineError extends Error {}

/**
 * Data sources. Everyone sees the sources they may query and can test them; users with
 * `connections.manage` (tenant/data admins) can also add, edit, delete, discover tables
 * and build the semantic index. Passwords are write-only and never shown.
 */
@Component({
  selector: 'eia-connections-list',
  imports: [
    DatePipe,
    MatButtonModule,
    ConnectionHealthBadgeComponent,
    DataSourceFormComponent,
    ErrorStateComponent,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <section class="page" aria-labelledby="connections-title">
      <div class="page__head">
        <h1 id="connections-title" class="page__title">Data sources</h1>
        @if (admin.canManage() && editing() === null) {
          <button mat-flat-button type="button" (click)="editing.set('new')">
            Add data source
          </button>
        }
      </div>
      <p class="page__subtitle">
        @if (admin.canManage()) {
          Connect your ERP databases with a read-only account, discover their tables and build the
          index the assistant answers from.
        } @else {
          Connections are configured by your data admin and always use a read-only database role.
        }
      </p>

      @if (editing() === 'new') {
        <eia-data-source-form (saved)="created($event)" (cancelled)="editing.set(null)" />
      }

      @if (workspace.error(); as error) {
        <eia-error-state
          title="Could not load data sources"
          [message]="error"
          [retryable]="true"
          (retry)="workspace.load()"
        />
      } @else if (workspace.loading() && !workspace.loaded()) {
        <p role="status">Loading data sources…</p>
      } @else if (workspace.connections().length === 0) {
        <p data-testid="no-connections">
          @if (admin.canManage()) {
            No data sources yet. Use "Add data source" to connect your ERP database.
          } @else {
            No data sources are available to you yet. Ask your data admin to add a connection.
          }
        </p>
      } @else {
        <ul class="list">
          @for (connection of workspace.connections(); track connection.id) {
            <li class="card" [attr.aria-labelledby]="'conn-' + connection.id">
              @if (editing() === connection.id) {
                <eia-data-source-form
                  [connection]="connection"
                  (saved)="edited(connection, $event)"
                  (cancelled)="editing.set(null)"
                />
              } @else {
                <div class="card__head">
                  <h2 class="card__title" [id]="'conn-' + connection.id">{{ connection.name }}</h2>
                  <eia-connection-health-badge [status]="connection.status" />
                  <span class="readonly">Read-only</span>
                </div>
                <dl class="card__meta">
                  <div>
                    <dt>Type</dt>
                    <dd>{{ connection.connectorType }}</dd>
                  </div>
                  <div>
                    <dt>Location</dt>
                    <dd>{{ location(connection) }}</dd>
                  </div>
                  <div>
                    <dt>Last tested</dt>
                    <dd>
                      {{
                        connection.lastTestedAt
                          ? (connection.lastTestedAt | date: 'medium')
                          : 'Never'
                      }}
                    </dd>
                  </div>
                </dl>

                @if (pipelines()[connection.id]; as p) {
                  <div class="pipeline" [attr.data-testid]="'pipeline-' + connection.id">
                    <ol class="steps" aria-label="Preparing this data source">
                      <li class="step step--done">
                        <span class="step__mark" aria-hidden="true"></span>Connection tested
                      </li>
                      <li class="step step--{{ p.sync }}">
                        <span class="step__mark" aria-hidden="true"></span>{{ syncLabel(p.sync) }}
                      </li>
                      <li class="step step--{{ p.index }}">
                        <span class="step__mark" aria-hidden="true"></span>{{ indexLabel(p.index) }}
                      </li>
                    </ol>
                    <p
                      class="pipeline__message"
                      role="status"
                      [class.pipeline__message--error]="p.sync === 'failed' || p.index === 'failed'"
                    >
                      {{ p.message }}
                    </p>
                  </div>
                }

                @if (admin.canManage()) {
                  <p class="status" role="status" [attr.data-testid]="'status-' + connection.id">
                    @switch (statuses()[connection.id]?.kind) {
                      @case ('loading') {
                        Checking status…
                      }
                      @case ('ready') {
                        {{ statusText(connection.id) }}
                      }
                      @case ('failed') {
                        {{ statusError(connection.id) }}
                      }
                      @default {
                        <button mat-button type="button" (click)="refresh(connection.id)">
                          Show status
                        </button>
                      }
                    }
                  </p>
                  @if (needsEntities(connection.id)) {
                    <div class="guide" [attr.data-testid]="'guide-' + connection.id">
                      <p>
                        The assistant needs a <strong>semantic layer</strong> that describes this
                        database's tables in business terms. An administrator drafts it from the
                        discovered tables on the server, reviews it, then clicks "Build index":
                      </p>
                      <code
                        >python scripts/dev.py onboard --tenant {{ tenant() }} --email
                        {{ email() }} draft --name "{{ connection.name }}" --list</code
                      >
                      <code
                        >python scripts/dev.py onboard --tenant {{ tenant() }} --email
                        {{ email() }} draft --name "{{ connection.name }}" --objects
                        "&lt;tables&gt;"</code
                      >
                    </div>
                  }
                }

                <div class="card__actions">
                  <button
                    mat-stroked-button
                    type="button"
                    [disabled]="tests()[connection.id]?.kind === 'running'"
                    (click)="test(connection.id)"
                  >
                    Test connection
                  </button>
                  @if (admin.canManage()) {
                    <button
                      mat-stroked-button
                      type="button"
                      [disabled]="busy(connection.id)"
                      (click)="sync(connection.id)"
                    >
                      Sync tables
                    </button>
                    <button
                      mat-stroked-button
                      type="button"
                      [disabled]="busy(connection.id)"
                      (click)="index(connection.id)"
                    >
                      Build index
                    </button>
                    <button mat-button type="button" (click)="editing.set(connection.id)">
                      Edit
                    </button>
                    @if (confirmDelete() === connection.id) {
                      <span class="confirm" role="alert">
                        Delete "{{ connection.name }}"? Its stored password is destroyed.
                      </span>
                      <button
                        mat-flat-button
                        type="button"
                        class="danger"
                        (click)="remove(connection.id)"
                      >
                        Delete
                      </button>
                      <button mat-button type="button" (click)="confirmDelete.set(null)">
                        Keep
                      </button>
                    } @else {
                      <button mat-button type="button" (click)="confirmDelete.set(connection.id)">
                        Delete
                      </button>
                    }
                  }
                  @if (tests()[connection.id]; as state) {
                    <span class="test" role="status" [attr.data-testid]="'test-' + connection.id">
                      @switch (state.kind) {
                        @case ('running') {
                          Testing…
                        }
                        @case ('done') {
                          {{ describe(state.result) }}
                        }
                        @case ('failed') {
                          {{ state.message }}
                        }
                      }
                    </span>
                  }
                </div>
                @if (actionErrors()[connection.id]; as message) {
                  <p class="error" role="alert">{{ message }}</p>
                }
              }
            </li>
          }
        </ul>
      }
    </section>
  `,
  styles: `
    .page__head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
    }
    .list {
      list-style: none;
      margin: 12px 0 0;
      padding: 0;
      display: grid;
      gap: 12px;
    }
    .card {
      padding: 12px;
      border-radius: 12px;
      border: 1px solid var(--mat-sys-outline-variant);
      background: var(--mat-sys-surface-container-low);
    }
    .card__head {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 8px;
    }
    .card__title {
      margin: 0;
      flex: 1 1 auto;
      font: var(--mat-sys-title-small);
    }
    .readonly {
      padding: 2px 8px;
      border-radius: 999px;
      font: var(--mat-sys-label-small);
      background: var(--mat-sys-tertiary-container);
      color: var(--mat-sys-on-tertiary-container);
    }
    .card__meta {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
      gap: 8px;
      margin: 8px 0;
    }
    .card__meta dt {
      font: var(--mat-sys-label-small);
      color: var(--mat-sys-on-surface-variant);
    }
    .card__meta dd {
      margin: 0;
      font: var(--mat-sys-body-small);
      overflow-wrap: anywhere;
    }
    .status {
      margin: 0 0 8px;
      font: var(--mat-sys-body-small);
    }
    .guide {
      margin: 0 0 8px;
      padding: 8px;
      border-radius: 8px;
      background: var(--mat-sys-secondary-container);
      color: var(--mat-sys-on-secondary-container);
      font: var(--mat-sys-body-small);
    }
    .guide p {
      margin: 0 0 6px;
    }
    .guide code {
      display: block;
      margin-top: 4px;
      overflow-wrap: anywhere;
      font-size: 11px;
    }
    .pipeline {
      margin: 0 0 8px;
      padding: 8px;
      border-radius: 8px;
      background: var(--mat-sys-surface-container);
    }
    .steps {
      list-style: none;
      margin: 0;
      padding: 0;
      display: flex;
      flex-wrap: wrap;
      gap: 4px 14px;
      font: var(--mat-sys-label-medium);
    }
    .step {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      color: var(--mat-sys-on-surface-variant);
    }
    .step__mark {
      width: 10px;
      height: 10px;
      border-radius: 50%;
      border: 2px solid currentColor;
      box-sizing: border-box;
    }
    .step--done,
    .step--skipped {
      color: var(--eia-status-ok);
    }
    .step--done .step__mark,
    .step--skipped .step__mark {
      background: currentColor;
    }
    .step--running {
      color: var(--mat-sys-primary);
    }
    .step--running .step__mark {
      border-right-color: transparent;
      animation: eia-spin 0.9s linear infinite;
    }
    .step--failed {
      color: var(--mat-sys-error);
    }
    .step--failed .step__mark {
      background: currentColor;
    }
    @keyframes eia-spin {
      to {
        transform: rotate(360deg);
      }
    }
    @media (prefers-reduced-motion: reduce) {
      .step--running .step__mark {
        animation: none;
      }
    }
    .pipeline__message {
      margin: 6px 0 0;
      font: var(--mat-sys-body-small);
    }
    .pipeline__message--error {
      color: var(--mat-sys-error);
    }
    .card__actions {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 8px;
    }
    .test,
    .confirm {
      font: var(--mat-sys-body-small);
    }
    .confirm {
      flex-basis: 100%;
    }
    .danger {
      background: var(--mat-sys-error);
      color: var(--mat-sys-on-error);
    }
    .error {
      margin: 8px 0 0;
      color: var(--mat-sys-error);
      font: var(--mat-sys-body-small);
    }
  `,
})
export class ConnectionsListComponent implements OnInit {
  protected readonly workspace = inject(WorkspaceService);
  protected readonly admin = inject(DataSourceAdminService);
  private readonly auth = inject(AuthService);
  private readonly pollMs = inject(STATUS_POLL_MS);

  protected readonly tests = signal<Readonly<Record<string, TestState>>>({});
  protected readonly statuses = signal<Readonly<Record<string, StatusState>>>({});
  protected readonly pipelines = signal<Readonly<Record<string, PipelineState>>>({});
  protected readonly actionErrors = signal<Readonly<Record<string, string>>>({});
  /** 'new', a connection id being edited, or null. */
  protected readonly editing = signal<string | null>(null);
  protected readonly confirmDelete = signal<string | null>(null);
  protected readonly tenant = computed(() => this.auth.user()?.tenant.slug ?? '<tenant>');
  protected readonly email = computed(() => this.auth.user()?.email ?? '<admin email>');

  private readonly polls = new Map<string, Subscription>();
  private readonly preparations = new Map<string, Subscription>();

  constructor() {
    inject(DestroyRef).onDestroy(() => {
      for (const sub of this.polls.values()) sub.unsubscribe();
      for (const sub of this.preparations.values()) sub.unsubscribe();
    });
  }

  ngOnInit(): void {
    this.workspace.load();
  }

  protected location(connection: ConnectionSummary): string {
    const host = connection.host ?? '';
    const port = connection.port === null ? '' : `:${connection.port}`;
    const db = connection.databaseName === null ? '' : `/${connection.databaseName}`;
    return `${host}${port}${db}` || '—';
  }

  protected describe(result: ConnectionTestResult): string {
    if (result.ok) {
      const latency = result.latencyMs === null ? '' : ` in ${Math.round(result.latencyMs)} ms`;
      const version = result.serverVersion === null ? '' : ` (${result.serverVersion})`;
      return `Connection succeeded${latency}${version}.`;
    }
    return result.message ?? 'Connection failed.';
  }

  protected statusText(id: string): string {
    const state = this.statuses()[id];
    return state?.kind === 'ready' ? describeStatus(state.status) : '';
  }

  protected statusError(id: string): string {
    const state = this.statuses()[id];
    return state?.kind === 'failed' ? state.message : '';
  }

  protected busy(id: string): boolean {
    const state = this.statuses()[id];
    return (
      state?.kind === 'loading' ||
      (state?.kind === 'ready' && isBusy(state.status)) ||
      this.preparing(id)
    );
  }

  protected syncLabel(state: StepState): string {
    switch (state) {
      case 'running':
        return 'Syncing tables…';
      case 'done':
        return 'Tables synced';
      case 'skipped':
        return 'Tables already synced';
      case 'failed':
        return 'Table sync failed';
      default:
        return 'Sync tables';
    }
  }

  protected indexLabel(state: StepState): string {
    switch (state) {
      case 'running':
        return 'Building index…';
      case 'done':
        return 'Index built successfully';
      case 'skipped':
        return 'Index already built';
      case 'failed':
        return 'Index build failed';
      default:
        return 'Build index';
    }
  }

  protected needsEntities(id: string): boolean {
    const state = this.statuses()[id];
    return (
      state?.kind === 'ready' &&
      state.status.metadata.status === 'succeeded' &&
      (state.status.semantic.errorCode === 'NO_ENTITY_BOUND' ||
        (state.status.semantic.activeVersion !== null && state.status.semantic.entitiesBound === 0))
    );
  }

  /**
   * Tests the connection. For data admins a successful test then prepares the source
   * automatically (sync tables, then build the index); `force` re-runs both even when
   * they look current (a new source, or changed connection details).
   */
  protected test(id: string, force = false): void {
    this.setTest(id, { kind: 'running' });
    this.workspace.test(id).subscribe({
      next: (result) => {
        this.setTest(id, { kind: 'done', result });
        if (result.ok && this.admin.canManage()) {
          this.prepare(id, force);
        }
      },
      error: (error: unknown) => {
        this.setTest(id, { kind: 'failed', message: describeBridgeError(error) });
      },
    });
  }

  /** A new source: test it, then discover its tables and build its index. */
  protected created(connection: ConnectionSummary): void {
    this.editing.set(null);
    this.test(connection.id, true);
  }

  /** Edited: test again; tables and index are rebuilt only if the connection itself changed. */
  protected edited(previous: ConnectionSummary, updated: ConnectionSummary): void {
    this.editing.set(null);
    const changed = IDENTITY_FIELDS.some((field) => previous[field] !== updated[field]);
    this.test(updated.id, changed);
  }

  private preparing(id: string): boolean {
    const p = this.pipelines()[id];
    return p !== undefined && (p.sync === 'running' || p.index === 'running');
  }

  /** Sync tables, then build the index, skipping steps that are already current. */
  private prepare(id: string, force: boolean): void {
    this.preparations.get(id)?.unsubscribe();
    this.polls.get(id)?.unsubscribe();
    this.setActionError(id, null);
    this.setPipeline(id, {
      sync: 'pending',
      index: 'pending',
      message: 'Connection tested. Checking tables and index…',
    });
    const sub = this.admin
      .status(id)
      .pipe(
        tap((status) => {
          this.setStatus(id, { kind: 'ready', status });
        }),
        switchMap((status) => {
          const synced = status.metadata.status === 'succeeded';
          const indexed = status.semantic.status === 'active';
          const needSync = force || !synced;
          if (!needSync && indexed) {
            this.setPipeline(id, {
              sync: 'skipped',
              index: 'skipped',
              message: 'Already synced and indexed - ready for questions.',
            });
            return of(null);
          }
          const synced$ = needSync ? this.runSync(id) : of(status);
          if (!needSync) this.patchPipeline(id, { sync: 'skipped' });
          return synced$.pipe(switchMap(() => this.runIndex(id)));
        }),
      )
      .subscribe({
        error: (error: unknown) => {
          const message =
            error instanceof PipelineError
              ? error.message
              : error instanceof EmptyError
                ? 'Still working after 15 minutes - check the status again later.'
                : describeBridgeError(error);
          const p = this.pipelines()[id];
          this.patchPipeline(id, {
            ...(p?.index === 'running' ? { index: 'failed' } : {}),
            ...(p?.sync === 'running' || p?.sync === 'pending' ? { sync: 'failed' } : {}),
            message,
          });
        },
      });
    this.preparations.set(id, sub);
  }

  private runSync(id: string): Observable<DataSourceStatus> {
    this.patchPipeline(id, { sync: 'running', message: 'Syncing tables…' });
    return this.follow(id, this.admin.sync(id), 'metadata').pipe(
      map((status) => {
        if (status.metadata.status !== 'succeeded') {
          throw new PipelineError(
            `Table sync failed (${status.metadata.errorCode ?? 'error'}). Check the connection and try "Sync tables" again.`,
          );
        }
        this.patchPipeline(id, {
          sync: 'done',
          message: `Tables synced: ${status.metadata.objectCount ?? 0} tables/views discovered.`,
        });
        return status;
      }),
    );
  }

  private runIndex(id: string): Observable<DataSourceStatus> {
    this.patchPipeline(id, { index: 'running', message: 'Building index…' });
    return this.follow(id, this.admin.index(id), 'semantic').pipe(
      map((status) => {
        if (status.semantic.status !== 'active') {
          throw new PipelineError(
            status.semantic.errorCode === 'NO_ENTITY_BOUND'
              ? 'Tables synced, but no semantic entities describe this database yet - see the steps below, then click "Build index".'
              : `Index build failed (${status.semantic.errorCode ?? 'error'}). Try "Build index" again.`,
          );
        }
        this.patchPipeline(id, {
          index: 'done',
          message: `Index built successfully - ready for questions (${status.semantic.entitiesBound} entities).`,
        });
        return status;
      }),
    );
  }

  /** Starts a job and polls until its part of the status is no longer running. */
  private follow(
    id: string,
    start: Observable<DataSourceStatus>,
    part: 'metadata' | 'semantic',
  ): Observable<DataSourceStatus> {
    const running = (status: DataSourceStatus) => RUNNING.has(status[part].status ?? '');
    const show = (status: DataSourceStatus) => {
      this.setStatus(id, { kind: 'ready', status });
    };
    return start.pipe(
      tap(show),
      switchMap((initial) =>
        running(initial)
          ? timer(this.pollMs, this.pollMs).pipe(
              take(MAX_POLLS),
              switchMap(() => this.admin.status(id)),
              tap(show),
              first((status) => !running(status)),
            )
          : of(initial),
      ),
    );
  }

  protected refresh(id: string): void {
    this.track(id, this.admin.status(id));
  }

  protected sync(id: string): void {
    this.track(id, this.admin.sync(id));
  }

  protected index(id: string): void {
    this.track(id, this.admin.index(id));
  }

  protected remove(id: string): void {
    this.confirmDelete.set(null);
    this.admin.remove(id).subscribe({
      error: (error: unknown) => {
        this.setActionError(id, describeBridgeError(error));
      },
    });
  }

  /** Runs an action, then polls the status until sync/index jobs finish. */
  private track(id: string, action: Observable<DataSourceStatus>): void {
    this.polls.get(id)?.unsubscribe();
    this.setActionError(id, null);
    this.setStatus(id, { kind: 'loading' });
    const sub = action.subscribe({
      next: (status) => {
        this.setStatus(id, { kind: 'ready', status });
        if (isBusy(status)) this.poll(id);
      },
      error: (error: unknown) => {
        this.setStatus(id, { kind: 'failed', message: describeBridgeError(error) });
      },
    });
    this.polls.set(id, sub);
  }

  private poll(id: string): void {
    const sub = timer(this.pollMs, this.pollMs)
      .pipe(
        take(MAX_POLLS),
        switchMap(() => this.admin.status(id)),
        takeWhile((status) => isBusy(status), true),
      )
      .subscribe({
        next: (status) => {
          this.setStatus(id, { kind: 'ready', status });
        },
        error: (error: unknown) => {
          this.setStatus(id, { kind: 'failed', message: describeBridgeError(error) });
        },
      });
    this.polls.set(id, sub);
  }

  private setTest(id: string, state: TestState): void {
    this.tests.update((all) => ({ ...all, [id]: state }));
  }

  private setStatus(id: string, state: StatusState): void {
    this.statuses.update((all) => ({ ...all, [id]: state }));
  }

  private setPipeline(id: string, state: PipelineState): void {
    this.pipelines.update((all) => ({ ...all, [id]: state }));
  }

  private patchPipeline(id: string, changes: Partial<PipelineState>): void {
    this.pipelines.update((all) => {
      const current = all[id];
      return current === undefined ? all : { ...all, [id]: { ...current, ...changes } };
    });
  }

  private setActionError(id: string, message: string | null): void {
    this.actionErrors.update((all) => {
      const others = Object.fromEntries(Object.entries(all).filter(([key]) => key !== id));
      return message === null ? others : { ...others, [id]: message };
    });
  }
}
