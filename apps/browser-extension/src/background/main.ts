/**
 * ERP Insight AI - background service worker (Manifest V3).
 *
 * Owns everything security-sensitive: settings, API access, authentication state
 * (tokens in `chrome.storage.session`), token refresh and message routing. The
 * side panel never calls the API or sees tokens directly.
 */
import { environment } from '../environments/environment';
import type { AuthChangedEvent } from '../app/core/messaging/protocol';
import { ANALYTICS_PORT } from '../app/core/messaging/analytics-types';
import { AnalyticsHttp, AnalyticsPortHandler } from './analytics-port';
import { AnalyticsSocket, type SocketLike } from './analytics-socket';
import { ApiClient, newRequestId } from './api-client';
import { ConnectionsService } from './connections';
import { checkHealth } from './health';
import { PreferencesStore } from './preferences';
import { createMessageRouter } from './router';
import { SessionManager } from './session-manager';
import { SettingsStore } from './settings';
import { ExportsService } from './exports';
import { HistoryService } from './history';
import { fromChromeStorageArea } from './storage';
import { TokenStore } from './token-store';

const localStorageArea = fromChromeStorageArea(chrome.storage.local);

const settingsStore = new SettingsStore(localStorageArea, {
  apiBaseUrl: environment.defaultApiBaseUrl,
});
const preferences = new PreferencesStore(localStorageArea);

const tokenStore = new TokenStore(fromChromeStorageArea(chrome.storage.session), {
  setAccessLevel: (options) => chrome.storage.session.setAccessLevel(options),
});

const api = new ApiClient({
  getBaseUrl: async () => (await settingsStore.get()).apiBaseUrl,
  fetchFn: (input, init) => fetch(input, init),
  timeoutMs: environment.apiTimeoutMs,
});

function broadcast(event: AuthChangedEvent): void {
  // Rejects with "Receiving end does not exist" when no extension page is open.
  chrome.runtime.sendMessage(event).catch(() => undefined);
}

const session = new SessionManager(tokenStore, api, { broadcast });
const connections = new ConnectionsService(api, session);
const history = new HistoryService(api, session);
const exports = new ExportsService(api, session, {
  getBaseUrl: async () => (await settingsStore.get()).apiBaseUrl,
  startDownload: async (url, filename) => {
    await chrome.downloads.download({ url, filename, conflictAction: 'uniquify', saveAs: false });
  },
});

const analytics = new AnalyticsPortHandler({
  extensionId: chrome.runtime.id,
  socket: new AnalyticsSocket({
    getBaseUrl: async () => (await settingsStore.get()).apiBaseUrl,
    getAccessToken: (forceRefresh) => session.accessToken(forceRefresh),
    // WebSocket's typed handlers (Event subclasses) accept everything SocketLike passes.
    createSocket: (url) => new WebSocket(url) as unknown as SocketLike,
  }),
  http: new AnalyticsHttp(api, session),
  newIdempotencyKey: () => newRequestId().replace('ext_', 'idem_'),
});

const handleMessage = createMessageRouter({
  extensionId: chrome.runtime.id,
  getSettings: () => settingsStore.get(),
  setSettings: (input) => settingsStore.set(input),
  checkHealth: (apiBaseUrl) =>
    checkHealth(apiBaseUrl, {
      fetchFn: (input, init) => fetch(input, init),
      timeoutMs: environment.healthCheckTimeoutMs,
    }),
  auth: session,
  connections,
  preferences,
  history,
  exports,
});

tokenStore.init().catch(() => console.error('[eia] failed to restrict session storage'));

chrome.sidePanel
  .setPanelBehavior({ openPanelOnActionClick: true })
  .catch(() => console.error('[eia] failed to configure side panel behaviour'));

chrome.runtime.onConnect.addListener((port) => {
  if (port.name === ANALYTICS_PORT) {
    analytics.attach(port);
  }
});

chrome.runtime.onMessage.addListener((message: unknown, sender, sendResponse) => {
  void handleMessage(message, sender).then(sendResponse);
  // Keep the channel open for the asynchronous response.
  return true;
});
