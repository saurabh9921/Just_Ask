# ERP Insight AI — Browser Extension

Chrome Manifest V3 side-panel extension built with Angular 22 (standalone, zoneless,
signals, strict TypeScript) and Angular Material (M3).

**Phase 2 status:** sign-in and sign-out, session handling with refresh-token
rotation, route guards, the signed-in user and tenant in the top bar, a workspace
selector (tenant plus data source) above the chat, and a **Data sources** page with
per-connection tests all work. Chat querying and history are still labelled shells;
they are wired up in Phases 4–5.

## Requirements

- **Node.js ≥ 22.22.3 or ≥ 24.15** (Angular 22 requirement; `.nvmrc` pins 24).
  `.npmrc` sets `engine-strict=true`, so installs fail fast on older Node.
- Chrome ≥ 116 (side panel API).

## Commands

```bash
npm install
npm run lint           # ESLint (typescript-eslint strict + angular-eslint + template a11y)
npm run typecheck      # tsc for app, background worker and specs
npm test               # Vitest via @angular/build:unit-test (jsdom)
npm run build          # production build -> dist/ (unpacked extension)
npm run build:dev      # development build (dev environment, source maps)
npm run format         # Prettier
npm run icons          # regenerate public/icons/*.png
```

`npm run build` runs `ng build`, then `scripts/build-extension.mjs`. The script bundles
`src/background/main.ts` to `dist/background.js` with esbuild, copies `manifest.json`,
and fails the build if `dist/index.html` has an inline script or an inline event
handler (the MV3 CSP blocks both). Critical-CSS inlining is turned off for that reason.

## Load unpacked

1. `npm run build`
2. Open `chrome://extensions`, then turn on **Developer mode**.
3. Click **Load unpacked** and select `apps/browser-extension/dist`.
4. Click the toolbar icon to open the side panel.

Start the API stack (`make up` from the repo root) so the status indicator shows
**Online**. The default API URL is `http://localhost:8080` (nginx).

## Architecture

```
side panel (Angular)  --chrome.runtime.sendMessage-->  background service worker  --fetch-->  API
   no tokens, no network                               owns settings, tokens, refresh,
   (user + tenant only)  <--auth.changed event------   health checks
        |                                                     |
        +--port "eia.analytics" (ask/cancel)-->  AnalyticsPortHandler --WebSocket--> /api/v1/analytics/ws
          <--accepted/progress/result/error--     (HTTP fallback: /analytics/query|follow-up|explain)
```

- `src/app/chat/`: `AnalyticsChatComponent` (composer, follow-ups, plan-only), `ChatStore`
  (conversation state, in memory only), `ChatMessageComponent`, `ResultViewComponent`,
  `QueryProgressComponent`, `QueryPlanViewerComponent`, `SourceMetadataComponent`,
  `FeedbackComponent`.
- `src/app/dashboards/`: KPI cards, result table, ECharts chart (`chart-options.ts`).
- `src/app/query-history/`: history list, detail, save, delete, run again.
- `src/app/shared/`: formatting, CSV/JSON export (formula-injection safe), inline SVG icons.
- `src/background/analytics-socket.ts` / `analytics-port.ts` / `analytics-parsers.ts` /
  `history.ts`: WebSocket coordination, HTTP fallback, response validation, history and
  feedback calls.

- `src/app/core/messaging/protocol.ts`: the typed, discriminated-union message protocol
  that both bundles share. It must stay DOM- and Angular-free.
- `src/background/`: the service worker. `router.ts` rejects messages from any sender
  other than this extension and rejects unknown message types. `settings.ts` persists the
  API URL in `chrome.storage.local` (non-secret only). `health.ts` handles readiness
  checks with timeout. `token-store.ts` keeps tokens in `chrome.storage.session`, locked
  to `TRUSTED_CONTEXTS` (memory-only, unreadable by content scripts).
- `src/background/api-client.ts`: typed `fetch` wrapper. It sends bearer auth only
  (`credentials: 'omit'`), adds an `X-Request-ID: ext_<24 hex>` header, applies a hard
  timeout, parses the API error envelope into `ApiError`, and validates every response
  body before use.
- `src/background/session-manager.ts`: sign-in, session lookup, lazy refresh and
  secure logout (see "Authentication design" below).
- `src/background/connections.ts` and `preferences.ts`: connection list and test; the
  last tenant and email used, plus the selected connection per user, kept in
  `chrome.storage.local`. These are non-secret values only.
- `src/app/core/messaging/background-bridge.service.ts`: typed Observable wrapper with a
  timeout. Outside the extension (for example `ng serve`), it reports `NOT_IN_EXTENSION`.
- `src/app/core/auth/`: `AuthService` holds session signals and listens for
  `auth.changed`. `authGuard` protects `/chat`, `/history` and `/connections`;
  `guestGuard` keeps signed-in users off `/login`. `/settings` is always reachable.
- `src/app/connections/`: `WorkspaceSelectorComponent`, `ConnectionsListComponent`,
  `WorkspaceService`.

## Authentication design

- **Messages:** `auth.login {tenant, email, password}` returns `SessionInfo {user}`.
  The other auth messages are `auth.logout`, `auth.session` (returns `SessionInfo | null`)
  and `auth.loginHints`. The side panel never receives tokens.
- **Password handling:** the password travels once, from the sign-in form to the
  background worker to `POST /api/v1/auth/login`. It is never stored or logged, and the
  form clears it after every attempt.
- **Storage:** the access token, its expiry, the refresh token, its expiry and the user
  live in `chrome.storage.session`. That area is memory-only, cleared when the browser
  closes, and locked to `TRUSTED_CONTEXTS`.
- **Lazy refresh:** when the access token expires within 60 s, the worker calls
  `POST /api/v1/auth/refresh` and stores the rotated refresh token. Refreshes are
  single-flight: concurrent requests share one in-flight refresh, because the server
  treats reuse of a rotated refresh token as theft.
- **Expired access token:** an API call that returns 401 `TOKEN_EXPIRED` triggers
  exactly one refresh and one retry.
- **Ended session:** if a refresh is rejected (`REFRESH_TOKEN_INVALID`,
  `SESSION_REVOKED`), or a call returns 401 `SESSION_REVOKED` or `UNAUTHENTICATED`,
  the worker clears the session and broadcasts `auth.changed`. The side panel then
  returns to sign-in.
- **Network errors during refresh** keep the session, so a later attempt can succeed.
- **Logout:** `POST /api/v1/auth/logout` with the refresh token, best effort. The local
  session is always cleared, even when that call fails.
- **Error mapping:** API errors reach the UI as protocol codes such as
  `INVALID_CREDENTIALS`, `RATE_LIMITED` (with `retryAfterSeconds`), `SESSION_EXPIRED`,
  `PERMISSION_DENIED`, `NETWORK_ERROR` and `API_ERROR`. For 5xx responses the server
  message is replaced with generic text.
- `src/content/`: intentionally empty. No content scripts are injected (see its README).

## Security notes

- The extension never holds database credentials. It only knows the API base URL.
- The API URL must use HTTPS. The only exception is loopback hosts (`localhost`,
  `127.0.0.1`, `[::1]`). Credentials, query strings, and fragments are rejected.
- Permissions: `sidePanel` and `storage` only. There is no `externally_connectable`, so
  web pages cannot message the extension.
- CSP: `script-src 'self'; object-src 'self'; base-uri 'self'; frame-ancestors 'none'`.

### Production hosts

`host_permissions` lists only `http://localhost:8080/*` for development. For a
production build, set it to your API origin, for example
`"host_permissions": ["https://insight.example.com/*"]`, and set
`defaultApiBaseUrl` in `src/environments/environment.ts`. If users point the extension
at an API origin that isn't listed in `host_permissions`, CORS still applies: the API
must allow the `chrome-extension://<id>` origin. Use a fixed extension ID (a `key`
in the manifest or a Web Store ID) so the server-side CORS allowlist stays stable.

## Known limitations (Phase 2)

- Chat querying and history are UI shells (Phases 4–5).
- Sessions end when the browser closes, because tokens live in session storage.
  There is no "remember me" option.
- The health check reads `GET /health/ready`. Any 2xx means `ok`, any other status
  means `degraded`, and a network failure or timeout means `offline`.
- Unit tests run under jsdom. There are no end-to-end tests in a real Chrome instance
  yet (planned for Phase 7).
