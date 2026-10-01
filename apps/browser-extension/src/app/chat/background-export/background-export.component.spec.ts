import { TestBed } from '@angular/core/testing';
import { map, of, throwError, timer, type Observable } from 'rxjs';

import type { ExportJob } from '../../core/messaging/analytics-types';
import {
  BackgroundBridgeService,
  BridgeError,
} from '../../core/messaging/background-bridge.service';
import { queryResponse, QUERY_ID } from '../../../testing/analytics-fixtures';
import { SourceMetadataComponent } from '../source-metadata/source-metadata.component';
import { BackgroundExportComponent, EXPORT_POLL_MS } from './background-export.component';

const EXPORT_ID = '9d2f7c1a-4b3e-4f5a-8c6d-1e2f3a4b5c6d';

function job(overrides: Partial<ExportJob> = {}): ExportJob {
  return {
    exportId: EXPORT_ID,
    queryId: QUERY_ID,
    format: 'csv',
    scope: 'all_rows',
    status: 'succeeded',
    rowCount: 1200,
    byteSize: 4096,
    truncated: false,
    fileName: 'sales.csv',
    errorCode: null,
    createdAt: '2026-09-23T10:00:00Z',
    expiresAt: '2026-09-23T11:00:00Z',
    ...overrides,
  };
}

interface Message {
  type: string;
  payload?: unknown;
}

function render(handler: (message: Message) => Observable<unknown>) {
  const request = vi.fn(handler);
  TestBed.configureTestingModule({
    providers: [
      { provide: BackgroundBridgeService, useValue: { request } },
      { provide: EXPORT_POLL_MS, useValue: 1 },
    ],
  });
  const fixture = TestBed.createComponent(BackgroundExportComponent);
  fixture.componentRef.setInput('queryId', QUERY_ID);
  fixture.detectChanges();
  const element = fixture.nativeElement as HTMLElement;
  const button = (label: string) =>
    Array.from(element.querySelectorAll('button')).find((b) => b.textContent.includes(label))!;
  const settle = async () => {
    for (let i = 0; i < 20; i++) {
      await new Promise((resolve) => setTimeout(resolve, 5));
      fixture.detectChanges();
    }
  };
  return { request, element, button, fixture, settle };
}

describe('BackgroundExportComponent', () => {
  it('requires confirmation, polls until ready and starts the download', async () => {
    let polls = 0;
    const { request, element, button, fixture, settle } = render((m) => {
      if (m.type === 'exports.create') return of(job({ status: 'pending' }));
      if (m.type === 'exports.get') {
        polls += 1;
        return of(job(polls < 3 ? { status: 'running' } : {}));
      }
      // Asynchronous, like the real bridge: the poll stream completes first.
      return timer(60).pipe(map(() => ({ fileName: 'sales.csv' })));
    });
    button('All rows').click();
    fixture.detectChanges();
    expect(request).not.toHaveBeenCalled();
    expect(element.textContent).toContain('every matching row');
    button('Export all rows').click();
    await new Promise((resolve) => setTimeout(resolve, 30));
    fixture.detectChanges();
    expect(element.textContent).not.toContain('taking longer'); // download still in flight
    await settle();
    expect(request.mock.calls[0]![0]).toEqual({
      type: 'exports.create',
      payload: { queryId: QUERY_ID, format: 'csv', scope: 'all_rows' },
    });
    expect(polls).toBe(3);
    expect(request).toHaveBeenLastCalledWith({
      type: 'exports.download',
      payload: { exportId: EXPORT_ID },
    });
    expect(element.textContent).toContain('Downloading sales.csv (1200 rows)');
  });

  it('explains a job stopped by the sensitive-data scan and never downloads', async () => {
    const { request, element, button, fixture, settle } = render((m) =>
      m.type === 'exports.create'
        ? of(job({ status: 'pending' }))
        : of(job({ status: 'failed', errorCode: 'SENSITIVE_DATA_DETECTED' })),
    );
    button('All rows').click();
    fixture.detectChanges();
    button('Export all rows').click();
    await settle();
    expect(element.textContent).toContain('card or bank numbers');
    expect(request.mock.calls.some(([m]) => m.type === 'exports.download')).toBe(false);
  });

  it('shows API refusals (e.g. too many exports)', async () => {
    const { element, button, fixture, settle } = render(() =>
      throwError(() => new BridgeError('RATE_LIMITED', 'You already have 2 exports running.')),
    );
    button('All rows').click();
    fixture.detectChanges();
    button('Export all rows').click();
    await settle();
    expect(element.textContent).toContain('You already have 2 exports running.');
  });

  it('warns when the platform export cap truncated the file', async () => {
    const { element, button, fixture, settle } = render((m) =>
      m.type === 'exports.download' ? of({ fileName: 'sales.csv' }) : of(job({ truncated: true })),
    );
    button('All rows').click();
    fixture.detectChanges();
    button('Export all rows').click();
    await settle();
    expect(element.textContent).toContain('platform export limit was reached');
  });
});

describe('SourceMetadataComponent cache marker', () => {
  it('shows cached answers and their age', () => {
    const fixture = TestBed.createComponent(SourceMetadataComponent);
    fixture.componentRef.setInput(
      'response',
      queryResponse({ cache: { hit: true, age_seconds: 125 } }),
    );
    fixture.detectChanges();
    const marker = (fixture.nativeElement as HTMLElement).querySelector(
      '[data-testid="cache-hit"]',
    );
    expect(marker?.textContent).toContain('cached result, 2 min old');
  });
});
