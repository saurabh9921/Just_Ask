import { inject } from '@angular/core';
import { Router, type CanActivateFn } from '@angular/router';

import { AuthService } from './auth.service';

/** Only internal, hash-routed paths are accepted as a post-login destination. */
export function safeReturnUrl(value: unknown): string {
  return typeof value === 'string' && /^\/(chat|history|connections)(\/[\w-]*)*$/.test(value)
    ? value
    : '/chat';
}

/** Requires a signed-in user; otherwise redirects to /login?returnUrl=... */
export const authGuard: CanActivateFn = async (_route, state) => {
  const auth = inject(AuthService);
  const router = inject(Router);
  const session = await auth.ensureLoaded();
  if (session !== null) {
    return true;
  }
  return router.createUrlTree(['/login'], { queryParams: { returnUrl: state.url } });
};

/** Keeps signed-in users away from the sign-in page. */
export const guestGuard: CanActivateFn = async () => {
  const auth = inject(AuthService);
  const router = inject(Router);
  const session = await auth.ensureLoaded();
  return session === null ? true : router.createUrlTree(['/chat']);
};
