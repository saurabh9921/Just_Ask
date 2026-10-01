import type { ExportJob, ExportRequest } from '../app/core/messaging/analytics-types';
import { parseDownloadToken, parseExportJob } from './analytics-parsers';
import type { ApiClient } from './api-client';
import type { SessionManager } from './session-manager';

export interface ExportsDependencies {
  readonly getBaseUrl: () => Promise<string>;
  /** Starts a browser download (`chrome.downloads.download`). */
  readonly startDownload: (url: string, fileName: string) => Promise<void>;
}

/** File names come from the API; keep them to a safe, flat subset anyway. */
export function safeFileName(name: string | null, fallback: string): string {
  const cleaned = (name ?? '')
    .replace(/[^A-Za-z0-9._-]+/g, '-')
    .replace(/^[.-]+/, '')
    .slice(0, 120);
  return cleaned === '' ? fallback : cleaned;
}

/**
 * Background exports on behalf of the signed-in user. The file itself never passes
 * through the side panel or extension storage: the API mints a one-time, short-lived
 * download URL and the browser's download manager fetches it directly.
 */
export class ExportsService {
  constructor(
    private readonly api: ApiClient,
    private readonly session: SessionManager,
    private readonly deps: ExportsDependencies,
  ) {}

  create(request: ExportRequest): Promise<ExportJob> {
    return this.session.authorized((accessToken) =>
      this.api.request({
        method: 'POST',
        path: '/api/v1/exports',
        // The side panel only sends this after the user confirmed the export.
        body: {
          query_id: request.queryId,
          format: request.format,
          scope: request.scope,
          confirm: true,
        },
        accessToken,
        parse: parseExportJob,
      }),
    );
  }

  get(exportId: string): Promise<ExportJob> {
    return this.session.authorized((accessToken) =>
      this.api.request({
        method: 'GET',
        path: `/api/v1/exports/${encodeURIComponent(exportId)}`,
        accessToken,
        parse: parseExportJob,
      }),
    );
  }

  async download(exportId: string): Promise<{ readonly fileName: string }> {
    const job = await this.get(exportId);
    const token = await this.session.authorized((accessToken) =>
      this.api.request({
        method: 'POST',
        path: `/api/v1/exports/${encodeURIComponent(exportId)}/download-token`,
        accessToken,
        parse: parseDownloadToken,
      }),
    );
    const base = (await this.deps.getBaseUrl()).replace(/\/+$/, '');
    const fileName = safeFileName(job.fileName, `erp-insight-export.${job.format}`);
    await this.deps.startDownload(`${base}${token.path}`, fileName);
    return { fileName };
  }
}
