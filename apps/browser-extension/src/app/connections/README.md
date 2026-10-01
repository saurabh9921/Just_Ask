# connections

Workspace selection and data-source management.

- `WorkspaceService` loads `connections.list` and restores or persists the selected
  connection per user (`workspace.get` / `workspace.set`).
- `WorkspaceSelectorComponent` sits above the chat. It shows the tenant, a data-source
  picker, a status badge and a read-only badge.
- `ConnectionsListComponent` is the `/connections` route ("Data sources"). Everyone
  can list the sources they may query and **Test connection**. Users with the
  `connections.manage` permission (tenant and data admins) also get:
  - **Add data source** / **Edit** (`DataSourceFormComponent`): PostgreSQL, MySQL/MariaDB
    or SQL Server; host, port, database, schema, read-only user, password, TLS mode,
    read replica, row limit and query timeout;
  - automatic preparation after every successful **Test connection**: sync tables, then
    build the index, shown as *Connection tested → Syncing tables → Tables synced →
    Building index → Index built successfully* (failures stop the chain with a message).
    Steps that are already current are skipped, so re-testing does not re-run them; a
    new source, or an edit that changes the connection itself (type, host, port,
    database, schema or user), re-syncs and re-indexes;
  - **Sync tables** (metadata discovery) and **Build index** (semantic index) to force a
    re-run, with live status polling (disabled while a job runs);
  - **Delete** with an inline confirmation;
  - a status line and, when no semantic entity describes the database yet, the exact
    `onboard draft` command to create the semantic layer.
- `DataSourceAdminService` sends the `connections.create|update|delete|sync|index|status`
  messages; the background worker validates every payload (`router.ts`) before calling
  the API, which enforces the permission, the SSRF host policy and the read-only
  posture check.

Passwords are **write-only**: typed once, passed through the background worker to the
API (stored encrypted server-side), never persisted in the browser, never returned by
the API (connections expose only `hasSecret`), and never logged. Leaving the password
empty when editing keeps the stored one.
