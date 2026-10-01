/**
 * API base URL validation, shared by the settings UI and the background worker.
 * Must stay free of DOM- and Angular-specific dependencies.
 */

const LOCAL_HOSTNAMES = new Set(['localhost', '127.0.0.1', '[::1]']);
const MAX_URL_LENGTH = 2048;

export type UrlValidationResult =
  | { readonly valid: true; readonly normalized: string }
  | { readonly valid: false; readonly reason: string };

/**
 * Validates an API base URL.
 *
 * Rules: absolute http(s) URL, no credentials, no query or fragment, and HTTPS is
 * mandatory for every host except loopback development hosts. Returns the URL
 * normalised without a trailing slash.
 */
export function validateApiBaseUrl(input: unknown): UrlValidationResult {
  if (typeof input !== 'string' || input.trim() === '') {
    return { valid: false, reason: 'API URL is required.' };
  }
  const raw = input.trim();
  if (raw.length > MAX_URL_LENGTH) {
    return { valid: false, reason: 'API URL is too long.' };
  }
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    return { valid: false, reason: 'API URL is not a valid absolute URL.' };
  }
  if (url.protocol !== 'https:' && url.protocol !== 'http:') {
    return { valid: false, reason: 'API URL must use HTTPS.' };
  }
  if (url.username !== '' || url.password !== '') {
    return { valid: false, reason: 'API URL must not contain credentials.' };
  }
  if (url.search !== '' || url.hash !== '') {
    return { valid: false, reason: 'API URL must not contain a query string or fragment.' };
  }
  if (url.protocol === 'http:' && !LOCAL_HOSTNAMES.has(url.hostname)) {
    return { valid: false, reason: 'API URL must use HTTPS (HTTP is allowed only for localhost).' };
  }
  const normalized = `${url.origin}${url.pathname}`.replace(/\/+$/, '');
  return { valid: true, normalized };
}
