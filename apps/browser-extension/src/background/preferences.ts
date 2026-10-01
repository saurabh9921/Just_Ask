import type { LoginHints, WorkspaceSelection } from '../app/core/messaging/protocol';
import type { KeyValueStorage } from './storage';

const LOGIN_HINTS_KEY = 'eia.prefs.loginHints.v1';
const WORKSPACE_KEY = 'eia.prefs.workspace.v1';

const TENANT_SLUG = /^[a-z0-9][a-z0-9-]{0,62}$/;
export const CONNECTION_ID = /^[A-Za-z0-9_-]{1,64}$/;

/**
 * Non-secret per-user preferences in `chrome.storage.local`: the last tenant/email
 * used to sign in and the selected connection. Never passwords or tokens.
 */
export class PreferencesStore {
  constructor(private readonly storage: KeyValueStorage) {}

  async getLoginHints(): Promise<LoginHints | null> {
    const stored = (await this.storage.get(LOGIN_HINTS_KEY))[LOGIN_HINTS_KEY];
    if (typeof stored !== 'object' || stored === null) {
      return null;
    }
    const v = stored as Record<string, unknown>;
    const tenant = v['tenant'];
    const email = v['email'];
    if (typeof tenant !== 'string' || !TENANT_SLUG.test(tenant) || typeof email !== 'string') {
      return null;
    }
    return { tenant, email: email.slice(0, 320) };
  }

  async setLoginHints(hints: LoginHints): Promise<void> {
    await this.storage.set({
      [LOGIN_HINTS_KEY]: { tenant: hints.tenant, email: hints.email },
    });
  }

  /** The selected connection is remembered per user so accounts never share it. */
  async getWorkspace(userId: string): Promise<WorkspaceSelection> {
    const stored = (await this.storage.get(WORKSPACE_KEY))[WORKSPACE_KEY];
    if (typeof stored === 'object' && stored !== null) {
      const v = stored as Record<string, unknown>;
      const id = v['connectionId'];
      if (v['userId'] === userId && typeof id === 'string' && CONNECTION_ID.test(id)) {
        return { connectionId: id };
      }
    }
    return { connectionId: null };
  }

  async setWorkspace(userId: string, selection: WorkspaceSelection): Promise<WorkspaceSelection> {
    await this.storage.set({
      [WORKSPACE_KEY]: { userId, connectionId: selection.connectionId },
    });
    return selection;
  }
}

export function isTenantSlug(value: unknown): value is string {
  return typeof value === 'string' && TENANT_SLUG.test(value);
}
