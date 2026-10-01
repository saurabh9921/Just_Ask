import { ChangeDetectionStrategy, Component, inject, type OnInit } from '@angular/core';
import { MatButtonModule } from '@angular/material/button';

import { AuthService } from '../core/auth/auth.service';
import { ConnectionHealthBadgeComponent } from './connection-health-badge.component';
import { WorkspaceService } from './workspace.service';

/** Tenant + data source picker shown above the chat. */
@Component({
  selector: 'eia-workspace-selector',
  imports: [MatButtonModule, ConnectionHealthBadgeComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <section class="workspace" aria-label="Workspace">
      <div>
        <span class="label">Tenant</span>
        <span class="value" data-testid="tenant-name">{{ tenantName() }}</span>
      </div>

      @if (workspace.error(); as error) {
        <p class="workspace__error" role="alert">
          {{ error }}
          <button mat-button type="button" (click)="workspace.load()">Retry</button>
        </p>
      } @else if (workspace.loaded() && workspace.connections().length === 0) {
        <p class="workspace__empty" data-testid="no-connections">
          No data sources are available to you yet. Ask your data admin to add a connection.
        </p>
      } @else {
        <div>
          <label for="eia-connection-select" class="label">Data source</label>
          <div class="workspace__row">
            <select
              id="eia-connection-select"
              class="select"
              [disabled]="workspace.loading()"
              (change)="onSelect($event)"
            >
              @if (!workspace.loaded()) {
                <option value="">Loading…</option>
              }
              @for (connection of workspace.connections(); track connection.id) {
                <option
                  [value]="connection.id"
                  [selected]="connection.id === workspace.selectedId()"
                >
                  {{ connection.name }} ({{ connection.connectorType }})
                </option>
              }
            </select>
            @if (workspace.selected(); as selected) {
              <eia-connection-health-badge [status]="selected.status" />
              <span class="readonly" title="Queries run with a read-only database role"
                >Read-only</span
              >
            }
          </div>
        </div>
      }
    </section>
  `,
  styles: `
    .workspace {
      display: grid;
      gap: 8px;
      padding: 12px;
      margin-bottom: 16px;
      border-radius: 12px;
      background: var(--mat-sys-surface-container);
    }
    .label {
      display: block;
      font: var(--mat-sys-label-small);
      color: var(--mat-sys-on-surface-variant);
    }
    .value {
      font: var(--mat-sys-title-small);
    }
    .workspace__row {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 8px;
    }
    .select {
      min-width: 0;
      flex: 1 1 180px;
      padding: 6px 8px;
      border-radius: 8px;
      border: 1px solid var(--mat-sys-outline);
      background: var(--mat-sys-surface);
      color: var(--mat-sys-on-surface);
      font: var(--mat-sys-body-medium);
    }
    .readonly {
      padding: 2px 8px;
      border-radius: 999px;
      font: var(--mat-sys-label-small);
      background: var(--mat-sys-tertiary-container);
      color: var(--mat-sys-on-tertiary-container);
    }
    .workspace__empty,
    .workspace__error {
      margin: 0;
      font: var(--mat-sys-body-medium);
    }
    .workspace__error {
      color: var(--mat-sys-error);
    }
  `,
})
export class WorkspaceSelectorComponent implements OnInit {
  private readonly auth = inject(AuthService);
  protected readonly workspace = inject(WorkspaceService);

  protected tenantName(): string {
    return this.auth.user()?.tenant.name ?? '—';
  }

  ngOnInit(): void {
    if (!this.workspace.loaded() && !this.workspace.loading()) {
      this.workspace.load();
    }
  }

  protected onSelect(event: Event): void {
    const value = (event.target as HTMLSelectElement).value;
    this.workspace.select(value === '' ? null : value);
  }
}
