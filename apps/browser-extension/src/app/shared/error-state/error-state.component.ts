import { ChangeDetectionStrategy, Component, input, output } from '@angular/core';
import { MatButtonModule } from '@angular/material/button';

/** Consistent, accessible error presentation with optional retry. */
@Component({
  selector: 'eia-error-state',
  imports: [MatButtonModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <section class="error" role="alert" [attr.aria-labelledby]="headingId">
      <h2 class="error__title" [id]="headingId">{{ title() }}</h2>
      <p class="error__message">{{ message() }}</p>
      @if (retryable()) {
        <button mat-stroked-button type="button" (click)="retry.emit()">{{ retryLabel() }}</button>
      }
    </section>
  `,
  styles: `
    .error {
      padding: 16px;
      border-radius: 12px;
      border: 1px solid var(--mat-sys-error);
      background: var(--mat-sys-error-container);
      color: var(--mat-sys-on-error-container);
    }
    .error__title {
      margin: 0 0 4px;
      font: var(--mat-sys-title-small);
    }
    .error__message {
      margin: 0 0 12px;
      font: var(--mat-sys-body-medium);
    }
    .error__message:last-child {
      margin-bottom: 0;
    }
  `,
})
export class ErrorStateComponent {
  private static nextId = 0;

  readonly title = input('Something went wrong');
  readonly message = input.required<string>();
  readonly retryable = input(false);
  readonly retryLabel = input('Try again');
  readonly retry = output();

  protected readonly headingId = `eia-error-${ErrorStateComponent.nextId++}`;
}
