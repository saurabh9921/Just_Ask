import { ChangeDetectionStrategy, Component, inject, signal, type OnInit } from '@angular/core';
import { NonNullableFormBuilder, ReactiveFormsModule, Validators } from '@angular/forms';
import { MatButtonModule } from '@angular/material/button';
import { MatFormFieldModule } from '@angular/material/form-field';
import { MatInputModule } from '@angular/material/input';
import { MatProgressSpinnerModule } from '@angular/material/progress-spinner';
import { ActivatedRoute, Router } from '@angular/router';

import { safeReturnUrl } from '../../core/auth/auth.guards';
import { AuthService } from '../../core/auth/auth.service';
import { BridgeError } from '../../core/messaging/background-bridge.service';

const CONNECTIVITY_CODES = new Set(['NETWORK_ERROR', 'TIMEOUT', 'NOT_IN_EXTENSION']);

/** Maps a sign-in failure to a user-facing message (never echoes server internals). */
export function describeLoginError(error: unknown): string {
  if (!(error instanceof BridgeError)) {
    return 'Sign-in failed. Please try again.';
  }
  if (error.code === 'INVALID_CREDENTIALS' || error.code === 'UNAUTHENTICATED') {
    return 'Invalid tenant, email or password.';
  }
  if (error.code === 'RATE_LIMITED') {
    const seconds = error.retryAfterSeconds;
    return seconds !== null && seconds > 0
      ? `Too many sign-in attempts. Try again in ${seconds} seconds.`
      : 'Too many sign-in attempts. Try again in a moment.';
  }
  if (CONNECTIVITY_CODES.has(error.code)) {
    return 'Cannot reach the ERP Insight AI server. Check the API URL in Settings and your network connection.';
  }
  if (error.code === 'VALIDATION_ERROR') {
    return 'Enter a valid tenant, email and password.';
  }
  if (error.code === 'PERMISSION_DENIED') {
    return 'Your account is not allowed to sign in. Contact your administrator.';
  }
  return 'Sign-in failed. Please try again.';
}

/**
 * Sign-in form. Credentials go to the background worker once (`auth.login`), which
 * calls the API and keeps tokens in session storage. The password is cleared from
 * the form after every attempt and is never persisted.
 */
@Component({
  selector: 'eia-login',
  imports: [
    ReactiveFormsModule,
    MatButtonModule,
    MatFormFieldModule,
    MatInputModule,
    MatProgressSpinnerModule,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <section class="page login" aria-labelledby="login-title">
      <h1 id="login-title" class="page__title">Sign in</h1>
      <p class="page__subtitle">Use your ERP Insight AI account to query your ERP data.</p>

      <form [formGroup]="form" (ngSubmit)="submit()" novalidate>
        <mat-form-field appearance="outline">
          <mat-label>Organisation (tenant)</mat-label>
          <input
            matInput
            type="text"
            formControlName="tenant"
            autocomplete="organization"
            autocapitalize="none"
            required
            spellcheck="false"
          />
          @if (form.controls.tenant.hasError('required')) {
            <mat-error>Tenant is required.</mat-error>
          } @else if (form.controls.tenant.hasError('pattern')) {
            <mat-error>Use lowercase letters, numbers and hyphens.</mat-error>
          }
        </mat-form-field>

        <mat-form-field appearance="outline">
          <mat-label>Work email</mat-label>
          <input
            matInput
            type="email"
            formControlName="email"
            autocomplete="username"
            required
            spellcheck="false"
          />
          @if (form.controls.email.hasError('required')) {
            <mat-error>Email is required.</mat-error>
          } @else if (form.controls.email.hasError('email')) {
            <mat-error>Enter a valid email address.</mat-error>
          }
        </mat-form-field>

        <mat-form-field appearance="outline">
          <mat-label>Password</mat-label>
          <input
            matInput
            type="password"
            formControlName="password"
            autocomplete="current-password"
            required
          />
          @if (form.controls.password.hasError('required')) {
            <mat-error>Password is required.</mat-error>
          }
        </mat-form-field>

        <button mat-flat-button type="submit" [disabled]="submitting()">
          @if (submitting()) {
            <mat-progress-spinner mode="indeterminate" diameter="18" aria-hidden="true" />
            Signing in…
          } @else {
            Sign in
          }
        </button>
      </form>

      <div aria-live="assertive">
        @if (error(); as message) {
          <p class="error" role="alert" data-testid="login-error">{{ message }}</p>
        }
      </div>
    </section>
  `,
  styles: `
    form {
      display: flex;
      flex-direction: column;
      gap: 4px;
      max-width: 420px;
    }
    button[type='submit'] {
      align-self: flex-start;
    }
    mat-progress-spinner {
      display: inline-block;
      margin-right: 8px;
      vertical-align: middle;
    }
    .error {
      margin-top: 16px;
      padding: 12px;
      border-radius: 8px;
      background: var(--mat-sys-error-container);
      color: var(--mat-sys-on-error-container);
      font: var(--mat-sys-body-medium);
    }
  `,
})
export class LoginComponent implements OnInit {
  private readonly fb = inject(NonNullableFormBuilder);
  private readonly auth = inject(AuthService);
  private readonly router = inject(Router);
  private readonly route = inject(ActivatedRoute);

  protected readonly form = this.fb.group({
    tenant: [
      '',
      [Validators.required, Validators.pattern(/^\s*[A-Za-z0-9][A-Za-z0-9-]{0,62}\s*$/)],
    ],
    email: ['', [Validators.required, Validators.email, Validators.maxLength(320)]],
    password: ['', [Validators.required, Validators.maxLength(1024)]],
  });

  protected readonly submitting = signal(false);
  protected readonly error = signal<string | null>(null);

  ngOnInit(): void {
    this.auth.loginHints().subscribe((hints) => {
      if (hints !== null && this.form.pristine) {
        this.form.patchValue({ tenant: hints.tenant, email: hints.email });
      }
    });
  }

  protected submit(): void {
    this.form.markAllAsTouched();
    if (this.form.invalid || this.submitting()) {
      return;
    }
    const { tenant, email, password } = this.form.getRawValue();
    // Do not keep the password in the form longer than necessary.
    this.form.controls.password.reset();
    this.error.set(null);
    this.submitting.set(true);

    this.auth
      .login({ tenant: tenant.trim().toLowerCase(), email: email.trim(), password })
      .subscribe({
        next: () => {
          this.submitting.set(false);
          const returnUrl = safeReturnUrl(this.route.snapshot.queryParamMap.get('returnUrl'));
          void this.router.navigateByUrl(returnUrl);
        },
        error: (error: unknown) => {
          this.submitting.set(false);
          this.error.set(describeLoginError(error));
        },
      });
  }
}
