import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  OnInit,
  inject,
  signal,
} from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import {
  NonNullableFormBuilder,
  ReactiveFormsModule,
  type AbstractControl,
  type ValidationErrors,
} from '@angular/forms';
import { MatButtonModule } from '@angular/material/button';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatProgressBarModule } from '@angular/material/progress-bar';
import { finalize } from 'rxjs';

import {
  BackgroundBridgeService,
  describeBridgeError,
} from '../../core/messaging/background-bridge.service';
import type { HealthCheckResult } from '../../core/messaging/protocol';
import { validateApiBaseUrl } from '../../core/settings/api-url';
import { ConnectionStatusService } from '../../core/status/connection-status.service';
import { ErrorStateComponent } from '../../shared/error-state/error-state.component';

function apiUrlValidator(control: AbstractControl<string>): ValidationErrors | null {
  const result = validateApiBaseUrl(control.value);
  return result.valid ? null : { apiUrl: result.reason };
}

/**
 * API endpoint settings. Only the (non-secret) API base URL is configurable here;
 * ERP database credentials are managed server-side and never reach the extension.
 */
@Component({
  selector: 'eia-connection-settings',
  imports: [
    ReactiveFormsModule,
    MatButtonModule,
    MatFormFieldModule,
    MatInputModule,
    MatProgressBarModule,
    ErrorStateComponent,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <section class="page" aria-labelledby="settings-title">
      <h1 id="settings-title" class="page__title">Settings</h1>
      <p class="page__subtitle">
        Configure the ERP Insight AI server this extension connects to. Database credentials are
        managed by your administrator on the server and are never stored in the browser.
      </p>

      @if (loadError()) {
        <eia-error-state
          title="Settings unavailable"
          [message]="loadError()!"
          [retryable]="true"
          (retry)="load()"
        />
      }

      <form [formGroup]="form" (ngSubmit)="save()" novalidate>
        <mat-form-field appearance="outline">
          <mat-label>API base URL</mat-label>
          <input
            matInput
            type="url"
            formControlName="apiBaseUrl"
            autocomplete="url"
            spellcheck="false"
            required
          />
          <mat-hint>HTTPS is required except for localhost.</mat-hint>
          @if (form.controls.apiBaseUrl.errors?.['apiUrl']; as reason) {
            <mat-error>{{ reason }}</mat-error>
          }
        </mat-form-field>

        <div class="actions">
          <button mat-flat-button type="submit" [disabled]="busy()">Save</button>
          <button mat-stroked-button type="button" [disabled]="busy()" (click)="testConnection()">
            Test connection
          </button>
        </div>
        @if (busy()) {
          <mat-progress-bar mode="indeterminate" aria-label="Working" />
        }
      </form>

      <div aria-live="polite">
        @if (saveMessage()) {
          <p class="message" role="status" data-testid="save-message">{{ saveMessage() }}</p>
        }
        @if (actionError()) {
          <eia-error-state title="Request failed" [message]="actionError()!" />
        }
        @if (health(); as h) {
          <section class="health" aria-label="Connection test result" data-testid="health-result">
            <p class="health__summary">
              <span class="badge" [attr.data-state]="h.status">{{ h.status }}</span>
              @if (h.latencyMs !== null) {
                <span>{{ h.latencyMs }} ms</span>
              }
            </p>
            @if (h.detail) {
              <p class="health__detail">{{ h.detail }}</p>
            }
            @if (checkEntries(h).length > 0) {
              <table class="health__checks">
                <caption class="visually-hidden">
                  Dependency checks
                </caption>
                <thead>
                  <tr>
                    <th scope="col">Dependency</th>
                    <th scope="col">Status</th>
                  </tr>
                </thead>
                <tbody>
                  @for (entry of checkEntries(h); track entry[0]) {
                    <tr>
                      <td>{{ entry[0] }}</td>
                      <td>{{ entry[1] }}</td>
                    </tr>
                  }
                </tbody>
              </table>
            }
          </section>
        }
      </div>
    </section>
  `,
  styles: `
    form {
      display: flex;
      flex-direction: column;
      gap: 8px;
      max-width: 520px;
    }
    .actions {
      display: flex;
      gap: 8px;
    }
    .message {
      font: var(--mat-sys-body-medium);
      color: var(--mat-sys-primary);
    }
    .health {
      margin-top: 16px;
      padding: 12px 16px;
      border-radius: 12px;
      background: var(--mat-sys-surface-container);
    }
    .health__summary {
      display: flex;
      gap: 12px;
      align-items: center;
      margin: 0;
      font: var(--mat-sys-body-medium);
    }
    .health__detail {
      font: var(--mat-sys-body-small);
      color: var(--mat-sys-on-surface-variant);
    }
    .badge {
      padding: 2px 10px;
      border-radius: 999px;
      text-transform: capitalize;
      font: var(--mat-sys-label-medium);
      background: var(--eia-status-unknown);
      color: #fff;
    }
    .badge[data-state='ok'] {
      background: var(--eia-status-ok);
    }
    .badge[data-state='degraded'] {
      background: var(--eia-status-degraded);
    }
    .badge[data-state='offline'] {
      background: var(--eia-status-offline);
    }
    .health__checks {
      width: 100%;
      margin-top: 8px;
      border-collapse: collapse;
      font: var(--mat-sys-body-small);
    }
    .health__checks th,
    .health__checks td {
      text-align: left;
      padding: 4px 0;
      border-bottom: 1px solid var(--mat-sys-outline-variant);
    }
  `,
})
export class ConnectionSettingsComponent implements OnInit {
  private readonly bridge = inject(BackgroundBridgeService);
  private readonly connectionStatus = inject(ConnectionStatusService);
  private readonly destroyRef = inject(DestroyRef);
  private readonly fb = inject(NonNullableFormBuilder);

  protected readonly form = this.fb.group({
    apiBaseUrl: ['', [apiUrlValidator]],
  });

  protected readonly busy = signal(false);
  protected readonly loadError = signal<string | null>(null);
  protected readonly actionError = signal<string | null>(null);
  protected readonly saveMessage = signal<string | null>(null);
  protected readonly health = signal<HealthCheckResult | null>(null);

  ngOnInit(): void {
    this.load();
  }

  protected load(): void {
    this.loadError.set(null);
    this.bridge
      .request<'settings.get'>({ type: 'settings.get' })
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: (settings) => this.form.setValue({ apiBaseUrl: settings.apiBaseUrl }),
        error: (error: unknown) => this.loadError.set(describeBridgeError(error)),
      });
  }

  protected save(): void {
    this.form.markAllAsTouched();
    if (this.form.invalid) {
      return;
    }
    this.startAction();
    this.bridge
      .request<'settings.set'>({
        type: 'settings.set',
        payload: { apiBaseUrl: this.form.getRawValue().apiBaseUrl },
      })
      .pipe(
        finalize(() => this.busy.set(false)),
        takeUntilDestroyed(this.destroyRef),
      )
      .subscribe({
        next: (settings) => {
          this.form.setValue({ apiBaseUrl: settings.apiBaseUrl });
          this.saveMessage.set('Settings saved.');
          this.connectionStatus.refresh();
        },
        error: (error: unknown) => this.actionError.set(describeBridgeError(error)),
      });
  }

  protected testConnection(): void {
    this.startAction();
    this.bridge
      .request<'health.check'>({ type: 'health.check' })
      .pipe(
        finalize(() => this.busy.set(false)),
        takeUntilDestroyed(this.destroyRef),
      )
      .subscribe({
        next: (result) => this.health.set(result),
        error: (error: unknown) => this.actionError.set(describeBridgeError(error)),
      });
  }

  protected checkEntries(result: HealthCheckResult): [string, string][] {
    return Object.entries(result.checks);
  }

  private startAction(): void {
    this.busy.set(true);
    this.actionError.set(null);
    this.saveMessage.set(null);
    this.health.set(null);
  }
}
