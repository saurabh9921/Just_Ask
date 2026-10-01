import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  output,
  signal,
  type OnInit,
} from '@angular/core';
import { NonNullableFormBuilder, ReactiveFormsModule, Validators } from '@angular/forms';
import { MatButtonModule } from '@angular/material/button';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';

import { describeBridgeError } from '../core/messaging/background-bridge.service';
import {
  CONNECTOR_KINDS,
  TLS_MODES,
  type ConnectionChanges,
  type ConnectionInput,
  type ConnectionSummary,
  type ConnectorKind,
  type TlsModeName,
} from '../core/messaging/protocol';
import { DataSourceAdminService } from './data-source-admin.service';

const DEFAULT_PORTS: Readonly<Record<ConnectorKind, number>> = {
  postgresql: 5432,
  mysql: 3306,
  sqlserver: 1433,
};
const LABELS: Readonly<Record<ConnectorKind, string>> = {
  postgresql: 'PostgreSQL',
  mysql: 'MySQL / MariaDB',
  sqlserver: 'SQL Server / Azure SQL',
};
const TLS_LABELS: Readonly<Record<TlsModeName, string>> = {
  'verify-full': 'Verify certificate and host (recommended)',
  'verify-ca': 'Verify certificate',
  require: 'Encrypt, no verification',
  prefer: 'Encrypt if available (not allowed in production)',
  disable: 'No encryption (not allowed in production)',
};

/**
 * Add or edit a data source. The password field is write-only: it is sent once,
 * stored encrypted by the server, and never shown again (leave it blank to keep it).
 */
@Component({
  selector: 'eia-data-source-form',
  imports: [ReactiveFormsModule, MatButtonModule, MatFormFieldModule, MatInputModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <form
      class="form"
      [formGroup]="form"
      (ngSubmit)="submit()"
      novalidate
      [attr.aria-label]="editing() ? 'Edit data source' : 'Add data source'"
    >
      <h2 class="form__title">{{ editing() ? 'Edit data source' : 'Add data source' }}</h2>
      <p class="hint">
        Use a <strong>read-only</strong> database account (SELECT on reporting views). Accounts that
        can write are refused.
      </p>

      <mat-form-field appearance="outline">
        <mat-label>Name</mat-label>
        <input matInput formControlName="name" maxlength="120" required />
      </mat-form-field>

      <mat-form-field appearance="outline">
        <mat-label>Database type</mat-label>
        <select matNativeControl formControlName="connectorType" (change)="typeChanged()">
          @for (kind of kinds; track kind) {
            <option [value]="kind">{{ labels[kind] }}</option>
          }
        </select>
      </mat-form-field>

      <div class="row">
        <mat-form-field appearance="outline" class="grow">
          <mat-label>Host</mat-label>
          <input matInput formControlName="host" spellcheck="false" required />
          <mat-hint>DNS name or IP; this PC from Docker: host.docker.internal</mat-hint>
        </mat-form-field>
        <mat-form-field appearance="outline" class="port">
          <mat-label>Port</mat-label>
          <input matInput type="number" formControlName="port" min="1" max="65535" />
        </mat-form-field>
      </div>

      <div class="row">
        <mat-form-field appearance="outline" class="grow">
          <mat-label>Database</mat-label>
          <input matInput formControlName="databaseName" spellcheck="false" required />
        </mat-form-field>
        <mat-form-field appearance="outline" class="grow">
          <mat-label>Schema (optional)</mat-label>
          <input matInput formControlName="schemaName" spellcheck="false" />
        </mat-form-field>
      </div>

      <div class="row">
        <mat-form-field appearance="outline" class="grow">
          <mat-label>Database user</mat-label>
          <input
            matInput
            formControlName="username"
            autocomplete="off"
            spellcheck="false"
            required
          />
        </mat-form-field>
        <mat-form-field appearance="outline" class="grow">
          <mat-label>Password</mat-label>
          <input matInput type="password" formControlName="password" autocomplete="new-password" />
          @if (editing()) {
            <mat-hint>Leave blank to keep the stored password</mat-hint>
          }
        </mat-form-field>
      </div>

      <mat-form-field appearance="outline">
        <mat-label>Encryption (TLS)</mat-label>
        <select matNativeControl formControlName="tlsMode">
          @for (mode of tlsModes; track mode) {
            <option [value]="mode">{{ tlsLabels[mode] }}</option>
          }
        </select>
      </mat-form-field>

      <details class="advanced">
        <summary>Advanced</summary>
        <div class="row">
          <mat-form-field appearance="outline" class="grow">
            <mat-label>Read replica host (optional)</mat-label>
            <input matInput formControlName="readReplicaHost" spellcheck="false" />
          </mat-form-field>
          <mat-form-field appearance="outline" class="port">
            <mat-label>Replica port</mat-label>
            <input matInput type="number" formControlName="readReplicaPort" />
          </mat-form-field>
        </div>
        <div class="row">
          <mat-form-field appearance="outline" class="grow">
            <mat-label>Max rows per answer</mat-label>
            <input matInput type="number" formControlName="maxRows" min="1" max="100000" />
          </mat-form-field>
          <mat-form-field appearance="outline" class="grow">
            <mat-label>Query timeout (seconds)</mat-label>
            <input matInput type="number" formControlName="queryTimeoutSeconds" min="1" max="600" />
          </mat-form-field>
        </div>
      </details>

      @if (error()) {
        <p class="error" role="alert" data-testid="form-error">{{ error() }}</p>
      }
      <div class="actions">
        <button mat-flat-button type="submit" [disabled]="saving()">
          {{ saving() ? 'Saving…' : editing() ? 'Save changes' : 'Add data source' }}
        </button>
        <button mat-button type="button" [disabled]="saving()" (click)="cancelled.emit()">
          Cancel
        </button>
      </div>
    </form>
  `,
  styles: `
    .form {
      display: grid;
      gap: 4px;
      padding: 12px;
      border-radius: 12px;
      border: 1px solid var(--mat-sys-outline-variant);
      background: var(--mat-sys-surface-container-low);
    }
    .form__title {
      margin: 0 0 4px;
      font: var(--mat-sys-title-small);
    }
    .hint {
      margin: 0 0 8px;
      font: var(--mat-sys-body-small);
      color: var(--mat-sys-on-surface-variant);
    }
    .row {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }
    .grow {
      flex: 1 1 160px;
    }
    .port {
      flex: 0 1 110px;
    }
    .advanced {
      margin: 4px 0 8px;
      font: var(--mat-sys-body-small);
    }
    .advanced summary {
      cursor: pointer;
      margin-bottom: 8px;
    }
    .error {
      color: var(--mat-sys-error);
      font: var(--mat-sys-body-small);
      margin: 0;
    }
    .actions {
      display: flex;
      gap: 8px;
    }
  `,
})
export class DataSourceFormComponent implements OnInit {
  /** The data source to edit; omit to add a new one. */
  readonly connection = input<ConnectionSummary | null>(null);
  readonly saved = output<ConnectionSummary>();
  readonly cancelled = output();

  private readonly admin = inject(DataSourceAdminService);
  private readonly fb = inject(NonNullableFormBuilder);

  protected readonly kinds = CONNECTOR_KINDS;
  protected readonly labels = LABELS;
  protected readonly tlsModes = TLS_MODES;
  protected readonly tlsLabels = TLS_LABELS;
  protected readonly editing = computed(() => this.connection() !== null);
  protected readonly saving = signal(false);
  protected readonly error = signal<string | null>(null);

  protected readonly form = this.fb.group({
    name: ['', [Validators.required, Validators.maxLength(120)]],
    connectorType: this.fb.control<ConnectorKind>('postgresql'),
    host: ['', [Validators.required, Validators.maxLength(255)]],
    port: this.fb.control<number | null>(5432),
    databaseName: ['', [Validators.required, Validators.maxLength(255)]],
    schemaName: [''],
    username: ['', [Validators.required, Validators.maxLength(255)]],
    password: ['', [Validators.maxLength(1024)]],
    tlsMode: this.fb.control<TlsModeName>('verify-full'),
    readReplicaHost: [''],
    readReplicaPort: this.fb.control<number | null>(null),
    maxRows: [1000, [Validators.required, Validators.min(1), Validators.max(100_000)]],
    queryTimeoutSeconds: [30, [Validators.required, Validators.min(1), Validators.max(600)]],
  });

  ngOnInit(): void {
    const c = this.connection();
    if (c === null) {
      this.form.controls.password.addValidators(Validators.required);
      return;
    }
    const kind = (CONNECTOR_KINDS as readonly string[]).includes(c.connectorType)
      ? (c.connectorType as ConnectorKind)
      : 'postgresql';
    this.form.patchValue({
      name: c.name,
      connectorType: kind,
      host: c.host ?? '',
      port: c.port,
      databaseName: c.databaseName ?? '',
      schemaName: c.schemaName ?? '',
      username: c.username ?? '',
      tlsMode: c.tlsMode ?? 'verify-full',
      readReplicaHost: c.readReplicaHost ?? '',
      readReplicaPort: c.readReplicaPort ?? null,
      maxRows: c.maxRows ?? 1000,
      queryTimeoutSeconds: c.queryTimeoutSeconds ?? 30,
    });
    this.form.controls.connectorType.disable(); // the type of a data source cannot change
  }

  protected typeChanged(): void {
    this.form.controls.port.setValue(DEFAULT_PORTS[this.form.controls.connectorType.value]);
  }

  protected submit(): void {
    if (this.form.invalid) {
      this.form.markAllAsTouched();
      this.error.set('Please fill in the required fields.');
      return;
    }
    const v = this.form.getRawValue();
    const values: ConnectionInput = {
      name: v.name.trim(),
      connectorType: v.connectorType,
      host: v.host.trim(),
      port: v.port,
      databaseName: v.databaseName.trim(),
      schemaName: v.schemaName.trim() || null,
      username: v.username.trim(),
      tlsMode: v.tlsMode,
      readReplicaHost: v.readReplicaHost.trim() || null,
      readReplicaPort: v.readReplicaPort,
      maxRows: v.maxRows,
      queryTimeoutSeconds: v.queryTimeoutSeconds,
      ...(v.password ? { password: v.password } : {}),
    };
    this.saving.set(true);
    this.error.set(null);
    const existing = this.connection();
    const request =
      existing === null
        ? this.admin.create(values)
        : this.admin.update(existing.id, this.changes(values));
    request.subscribe({
      next: (connection) => {
        this.saving.set(false);
        this.form.controls.password.reset('');
        this.saved.emit(connection);
      },
      error: (err: unknown) => {
        this.saving.set(false);
        this.error.set(describeBridgeError(err));
      },
    });
  }

  private changes(values: ConnectionInput): ConnectionChanges {
    // The type of an existing data source cannot change, so it is not sent.
    return {
      name: values.name,
      host: values.host,
      port: values.port,
      databaseName: values.databaseName,
      schemaName: values.schemaName,
      username: values.username,
      tlsMode: values.tlsMode,
      readReplicaHost: values.readReplicaHost,
      readReplicaPort: values.readReplicaPort,
      maxRows: values.maxRows,
      queryTimeoutSeconds: values.queryTimeoutSeconds,
      ...(values.password === undefined ? {} : { password: values.password }),
    };
  }
}
