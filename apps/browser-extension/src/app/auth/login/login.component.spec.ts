import { TestBed } from '@angular/core/testing';
import { Router, provideRouter } from '@angular/router';
import { of, throwError, type Observable } from 'rxjs';

import { AuthService } from '../../core/auth/auth.service';
import { BridgeError } from '../../core/messaging/background-bridge.service';
import type { LoginHints, LoginRequest, SessionInfo } from '../../core/messaging/protocol';
import { LoginComponent, describeLoginError } from './login.component';

const SESSION: SessionInfo = {
  user: {
    id: 'u1',
    email: 'ana@acme.test',
    displayName: 'Ana',
    tenant: { id: 't1', slug: 'acme', name: 'Acme' },
    roles: ['analyst'],
    permissions: [],
  },
};

async function render(
  login: (request: LoginRequest) => Observable<SessionInfo>,
  hints: LoginHints | null = null,
) {
  const loginSpy = vi.fn(login);
  TestBed.configureTestingModule({
    imports: [LoginComponent],
    providers: [
      provideRouter([]),
      { provide: AuthService, useValue: { login: loginSpy, loginHints: () => of(hints) } },
    ],
  });
  const router = TestBed.inject(Router);
  const navigate = vi.spyOn(router, 'navigateByUrl').mockResolvedValue(true);
  const fixture = TestBed.createComponent(LoginComponent);
  fixture.detectChanges();
  await fixture.whenStable();
  const element = fixture.nativeElement as HTMLElement;
  const input = (name: string) =>
    element.querySelector<HTMLInputElement>(`input[formcontrolname="${name}"]`)!;
  const type = (name: string, value: string) => {
    input(name).value = value;
    input(name).dispatchEvent(new Event('input'));
  };
  const submit = async () => {
    element.querySelector<HTMLButtonElement>('button[type="submit"]')!.click();
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
  };
  const fill = () => {
    type('tenant', ' ACME ');
    type('email', 'ana@acme.test');
    type('password', 'correct horse battery staple');
  };
  return { element, input, type, submit, fill, loginSpy, navigate };
}

describe('LoginComponent', () => {
  it('uses labelled inputs with correct autocomplete hints', async () => {
    const { input } = await render(() => of(SESSION));
    expect(input('email').getAttribute('autocomplete')).toBe('username');
    expect(input('password').getAttribute('autocomplete')).toBe('current-password');
    expect(input('tenant').getAttribute('autocomplete')).toBe('organization');
  });

  it('prefills tenant and email from remembered hints, never the password', async () => {
    const { input } = await render(() => of(SESSION), { tenant: 'acme', email: 'a@acme.test' });
    expect(input('tenant').value).toBe('acme');
    expect(input('email').value).toBe('a@acme.test');
    expect(input('password').value).toBe('');
  });

  it('does not submit an invalid form', async () => {
    const { element, submit, loginSpy } = await render(() => of(SESSION));
    await submit();
    expect(loginSpy).not.toHaveBeenCalled();
    expect(element.textContent).toContain('Email is required.');
  });

  it('signs in with a normalised tenant, clears the password and navigates', async () => {
    const { input, fill, submit, loginSpy, navigate } = await render(() => of(SESSION));
    fill();
    await submit();
    expect(loginSpy).toHaveBeenCalledWith({
      tenant: 'acme',
      email: 'ana@acme.test',
      password: 'correct horse battery staple',
    });
    expect(input('password').value).toBe('');
    expect(navigate).toHaveBeenCalledWith('/chat');
  });

  it('shows an accessible error for invalid credentials', async () => {
    const { element, fill, submit, input, navigate } = await render(() =>
      throwError(() => new BridgeError('INVALID_CREDENTIALS', 'server text')),
    );
    fill();
    await submit();
    const error = element.querySelector('[data-testid="login-error"]');
    expect(error?.getAttribute('role')).toBe('alert');
    expect(error?.textContent).toContain('Invalid tenant, email or password.');
    expect(error?.textContent).not.toContain('server text');
    expect(input('password').value).toBe('');
    expect(navigate).not.toHaveBeenCalled();
  });
});

describe('describeLoginError', () => {
  it.each([
    [new BridgeError('INVALID_CREDENTIALS', 'x'), 'Invalid tenant, email or password.'],
    [
      new BridgeError('RATE_LIMITED', 'x', 30),
      'Too many sign-in attempts. Try again in 30 seconds.',
    ],
    [new BridgeError('RATE_LIMITED', 'x'), 'Too many sign-in attempts. Try again in a moment.'],
    [new BridgeError('NETWORK_ERROR', 'x'), 'Cannot reach the ERP Insight AI server.'],
    [new BridgeError('TIMEOUT', 'x'), 'Cannot reach the ERP Insight AI server.'],
    [new BridgeError('NOT_IN_EXTENSION', 'x'), 'Cannot reach the ERP Insight AI server.'],
    [new BridgeError('API_ERROR', 'internal detail'), 'Sign-in failed. Please try again.'],
    [new Error('boom'), 'Sign-in failed. Please try again.'],
  ])('maps %o', (error, expected) => {
    expect(describeLoginError(error)).toContain(expected);
  });
});
